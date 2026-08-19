"""发布调度器：定时发布 + 窗口管控 + 灰度推进"""
import json
import sys
import threading
import time


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


def check_release_window(db_path: str, project_id: int) -> dict | None:
    """检查当前是否命中发布窗口。

    返回 None 表示未命中任何窗口；
    返回 {"id", "name", "block_level"} 表示命中。
    repeat_yearly=1 的窗口将年份偏移到当前年份后比较。
    优先匹配项目级窗口（project_id 匹配），其次全局窗口（project_id=0）。
    """
    now = int(time.time())
    windows = []
    # 项目级 + 全局窗口
    for w in _list_windows(db_path, project_id):
        if _window_matches(w, now):
            windows.append(w)
    if not windows:
        return None
    # block 优先于 warn
    blocks = [w for w in windows if w["block_level"] == "block"]
    return blocks[0] if blocks else windows[0]


def _list_windows(db_path: str, project_id: int) -> list:
    """查询项目级 + 全局窗口"""
    from . import db as dbm
    return dbm.list_records(
        db_path, "release_windows",
        "project_id=? OR project_id=0", (project_id,))


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
                due = self._find_due_schedules()
                if due:
                    self._process_due(due)
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

    def _process_due(self, schedules: list):
        """处理到期计划：窗口检查 → 创建任务 → 更新下次执行时间"""
        from . import db as dbm
        now = int(time.time())
        for sched in schedules:
            # 窗口检查
            window = check_release_window(self._db_path, sched["project_id"])
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
                extra = json.loads(sched.get("extra") or "[]")
                if forced:
                    extra.append("--force-window")
                task_id = dbm.create_task(
                    self._db_path, sched["project_id"],
                    sched["ref_type"], sched["ref_id"],
                    sched["name"], sched["env"],
                    sched["version"], sched["previous_version"],
                    sched["created_by"], extra=extra)
                # 提交任务执行
                from . import task_manager
                task = dbm.get_task(self._db_path, task_id)
                task_manager.submit_task(self._db_path, task, extra)
                print(f"[deploy-sched] 定时任务 #{task_id} 已创建: {sched['name']}")
            except Exception as e:
                print(f"[deploy-sched] 创建任务失败: {e!r}", file=sys.stderr)

            self._update_next_run(sched, now)

    def _update_next_run(self, sched: dict, now: int):
        """更新 last_run_at 和 next_run_at"""
        from . import db as dbm
        updates = {"last_run_at": now}
        if sched["type"] == "cron" and sched.get("cron_expr"):
            next_ts = self._calc_next_run("cron", sched["cron_expr"], now)
            updates["next_run_at"] = next_ts
        else:
            # once 模式：执行后禁用
            updates["enabled"] = 0
            updates["next_run_at"] = 0
        dbm.update_record(self._db_path, "deploy_schedules", sched["id"], updates)

    def _calc_next_run(self, sched_type: str, cron_expr: str,
                       base_ts: int) -> int:
        """计算下次执行时间"""
        if sched_type != "cron" or not cron_expr:
            return 0
        try:
            from croniter import croniter
            from datetime import datetime
            cron = croniter(cron_expr, datetime.fromtimestamp(base_ts))
            next_dt = cron.get_next()
            return int(next_dt)
        except (ImportError, ValueError, KeyError) as e:
            print(f"[deploy-sched] CRON 解析失败 '{cron_expr}': {e!r}",
                  file=sys.stderr)
            return 0
