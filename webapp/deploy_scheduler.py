"""发布调度器：定时发布 + 窗口管控 + 灰度推进"""
import json
import os
import socket
import sys
import threading
import time


# 调度执行互斥锁名（多实例/多 worker 部署时保证同一时刻仅一个实例处理到期计划）
_SCHEDULER_LOCK = "deploy_scheduler"
# 锁租约：单轮处理耗时 + 余量；崩溃残留锁过期后自动可重新获取
_LOCK_TTL = 300


def _lock_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def try_acquire_scheduler_lock(db_path: str, ttl: int = _LOCK_TTL,
                               lock_name: str = _SCHEDULER_LOCK) -> bool:
    """获取调度执行租约锁（SQLite 事务串行化，多进程安全）。

    锁记录带过期时间：持有进程崩溃时残留锁在 TTL 后自动失效。
    lock_name 可为不同后台任务取独立锁（如 agent_digest），互不阻塞。"""
    from . import db as dbm
    owner = _lock_owner()
    conn = dbm.get_conn(db_path)
    try:
        now = int(time.time())
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT expires_at FROM scheduler_locks WHERE name=?",
            (lock_name,)
        ).fetchone()
        if row and row["expires_at"] > now:
            return False  # 其他实例持有中
        conn.execute(
            "INSERT INTO scheduler_locks (name, owner, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, "
            "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
            (lock_name, owner, now, now + ttl)
        )
        conn.commit()
        return True
    finally:
        conn.close()


def release_scheduler_lock(db_path: str,
                           lock_name: str = _SCHEDULER_LOCK):
    """释放本实例持有的调度锁（仅删除 owner 匹配的记录，防误删他人锁）"""
    from . import db as dbm
    conn = dbm.get_conn(db_path)
    try:
        conn.execute(
            "DELETE FROM scheduler_locks WHERE name=? AND owner=?",
            (lock_name, _lock_owner())
        )
        conn.commit()
    finally:
        conn.close()


def compute_canary_batches(steps: list, total_servers: int) -> list:
    """根据 canary_config.steps 和服务器总数，计算每批次的服务器数量。

    steps 中 servers 字段：
      - 整数：该批次的服务器数量
      - "all"：剩余所有服务器
    返回 [{"batch": int, "server_count": int, "observe_minutes": int,
           "auto_advance": bool, "server_indices": [int, ...]}]
    """
    batches = []
    assigned = 0
    for step in steps:
        srv_count = step["servers"]
        if srv_count == "all":
            count = total_servers - assigned
        else:
            count = min(int(srv_count), total_servers - assigned)
        if count <= 0:
            continue
        indices = list(range(assigned, assigned + count))
        batches.append({
            "batch": step["batch"],
            "server_count": count,
            "observe_minutes": step.get("observe_minutes", 0),
            "auto_advance": step.get("auto_advance", False),
            "server_indices": indices,
        })
        assigned += count
        if assigned >= total_servers:
            break
    return batches


def check_release_window(db_path: str, project_id: int,
                         env_id: int = None,
                         server_id: int = None,
                         service: int = None) -> dict | None:
    """检查当前是否命中发布窗口（仅按已知维度匹配）。

    返回 None 表示未命中任何窗口；
    返回 {"id", "name", "block_level"} 表示命中。
    repeat_yearly=1 的窗口将年份偏移到当前年份后比较。

    作用域匹配逻辑（与 alert_rules.scope 同构）：
      - type=all → 匹配所有部署
      - type=project → project_id 在 scope.ids 中
      - type=env → env_id 在 scope.ids 中
      - type=server → server_id 在 scope.ids 中
      - type=service → 服务 ID 在 scope.ids 中
    旧版无 scope 列的窗口回退到 project_id 匹配。

    完整发布目标（环境名/服务/流水线/多台服务器）请用 check_deploy_window，
    本函数保留给只掌握单个维度的调用方（如顾问建议）。
    """
    return _match_windows(db_path, project_id, env_id,
                          [server_id] if server_id is not None else [],
                          [service] if service is not None else [])


def check_deploy_window(db_path: str, project_id: int, env: str = "",
                        ref_type: str = "", ref_id: int = 0) -> dict | None:
    """按一次发布的完整目标（环境 + 服务/流水线 + 服务器）检查窗口。

    API 建单、调度器、审批/执行前统一走本函数，避免只传 project_id
    导致 env/server/service 作用域窗口被漏判。"""
    target = resolve_window_target(db_path, project_id, env=env,
                                   ref_type=ref_type, ref_id=ref_id)
    return _match_windows(db_path, project_id, target["env_id"],
                          target["server_ids"], target["service_ids"])


def resolve_window_target(db_path: str, project_id: int, env: str = "",
                          ref_type: str = "", ref_id: int = 0) -> dict:
    """解析发布目标在窗口作用域中的维度。

    - environment 名 → 环境 ID（不存在时为 None）
    - 服务 → 服务 ID；流水线 → 展开 stages 内全部服务 ID
    - 服务器 → 目标环境绑定的服务器（与重启/监控同口径）
    """
    from . import db as dbm
    env_obj = dbm.get_env_by_name(db_path, project_id, env) if env else None
    env_id = env_obj["id"] if env_obj else None
    server_ids = [s["id"] for s in dbm.list_env_servers(db_path, env_id)] \
        if env_id is not None else []
    service_ids = []
    kind = (ref_type or "").lower()
    if kind == "deploy_schedules":
        # 审批单引用定时计划时，按计划自身的发布目标解析
        sched = dbm.get_record(db_path, "deploy_schedules", int(ref_id or 0))
        if sched and sched.get("project_id") == project_id:
            return resolve_window_target(
                db_path, project_id, env=sched.get("env") or "",
                ref_type=sched.get("ref_type") or "",
                ref_id=sched.get("ref_id") or 0)
    if kind in ("project", "restart", "service", "services"):
        rec = dbm.get_record(db_path, "services", int(ref_id or 0))
        if rec and rec.get("project_id") == project_id:
            service_ids.append(rec["id"])
    elif kind in ("pipeline", "pipelines"):
        pl = dbm.get_record(db_path, "pipelines", int(ref_id or 0))
        if pl and pl.get("project_id") == project_id:
            seen = set()
            for stage in pl.get("stages") or []:
                for item in (stage or {}).get("services") or []:
                    sid = item.get("service_id")
                    if not sid and item.get("name"):
                        svc = dbm.get_service_by_name(db_path, project_id,
                                                      item["name"])
                        sid = svc["id"] if svc else None
                    if not sid or sid in seen:
                        continue
                    rec = dbm.get_record(db_path, "services", int(sid))
                    if rec and rec.get("project_id") == project_id:
                        seen.add(sid)
                        service_ids.append(sid)
    return {"env_id": env_id, "server_ids": server_ids,
            "service_ids": service_ids}


def _match_windows(db_path: str, project_id: int, env_id: int = None,
                   server_ids: list = None, service_ids: list = None):
    """收集命中时间与作用域的窗口，block 优先于 warn"""
    now = int(time.time())
    windows = []
    for w in _list_windows(db_path):
        if not _window_matches(w, now):
            continue
        if _scope_matches(w, project_id, env_id, server_ids or [],
                          service_ids or []):
            windows.append(w)
    if not windows:
        return None
    blocks = [w for w in windows if w["block_level"] == "block"]
    return blocks[0] if blocks else windows[0]


def _list_windows(db_path: str) -> list:
    """查询所有窗口"""
    from . import db as dbm
    return dbm.list_records(db_path, "release_windows", "", (), "id DESC")


def _scope_matches(w: dict, project_id: int,
                   env_id: int = None,
                   server_ids: list = None,
                   service_ids: list = None) -> bool:
    """判断窗口的作用域是否匹配当前部署目标。

    server/service 作用域允许目标集合中任一元素命中；scope.env_ids
    非空时再按环境收窄（前端 server/service 选择器的级联环境）。"""
    scope = w.get("scope")
    # 兼容旧数据：无 scope 字段时回退到 project_id 匹配
    if not scope or not isinstance(scope, dict):
        wp = w.get("project_id", 0)
        return wp == 0 or wp == project_id
    st = scope.get("type", "all")
    ids = scope.get("ids") or []
    env_ids = scope.get("env_ids") or []
    if st == "all":
        return True
    if st == "project":
        return project_id in ids
    if st == "env":
        return env_id is not None and env_id in ids
    if st == "server":
        if env_ids and env_id not in env_ids:
            return False
        return any(sid in ids for sid in (server_ids or []))
    if st == "service":
        if env_ids and env_id not in env_ids:
            return False
        return any(sid in ids for sid in (service_ids or []))
    return False


def _window_matches(w: dict, now: int) -> bool:
    """判断窗口在当前时刻是否生效（支持年度重复）"""
    start = w["start_at"]
    end = w["end_at"]
    if w.get("repeat_yearly"):
        # 将窗口的年份偏移到当前年份
        import time as _t
        cur_year = _t.localtime(now).tm_year
        win_year = _t.localtime(start).tm_year
        offset = (cur_year - win_year) * 365 * 24 * 3600
        # 粗略偏移（忽略闰年），精确到天级别足够
        start += offset
        end += offset
    return start <= now <= end


class DeployScheduler:
    """发布调度守护线程：扫描到期计划 → 窗口检查 → 创建任务"""

    def __init__(self, db_path: str, interval: int = 30):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="deploy-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self._find_due_schedules():
                    self._process_due()
            except Exception as e:
                print(f"[deploy-sched] 周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _find_due_schedules(self) -> list:
        """查找所有到期且启用的发布计划"""
        from . import db as dbm
        now = int(time.time())
        return dbm.list_records(
            self._db_path, "deploy_schedules",
            "enabled=1 AND next_run_at > 0 AND next_run_at <= ?",
            (now,), "next_run_at")

    def _process_due(self):
        """处理到期计划（带租约锁）；锁被其他实例持有时跳过本轮

        到期列表在拿到锁后重新读取：调用前扫描到的计划可能已在排队期间
        被禁用/改期，直接消费旧列表会执行已取消的计划。
        """
        if not try_acquire_scheduler_lock(self._db_path):
            print("[deploy-sched] 调度锁被其他实例持有，本轮跳过")
            return
        try:
            schedules = self._find_due_schedules()
            if schedules:
                self._process_due_locked(schedules)
        finally:
            release_scheduler_lock(self._db_path)

    def _process_due_locked(self, schedules: list):
        """持锁后的到期计划处理：窗口检查 → 创建任务 → 更新下次执行时间"""
        from . import db as dbm
        now = int(time.time())
        for sched in schedules:
            # 窗口检查：按计划的环境/服务/服务器完整作用域匹配
            window = check_deploy_window(
                self._db_path, sched["project_id"],
                env=sched.get("env") or "",
                ref_type=sched.get("ref_type") or "",
                ref_id=sched.get("ref_id") or 0)
            if window and window["block_level"] == "block":
                # block 窗口：跳过执行，更新 next_run_at
                self._update_next_run(sched, now)
                print(f"[deploy-sched] 跳过 {sched['name']}：命中封网窗口 '{window['name']}'")
                continue

            forced = 0
            if window and window["block_level"] == "warn":
                forced = 1

            # 创建任务
            try:
                # extra 已由 db 层反序列化为 list；兼容历史双编码字符串
                extra = sched.get("extra") or []
                if isinstance(extra, str):
                    try:
                        extra = json.loads(extra)
                    except (ValueError, TypeError):
                        extra = []
                if not isinstance(extra, list):
                    extra = []
                extra = list(extra)
                if forced:
                    extra.append("--force-window")
                # ref_type=service 时，解析服务名作为任务名
                task_name = sched["name"]
                if sched.get("ref_type") == "service" and sched.get("ref_id"):
                    svc = dbm.get_record(self._db_path, "services", sched["ref_id"])
                    if svc:
                        task_name = svc["name"]
                task_id = dbm.create_task(
                    self._db_path, sched["project_id"],
                    sched["ref_type"], sched["ref_id"],
                    task_name, sched["env"],
                    sched["version"], sched["previous_version"],
                    sched["created_by"], extra=extra)
                # 提交任务执行
                from . import task_manager
                task = dbm.get_task(self._db_path, task_id)
                task_manager.submit_task(self._db_path, task, extra)
                print(f"[deploy-sched] 定时任务 #{task_id} 已创建: {sched['name']}")
            except Exception as e:
                # 创建/提交失败：不推进 next_run_at，下轮重试；
                # 否则 cron 计划会跳过本次并静默等到下一个周期，once 计划则直接禁用
                print(f"[deploy-sched] 创建任务失败（保留 next_run_at 重试）: {e!r}",
                      file=sys.stderr)
                continue

            self._update_next_run(sched, now)

    def _update_next_run(self, sched: dict, now: int):
        """更新 last_run_at 和 next_run_at"""
        from . import db as dbm
        updates = {"last_run_at": now}
        if sched["type"] == "cron" and sched.get("cron_expr"):
            next_ts = self._calc_next_run("cron", sched["cron_expr"], now)
            if next_ts is None:
                # 解析失败：不推进 next_run_at（保留原值，下轮继续尝试触发日志），
                # 避免写 0 导致计划静默失效且无任何告警
                print(f"[deploy-sched] !! 计划 {sched['name']} (id={sched['id']}) CRON 解析失败"
                      f" '{sched.get('cron_expr')}'，保留 next_run_at={sched.get('next_run_at')} 待修复",
                      file=sys.stderr)
                return
            updates["next_run_at"] = next_ts
        else:
            # once 模式：执行后禁用
            updates["enabled"] = 0
            updates["next_run_at"] = 0
        dbm.update_record(self._db_path, "deploy_schedules", sched["id"], updates)

    def _calc_next_run(self, sched_type: str, cron_expr: str,
                       base_ts: int):
        """计算下次执行时间；解析失败返回 None（调用方不得写入 0）"""
        if sched_type != "cron" or not cron_expr:
            return None
        try:
            from croniter import croniter
            from datetime import datetime
            cron = croniter(cron_expr, datetime.fromtimestamp(base_ts))
            next_dt = cron.get_next()
            return int(next_dt)
        except (ImportError, ValueError, KeyError) as e:
            print(f"[deploy-sched] CRON 解析失败 '{cron_expr}': {e!r}",
                  file=sys.stderr)
            return None
