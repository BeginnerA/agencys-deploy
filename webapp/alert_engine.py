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
    dict(name="日志错误关键词", metric_type="log_keyword", metric="",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P3",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则（阶段三联动启用）"),
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
        self._last_task_scan = 0.0
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

    def _evaluate_metric_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "metric"]
        if not rules or not self._monitor:
            return
        # 1) 数据源：monitor 探测缓存 host_metrics → 灌入滑动窗口（按 metric 去重：
        #    同 metric 多条规则不重复灌入，保证滑动窗口一轮一条，持续时间判定不失真）
        metrics = {r.get("metric") or "cpu" for r in rules} & {"cpu", "mem", "disk", "load"}
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for host, m in (data.get("host_metrics") or {}).items():
                for metric in metrics:
                    self._push_history((env["id"], host, metric), m.get(metric))
        # 2) 评估滑动窗口（历史驱动：连续满足轮数 × interval ≥ duration 才触发）
        env_map = {e["id"]: e for e in dbm.list_records(self._db_path, "environments")}
        for (env_id, host, metric) in list(self._history):
            env = env_map.get(env_id)
            if env is None:
                continue
            key = (env_id, host, metric)
            value = self._history[key][-1][1]
            for rule in rules:
                if (rule.get("metric") or "cpu") != metric:
                    continue
                if not self._duration_hit(rule, key):
                    continue
                if not self._in_scope(rule, env, host=host):
                    continue
                self._fire(rule, f"host:{env_id}:{host}",
                           subject=f"{host} {metric.upper()} 过高",
                           description=f"当前 {value}%，阈值 {rule['compare']}{rule['threshold']}%，"
                                       f"持续 {rule['duration_seconds']}s",
                           target_info={"env": env["name"], "host": host})

    # ---------- 状态规则 ----------

    def _evaluate_status_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "status"]
        if not rules or not self._monitor:
            return
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for name, st in (data.get("services") or {}).items():
                for rule in rules:
                    metric = rule.get("metric", "service_down")
                    if metric == "service_down" and st.get("status") == "not_deployed":
                        if not self._in_scope(rule, env, service=name):
                            continue
                        self._fire(rule, f"service:{env['id']}:{name}",
                                   subject=f"服务下线: {name}",
                                   description=f"服务 {name} 在环境 {env['name']} 未部署或已停止",
                                   target_info={"env": env["name"], "service": name})

    def _check_tasks(self):
        """扫描最近失败任务（增量：finished_at > 上次扫描点）触发任务失败告警"""
        now = time.time()
        rows = dbm.list_records(self._db_path, "tasks", "status='failed'",
                                (), "finished_at DESC")[:20]
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
                       target_info={"env": t.get("env", ""), "task_id": t["id"]})
        self._last_task_scan = now

    def _task_fail_rule(self):
        rows = [r for r in dbm.list_records(self._db_path, "alert_rules")
                if r.get("enabled") and r.get("metric") == "task_failed"]
        return rows[0] if rows else None

    # ---------- 触发与收敛 ----------

    def _fire(self, rule: dict, target: str, subject: str,
              description: str, target_info: dict) -> int:
        """创建或更新告警（收敛）；新告警首次触发时通知（静默窗口内不通知）"""
        fp = f"{rule['id']}:{target}"
        rows = dbm.list_records(self._db_path, "alerts",
                                "fingerprint=? AND status != 'resolved'", (fp,))
        now = int(time.time())
        if rows:
            a = rows[0]
            # 静默期首次触发的告警：窗口已过期、仍在 firing 且从未通知 → 补发一次
            if a["notify_count"] == 0 and not self._silenced(rule, target_info):
                alert = dbm.get_record(self._db_path, "alerts", a["id"])
                send_alert(self._db_path, alert, rule.get("channels") or [])
                dbm.update_record(self._db_path, "alerts", a["id"],
                                  {"notify_count": 1, "last_seen_at": now})
            else:
                dbm.update_record(self._db_path, "alerts", a["id"],
                                  {"last_seen_at": now})
            return a["id"]
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
            h, _, m = str(s).partition(":")
            return int(h) * 60 + int(m)

        cur = lt.tm_hour * 60 + lt.tm_min
        s_min = to_min(w.get("start_at", "00:00"))
        e_min = to_min(w.get("end_at", "23:59"))
        if s_min <= e_min:
            return s_min <= cur <= e_min
        return cur >= s_min or cur <= e_min  # 跨午夜

    # ---------- 作用域 ----------

    def _in_scope(self, rule: dict, env: dict, host: str = None,
                  service: str = None) -> bool:
        """规则作用域：all/project/env/server/service；ids 为对应 id/名称列表"""
        scope = rule.get("scope") or {"type": "all", "ids": []}
        t, ids = scope.get("type", "all"), scope.get("ids") or []
        if t == "all":
            return True
        if t == "project":
            return env["project_id"] in ids
        if t == "env":
            return env["id"] in ids
        if t == "server":
            return host in ids
        if t == "service":
            return service in ids
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

    # ---------- 供日志联动调用（阶段三） ----------

    def trigger_log_alert(self, rule: dict, service: str, env_name: str,
                          line: str, context: list, env_id: int):
        """日志关键词命中：创建/更新日志告警（指纹含关键词，收敛由部分唯一索引保证）"""
        keyword = rule.get("log_keyword") or "log"
        target = f"log:{env_id}:{service}:{keyword}"
        self._fire(rule, target,
                   subject=f"日志告警 [{keyword}]: {service}",
                   description=f"{env_name}/{service} 日志命中关键词 {keyword}:\n{line}",
                   target_info={"env": env_name, "service": service,
                                "keyword": keyword, "context": context[:5]})


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
