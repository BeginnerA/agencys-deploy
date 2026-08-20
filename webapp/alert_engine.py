"""告警规则引擎：规则评估（滑动窗口持续判定）、状态机、升级/收敛、静默窗口、任务失败检测

AlertEvaluator 守护线程每 interval 秒一轮：
    1. 指标规则：读 monitor 探测缓存 host_metrics，滑动窗口判断连续满足阈值 ≥ duration
    2. 状态规则：服务下线（not_deployed）、任务失败（扫描 tasks 表）
    3. 自动恢复：指标/服务恢复后连续 RECOVER_ROUNDS 轮正常 → resolved(auto)
    4. 升级：acked 超时 escalation_interval 未解决 → 级别提升并重新通知

收敛：alerts.fingerprint 唯一索引（部分索引，仅非 resolved），
同指纹重复触发只更新 last_seen_at，不重复建告警。
滑动窗口上限 _max_history 轮（@30s 约 20 分钟）：规则 duration_seconds 超过窗口
覆盖范围时将永远无法触发，创建规则时应限制在窗口内（见 alert_routes._validate_rule）。
"""
import sys
import threading
import time
from collections import deque

from . import db as dbm
from .alert_channels import send_alert

SEVERITY_ORDER = ("P0", "P1", "P2", "P3")

# 内置默认规则种子（首次启动导入，可编辑/禁用）
DEFAULT_RULES = [
    dict(name="主机 CPU 过高", metric_type="metric", metric="cpu",
         compare=">", threshold=80, duration_seconds=300,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="磁盘使用率过高", metric_type="metric", metric="disk",
         compare=">", threshold=90, duration_seconds=300,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="服务下线", metric_type="status", metric="service_down",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P1",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="任务失败", metric_type="status", metric="task_failed",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="日志错误关键词", metric_type="log_keyword", metric="ERROR",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P3",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则（日志联动）"),
]

# 自动恢复判定轮数（连续 N 轮正常才 resolved，避免单轮抖动误恢复）
RECOVER_ROUNDS = 2


def _compare(op: str, value, threshold) -> bool:
    if value is None:
        return False
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
    return ops.get(op, lambda a, b: a > b)(value, threshold)


class AlertEngine:
    """告警引擎（DB 每轮独立连接，与 monitor 相同模式）"""

    def __init__(self, db_path: str, interval: int = 30, monitor=None):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._monitor = monitor
        self._stop = threading.Event()
        self._thread = None
        # 滑动窗口：{(env_id, host, metric): deque[(ts, value)]}
        self._history = {}
        self._max_history = 40  # 40 轮 = 20 分钟 @30s
        # 扫描基线=启动时刻：不回扫启动前已失败的历史任务（增量语义，避免首启刷陈旧告警）
        self._last_task_scan = time.time()
        self._seed_rules()

    # ---------- 调度 ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="alert-engine")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                # 引擎自愈：单周期异常不退出（与 monitor 调度器同策略）
                print(f"[alert] 评估周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _cycle(self):
        self._evaluate_metric_rules()
        self._evaluate_status_rules()
        self._check_tasks()
        self._auto_resolve()
        self._escalate()

    # ---------- 种子规则 ----------

    def _seed_rules(self):
        """首次启动导入内置默认规则（幂等：表非空即跳过）"""
        if dbm.list_records(self._db_path, "alert_rules"):
            return
        now = int(time.time())
        for r in DEFAULT_RULES:
            dbm.create_record(self._db_path, "alert_rules", dict(
                name=r["name"], metric_type=r["metric_type"],
                metric=r["metric"], compare=r["compare"],
                threshold=r["threshold"], duration_seconds=r["duration_seconds"],
                scope=r["scope"], severity=r["severity"], channels=r["channels"],
                escalation_interval=r["escalation_interval"], enabled=1,
                remark=r["remark"], created_at=now, updated_at=now))

    # ---------- 滑动窗口 ----------

    def _push_history(self, key, value):
        """记录一轮探测值；None 视为探测失败（不参与连续计数但保留位置）"""
        dq = self._history.setdefault(key, deque(maxlen=self._max_history))
        dq.append((time.time(), value))

    def _consecutive_hits(self, rule, key) -> int:
        """从最新一轮向前数连续满足阈值的轮数"""
        dq = self._history.get(key)
        if not dq:
            return 0
        n = 0
        for ts, value in reversed(dq):
            if _compare(rule.get("compare", ">"), value, rule.get("threshold", 0)):
                n += 1
            else:
                break
        return n

    def _duration_hit(self, rule, key) -> bool:
        """连续满足轮数 × 周期 ≥ duration_seconds 才触发（duration=0 时一轮即触发）"""
        duration = int(rule.get("duration_seconds", 0) or 0)
        if duration <= 0:
            return self._consecutive_hits(rule, key) >= 1
        return self._consecutive_hits(rule, key) * self._interval >= duration

    # ---------- 指标规则 ----------

    def _enabled_env_ids(self):
        """返回启用项目的环境 ID 集合，避免为禁用/未配置项目产生告警"""
        enabled_pids = {p["id"] for p in dbm.list_records(self._db_path, "projects")
                        if p.get("enabled", 1)}
        return {e["id"] for e in dbm.list_records(self._db_path, "environments")
                if e["project_id"] in enabled_pids}

    def _project_code_map(self):
        """返回 project_id → project_code 映射"""
        return {p["id"]: p.get("code", "") for p in dbm.list_records(self._db_path, "projects")}

    def _evaluate_metric_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "metric"]
        if not rules or not self._monitor:
            return
        # 1) 数据源：monitor 探测缓存 → 灌入滑动窗口
        metrics = {r.get("metric") or "cpu" for r in rules} & {"cpu", "mem", "disk", "load"}
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for host, m in (data.get("host_metrics") or {}).items():
                for metric in metrics:
                    self._push_history((env["id"], host, metric), m.get(metric))
        # 2) 构建 hostname → server_id 映射
        host_to_sid = {}
        for s in dbm.list_records(self._db_path, "servers"):
            host_to_sid[s["host"]] = s["id"]
        # 3) 评估滑动窗口（仅处理启用项目的环境）
        env_map = {e["id"]: e for e in dbm.list_records(self._db_path, "environments")}
        enabled_env_ids = self._enabled_env_ids()
        pcode_map = self._project_code_map()
        for (env_id, host, metric) in list(self._history):
            if env_id not in enabled_env_ids:
                continue
            env = env_map.get(env_id)
            if env is None:
                continue
            key = (env_id, host, metric)
            value = self._history[key][-1][1]
            server_id = host_to_sid.get(host)
            for rule in rules:
                if (rule.get("metric") or "cpu") != metric:
                    continue
                if not self._duration_hit(rule, key):
                    continue
                if not self._in_scope(rule, env, host=host, server_id=server_id):
                    continue
                self._fire(rule, f"host:{env_id}:{host}",
                           subject=f"{host} {metric.upper()} 过高",
                           description=f"当前 {value}%，阈值 {rule['compare']}{rule['threshold']}%，"
                                       f"持续 {rule['duration_seconds']}s",
                           target_info={"project": pcode_map.get(env["project_id"], ""), "env": env["name"], "host": host})

    # ---------- 状态规则 ----------

    def _evaluate_status_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "status"]
        if not rules or not self._monitor:
            return
        # 构建 service name → service_id 映射（复合键：project_id + name）
        name_to_sid = {}
        for s in dbm.list_records(self._db_path, "services"):
            name_to_sid[(s["project_id"], s["name"])] = s["id"]
        enabled_env_ids = self._enabled_env_ids()
        pcode_map = self._project_code_map()
        for env in dbm.list_records(self._db_path, "environments"):
            if env["id"] not in enabled_env_ids:
                continue
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for name, st in (data.get("services") or {}).items():
                for rule in rules:
                    metric = rule.get("metric", "service_down")
                    if metric == "service_down" and st.get("status") == "not_deployed":
                        service_id = name_to_sid.get((env["project_id"], name))
                        if not self._in_scope(rule, env, service=name,
                                              service_id=service_id):
                            continue
                        self._fire(rule, f"service:{env['id']}:{name}",
                                   subject=f"服务下线: {name}",
                                   description=f"服务 {name} 在环境 {env['name']} 未部署或已停止",
                                   target_info={"project": pcode_map.get(env["project_id"], ""), "env": env["name"], "service": name})

    def _check_tasks(self):
        """扫描最近失败任务（增量：finished_at > 上次扫描点）触发任务失败告警"""
        now = time.time()
        rows = dbm.list_records(self._db_path, "tasks", "status='failed'",
                                (), "finished_at DESC")[:20]
        pcode_map = self._project_code_map()
        for t in rows:
            finished = t.get("finished_at") or 0
            if finished <= self._last_task_scan:
                continue
            rule = self._task_fail_rule()
            if not rule:
                continue
            # 守卫用完整指纹（{rule_id}:task:{id}），与 _fire 写入一致；
            # 仅拦截非 resolved——解决后再失败视为新事件
            fp = f"{rule['id']}:task:{t['id']}"
            if dbm.list_records(self._db_path, "alerts",
                                "fingerprint=? AND status != 'resolved'", (fp,)):
                continue
            self._fire(rule, f"task:{t['id']}",
                       subject=f"任务失败: {t['name']}",
                       description=f"任务 #{t['id']} {t['name']} [{t.get('env','')}] 执行失败",
                       target_info={"project": t.get("project_code", "") or pcode_map.get(t.get("project_id", 0), ""), "env": t.get("env", ""), "task_id": t["id"]})
        self._last_task_scan = now

    def _task_fail_rule(self):
        rows = [r for r in dbm.list_records(self._db_path, "alert_rules")
                if r.get("enabled") and r.get("metric") == "task_failed"]
        return rows[0] if rows else None

    # ---------- 触发与收敛 ----------

    def _fire(self, rule: dict, target: str, subject: str,
              description: str, target_info: dict) -> int:
        """创建或更新告警（收敛）；新告警首次触发时通知（静默窗口内不通知）

        收敛策略：
        - 同指纹活跃告警（firing/acked）→ 仅刷新 last_seen_at / 位置
        - 同指纹已解决告警（resolved）→ 重新激活（避免 partial unique index 冲突）
        - 无同指纹告警 → 新建
        """
        fp = f"{rule['id']}:{target}"
        # 先查活跃告警（收敛），再查已解决告警（重新激活）
        active = dbm.list_records(self._db_path, "alerts",
                                  "fingerprint=? AND status != 'resolved'", (fp,))
        now = int(time.time())
        if active:
            a = active[0]
            updates = {"last_seen_at": now}
            # 位置刷新：收敛时若新命中携带 entry_id 且与既有不同，仅更新位置字段
            # （entry_id/hit_ts），保留 context 等既有字段；非日志告警/旧调用
            # （target_info 无 entry_id）路径不变，不清除已有位置信息
            new_entry = target_info.get("entry_id")
            old_ti = a.get("target_info") or {}
            if new_entry is not None and old_ti.get("entry_id") != new_entry:
                merged = dict(old_ti)
                merged["entry_id"] = new_entry
                if target_info.get("hit_ts") is not None:
                    merged["hit_ts"] = target_info["hit_ts"]
                updates["target_info"] = merged
            # 静默期首次触发的告警：窗口已过期、仍在 firing 且从未通知 → 补发一次
            # （acked/resolved 不再补发：ack 后停止重复通知）
            if (a["status"] == "firing" and a["notify_count"] == 0
                    and not self._silenced(rule, target_info)):
                alert = dbm.get_record(self._db_path, "alerts", a["id"])
                send_alert(self._db_path, alert, rule.get("channels") or [])
                updates["notify_count"] = 1
            dbm.update_record(self._db_path, "alerts", a["id"], updates)
            return a["id"]
        # 无活跃告警：检查是否有已解决的告警可重新激活
        resolved = dbm.list_records(self._db_path, "alerts",
                                    "fingerprint=?", (fp,), order="id DESC")
        if resolved and resolved[0].get("status") == "resolved":
            a = resolved[0]
            updates = {
                "status": "firing",
                "subject": subject,
                "description": description,
                "severity": rule.get("severity", "P2"),
                "target_info": target_info,
                "first_seen_at": now,
                "last_seen_at": now,
                "notify_count": 0,
                "escalation_level": 0,
                "resolved_by": "",
                "resolved_at": None,
                "resolved_type": "",
                "ack_by": "",
                "ack_at": None,
                "escalation_at": now + int(rule.get("escalation_interval", 30)) * 60,
            }
            dbm.update_record(self._db_path, "alerts", a["id"], updates)
            alert = dbm.get_record(self._db_path, "alerts", a["id"])
            if self._silenced(rule, target_info):
                dbm.create_record(self._db_path, "alert_events", dict(
                    alert_id=a["id"], channel="silenced", target="", payload="",
                    ok=1, detail="静默窗口内，未发送通知", created_at=now))
            else:
                send_alert(self._db_path, alert, rule.get("channels") or [])
                dbm.update_record(self._db_path, "alerts", a["id"],
                                  {"notify_count": 1})
            return a["id"]
        # 无任何同指纹告警 → 新建
        alert_id = dbm.create_record(self._db_path, "alerts", dict(
            rule_id=rule["id"], fingerprint=fp, status="firing",
            severity=rule.get("severity", "P2"), subject=subject,
            description=description, target_info=target_info,
            first_seen_at=now, last_seen_at=now, notify_count=0,
            escalation_level=0,
            escalation_at=now + int(rule.get("escalation_interval", 30)) * 60))
        alert = dbm.get_record(self._db_path, "alerts", alert_id)
        if self._silenced(rule, target_info):
            dbm.create_record(self._db_path, "alert_events", dict(
                alert_id=alert_id, channel="silenced", target="", payload="",
                ok=1, detail="静默窗口内，未发送通知", created_at=now))
        else:
            send_alert(self._db_path, alert, rule.get("channels") or [])
            dbm.update_record(self._db_path, "alerts", alert_id,
                              {"notify_count": 1})
        return alert_id

    # ---------- 静默窗口 ----------

    def _silenced(self, rule: dict, target_info: dict) -> bool:
        """命中任一启用窗口（作用域：规则/环境/服务器，空作用域=全局）"""
        now = time.time()
        for w in dbm.list_records(self._db_path, "silence_windows"):
            if not w.get("enabled") or not self._window_active(w, now):
                continue
            scope = w.get("scope") or {}
            if not isinstance(scope, dict):
                continue  # 脏数据：非法 scope 视为窗口不生效
            rules = scope.get("rules") or []
            envs = scope.get("envs") or []
            servers = scope.get("servers") or []
            if not (rules or envs or servers):
                return True
            if rule["id"] in rules:
                return True
            if target_info.get("env") in envs:
                return True
            if target_info.get("host") in servers:
                return True
        return False

    def _window_active(self, w: dict, now: float) -> bool:
        """窗口时间匹配：one_time 用 epoch 秒；recurring 用 weekdays + HH:MM（支持跨午夜）"""
        if w.get("type") == "one_time":
            try:
                return float(w["start_at"]) <= now <= float(w["end_at"])
            except (TypeError, ValueError):
                return False
        lt = time.localtime(now)
        weekdays = w.get("weekdays") or []
        if weekdays and lt.tm_wday not in weekdays:
            return False

        def to_min(s):
            try:
                h, _, m = str(s).partition(":")
                return int(h) * 60 + int(m)
            except ValueError:
                return None  # 非法时间格式

        s_min = to_min(w.get("start_at", "00:00"))
        e_min = to_min(w.get("end_at", "23:59"))
        if s_min is None or e_min is None:
            return False  # 脏数据：非法时间视为窗口不生效
        cur = lt.tm_hour * 60 + lt.tm_min
        if s_min <= e_min:
            return s_min <= cur <= e_min
        return cur >= s_min or cur <= e_min  # 跨午夜

    # ---------- 作用域 ----------

    def _in_scope(self, rule: dict, env: dict, host: str = None,
                  service: str = None, server_id: int = None,
                  service_id: int = None) -> bool:
        """规则作用域：all/project/env/server/service

        优先使用数字 ID（server_id/service_id）匹配；
        向后兼容：ids 含字符串元素时降级为 hostname/service name 匹配。
        env_ids 非空时额外检查当前环境是否在列表中。
        """
        scope = rule.get("scope") or {"type": "all", "ids": []}
        if not isinstance(scope, dict):
            return True  # 脏数据：非法 scope 按全局放行
        t, ids = scope.get("type", "all"), scope.get("ids") or []
        env_ids = scope.get("env_ids") or []
        if t == "all":
            return True
        if t == "project":
            return env["project_id"] in ids
        if t == "env":
            return env["id"] in ids
        if t == "server":
            if env_ids and env["id"] not in env_ids:
                return False
            # 向后兼容：ids 含字符串 → 按 hostname 匹配
            if ids and isinstance(ids[0], str):
                return host in ids
            if server_id is not None:
                return server_id in ids
            return host in ids  # server_id 未传，降级
        if t == "service":
            if env_ids and env["id"] not in env_ids:
                return False
            if ids and isinstance(ids[0], str):
                return service in ids
            if service_id is not None:
                return service_id in ids
            return service in ids  # service_id 未传，降级
        return True

    # ---------- 自动恢复 ----------

    def _auto_resolve(self):
        """指标类告警：连续 RECOVER_ROUNDS 轮恢复 → resolved(auto)；
        服务下线告警：服务重新在线 → resolved(auto)；任务失败不自动恢复"""
        for a in dbm.list_records(self._db_path, "alerts",
                                  "status IN ('firing','acked')", (), "id"):
            rule = dbm.get_record(self._db_path, "alert_rules", a["rule_id"]) or {}
            if rule.get("metric_type") == "metric":
                if self._metric_recovered(rule, a):
                    self._resolve(a, "auto")
            elif rule.get("metric") == "service_down":
                if self._service_back(rule, a):
                    self._resolve(a, "auto")

    def _metric_recovered(self, rule: dict, alert: dict) -> bool:
        if not self._monitor:
            return False
        # 指纹格式：{rule_id}:host:{env_id}:{host}
        parts = alert["fingerprint"].split(":")
        if len(parts) != 4 or parts[1] != "host":
            return False
        try:
            env_id = int(parts[2])
        except ValueError:
            return False
        host = parts[3]
        key = (env_id, host, rule.get("metric", "cpu"))
        dq = self._history.get(key)
        if not dq:
            return False
        n = 0
        for ts, value in reversed(dq):
            if _compare(rule.get("compare", ">"), value,
                        rule.get("threshold", 0)):
                return False  # 最新一轮仍超阈值
            n += 1
            if n >= RECOVER_ROUNDS:
                return True
        return False

    def _service_back(self, rule: dict, alert: dict) -> bool:
        if not self._monitor:
            return False
        # 指纹格式：{rule_id}:service:{env_id}:{name}
        parts = alert["fingerprint"].split(":")
        if len(parts) != 4 or parts[1] != "service":
            return False
        try:
            env_id = int(parts[2])
        except ValueError:
            return False
        name = parts[3]
        envs = [e for e in dbm.list_records(self._db_path, "environments")
                if e["id"] == env_id]
        if not envs:
            return False
        env = envs[0]
        data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
        st = (data.get("services") or {}).get(name) or {}
        return st.get("status") == "online"

    def _resolve(self, alert: dict, rtype: str, by: str = ""):
        dbm.update_record(self._db_path, "alerts", alert["id"], dict(
            status="resolved", resolved_by=by, resolved_at=int(time.time()),
            resolved_type=rtype))

    # ---------- 升级 ----------

    def _escalate(self):
        """acked 超时未解决 → 级别提升一级（最多 P0），按新级别重新通知"""
        now = int(time.time())
        for a in dbm.list_records(self._db_path, "alerts",
                                  "status='acked' AND escalation_at IS NOT NULL "
                                  "AND escalation_at <= ?", (now,), "id"):
            rule = dbm.get_record(self._db_path, "alert_rules", a["rule_id"]) or {}
            sev = a.get("severity", "P2")
            if sev not in SEVERITY_ORDER:
                continue
            idx = SEVERITY_ORDER.index(sev)
            if idx <= 0:
                continue  # 已是 P0，不再升级
            new_sev = SEVERITY_ORDER[idx - 1]
            interval = int(rule.get("escalation_interval", 30)) * 60
            dbm.update_record(self._db_path, "alerts", a["id"], dict(
                severity=new_sev,
                escalation_level=a.get("escalation_level", 0) + 1,
                escalation_at=now + interval))
            a["severity"] = new_sev
            if not self._silenced(rule, a.get("target_info") or {}):
                send_alert(self._db_path, a, rule.get("channels") or [])

    # ---------- 供日志联动调用 ----------

    def trigger_log_alert(self, rule: dict, service: str, env_name: str,
                          line: str, context: list, env_id: int,
                          matched_keyword: str = "", entry_id: int = None,
                          hit_ts: int = None):
        """日志关键词命中：创建/更新日志告警（指纹含关键词，收敛由部分唯一索引保证）

        matched_keyword: 实际命中的那个关键词（多关键词场景下精确到单个词）
        entry_id/hit_ts: 命中日志行在 log_entries 的 rowid 与其时间戳，
        写入 target_info 供详情抽屉实时查询上下文（旧告警无此字段，向后兼容）
        """
        # 查询 env_id 对应的环境记录
        envs = dbm.list_records(self._db_path, "environments", "id=?", (env_id,))
        env = envs[0] if envs else {"id": env_id, "project_id": 0, "name": env_name}
        # 查询 service name → service_id（按项目精确匹配）
        service_id = None
        svcs = dbm.list_records(self._db_path, "services",
                                "name=? AND project_id=?",
                                (service, env["project_id"]))
        if svcs:
            service_id = svcs[0]["id"]
        keyword = matched_keyword or rule.get("metric") or "log"
        target = f"log:{env_id}:{service}:{keyword}"
        # scope 检查
        if not self._in_scope(rule, env, service=service, service_id=service_id):
            return
        pcode_map = self._project_code_map()
        target_info = {"project": pcode_map.get(env["project_id"], ""), "env": env_name, "service": service,
                       "keyword": keyword, "context": context[:5]}
        if entry_id is not None:
            target_info["entry_id"] = entry_id
        if hit_ts:
            target_info["hit_ts"] = hit_ts
        self._fire(rule, target,
                   subject=f"日志告警 [{keyword}]: {service}",
                   description=f"{env_name}/{service} 日志命中关键词 {keyword}:\n{line}",
                   target_info=target_info)


_engine = None


def get_engine() -> "AlertEngine":
    global _engine
    return _engine


def init_engine(app) -> None:
    """启动告警引擎（依赖 monitor 实例；monitor 未启动时引擎不启动）"""
    global _engine
    from .monitor import get_monitor
    mon = get_monitor()
    if mon is None:
        return
    if _engine is None:
        _engine = AlertEngine(app.config["DB_PATH"],
                              app.config.get("MONITOR_INTERVAL", 30),
                              monitor=mon)
    _engine.start()
