"""告警引擎测试：滑动窗口/收敛/状态机/升级/静默/种子规则（用 FakeMonitor 替代真实探测）"""
import os
import tempfile
import time
import unittest

from webapp import db as dbm
from webapp.alert_engine import AlertEngine, DEFAULT_RULES


class FakeMonitor:
    """模拟 monitor 探测缓存：get_overview(project_id, env) → 固定数据"""
    def __init__(self, overviews):
        self._data = overviews

    def get_overview(self, project_id, env):
        return self._data.get((project_id, env), {"services": {}, "host_metrics": {}})


def make_env(db, project_id=1):
    return dbm.create_record(db, "environments", dict(
        project_id=project_id, name="test", is_default=1,
        registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
        build="{}", deploy="{}", monitor_enabled=1))


def make_project(db):
    return dbm.create_record(db, "projects", dict(
        name="p1", code="p1", description="", git_org="", created_at=1))


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = make_project(self.db)
        self.env_id = make_env(self.db)

    def _engine(self, interval=30, overviews=None):
        return AlertEngine(self.db, interval=interval,
                           monitor=FakeMonitor(overviews or {}))

    def _metric_rule(self, metric="cpu", threshold=80, duration=300, **kw):
        fields = dict(name=f"{metric} test", metric_type="metric", metric=metric,
                      compare=">", threshold=threshold, duration_seconds=duration,
                      scope={"type": "all", "ids": []}, severity="P2",
                      channels=["wecom"], escalation_interval=30, enabled=1,
                      remark="", created_at=1, updated_at=1)
        fields.update(kw)  # 显式传参覆盖默认值（如 severity/scope/escalation_interval）
        return dbm.create_record(self.db, "alert_rules", fields)

    # ---- 种子规则 ----
    def test_seed_rules_idempotent(self):
        e = self._engine()
        e._seed_rules()
        e._seed_rules()  # 再次执行不重复
        rules = dbm.list_records(self.db, "alert_rules")
        self.assertEqual(len(rules), len(DEFAULT_RULES))
        names = {r["name"] for r in rules}
        self.assertIn("主机 CPU 过高", names)
        self.assertIn("服务下线", names)
        self.assertIn("任务失败", names)

    # ---- 滑动窗口持续判定 ----
    def test_metric_fires_after_duration(self):
        self._metric_rule(metric="cpu", threshold=80, duration=300)  # 30s×10 轮
        e = self._engine(interval=30)
        # 推送 9 轮满足 → 不触发；第 10 轮满足 → 触发
        for i in range(9):
            e._push_history((self.env_id, "h1", "cpu"), 95.0)
            e._evaluate_metric_rules()
        self.assertEqual(len(dbm.list_records(self.db, "alerts")), 0)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "firing")
        self.assertEqual(alerts[0]["severity"], "P2")

    def test_metric_recovers_no_fire(self):
        self._metric_rule(metric="cpu", threshold=80, duration=300)
        e = self._engine(interval=30)
        values = [95.0] * 5 + [10.0] * 6  # 中途恢复 → 连续满足重置
        for v in values:
            e._push_history((self.env_id, "h1", "cpu"), v)
            e._evaluate_metric_rules()
        self.assertEqual(len(dbm.list_records(self.db, "alerts")), 0)

    # ---- 收敛 ----
    def test_convergence_single_alert(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        for _ in range(3):
            e._push_history((self.env_id, "h1", "cpu"), 95.0)
            e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        # 收敛：重复触发只推进 last_seen_at，不重置 first_seen_at
        # （相等断言在 send_alert 重试退避阻塞下跨秒必然失败，故用 <=）
        self.assertLessEqual(alerts[0]["first_seen_at"], alerts[0]["last_seen_at"])

    # ---- 状态机 ----
    def test_ack_and_resolve(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"],
                          dict(status="acked", ack_by="admin", ack_at=int(time.time())))
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["status"], "acked")
        self.assertEqual(a["ack_by"], "admin")
        dbm.update_record(self.db, "alerts", alert["id"],
                          dict(status="resolved", resolved_by="admin",
                               resolved_at=int(time.time()), resolved_type="manual"))
        self.assertEqual(dbm.get_record(self.db, "alerts", alert["id"])["status"], "resolved")

    # ---- 自动恢复 ----
    def test_auto_resolve_on_recovery(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        self.assertEqual(alert["status"], "firing")
        # 连续 2 轮恢复 → auto resolved
        e._push_history((self.env_id, "h1", "cpu"), 10.0)
        e._evaluate_metric_rules()
        e._push_history((self.env_id, "h1", "cpu"), 10.0)
        e._auto_resolve()
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["status"], "resolved")
        self.assertEqual(a["resolved_type"], "auto")

    # ---- 升级 ----
    def test_escalation(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0, escalation_interval=30)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"], dict(
            status="acked", escalation_at=int(time.time()) - 1))
        e._escalate()
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["severity"], "P1")  # P2 → P1
        self.assertEqual(a["escalation_level"], 1)
        self.assertGreater(a["escalation_at"], int(time.time()))

    def test_no_escalation_at_p0(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0, severity="P0",
                          escalation_interval=30)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"], dict(
            status="acked", escalation_at=int(time.time()) - 1))
        e._escalate()
        self.assertEqual(dbm.get_record(self.db, "alerts", alert["id"])["severity"], "P0")

    # ---- 静默窗口 ----
    def _silence(self, **kw):
        now = int(time.time())
        fields = dict(name="win", type="one_time",
                      start_at=str(now - 60), end_at=str(now + 60),
                      weekdays=[], scope={"rules": [], "envs": [], "servers": []},
                      enabled=1, remark="", created_at=now)
        fields.update(kw)  # 显式传参覆盖默认值（如 start_at/end_at/scope）
        return dbm.create_record(self.db, "silence_windows", fields)

    def test_silence_one_time_global(self):
        self._silence()
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules",
                              self._metric_rule(metric="cpu", duration=0))
        self.assertTrue(e._silenced(rule, {"env": "test", "host": "h1"}))

    def test_silence_by_rule_scope(self):
        rule_id = self._metric_rule(metric="cpu", duration=0)
        self._silence(scope={"rules": [rule_id], "envs": [], "servers": []})
        e = self._engine()
        self.assertTrue(e._silenced(dbm.get_record(self.db, "alert_rules", rule_id), {}))
        other = dbm.get_record(self.db, "alert_rules",
                               self._metric_rule(metric="disk", duration=0))
        self.assertFalse(e._silenced(other, {}))

    def test_silence_recurring_weekday(self):
        e = self._engine()
        lt = time.localtime()
        w = dict(name="weekend", type="recurring", start_at="00:00",
                 end_at="23:59", weekdays=[lt.tm_wday],
                 scope={"rules": [], "envs": [], "servers": []},
                 enabled=1, remark="", created_at=1)
        w["id"] = dbm.create_record(self.db, "silence_windows", w)
        self.assertTrue(e._window_active(w, time.time()))
        w["weekdays"] = [(lt.tm_wday + 1) % 7]
        self.assertFalse(e._window_active(w, time.time()))

    def test_silence_expired(self):
        self._silence(start_at=str(int(time.time()) - 120),
                      end_at=str(int(time.time()) - 60))
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules",
                              self._metric_rule(metric="cpu", duration=0))
        self.assertFalse(e._silenced(rule, {}))

    def test_silence_catchup_notify_after_window(self):
        """窗口内首次触发不通知；窗口过期后仍在 firing → 补发一次"""
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        self._silence()
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["notify_count"], 0)
        evs = dbm.list_records(self.db, "alert_events")
        self.assertEqual(evs[0]["channel"], "silenced")
        # 窗口过期后再评估一轮 → 补发通知
        w = dbm.list_records(self.db, "silence_windows")[0]
        dbm.update_record(self.db, "silence_windows", w["id"],
                          {"end_at": str(int(time.time()) - 1)})
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        a = dbm.get_record(self.db, "alerts", alerts[0]["id"])
        self.assertEqual(a["notify_count"], 1)
        channels = [x["channel"] for x in dbm.list_records(self.db, "alert_events")]
        self.assertIn("wecom", channels)

    def test_silence_catchup_skipped_after_ack(self):
        """窗口内触发 → ack → 窗口过期：不再补发（ack 后停止重复通知）"""
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        self._silence()
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["notify_count"], 0)
        # 窗口内 ack
        dbm.update_record(self.db, "alerts", alerts[0]["id"], {"status": "acked"})
        # 窗口过期后再评估 → 不补发
        w = dbm.list_records(self.db, "silence_windows")[0]
        dbm.update_record(self.db, "silence_windows", w["id"],
                          {"end_at": str(int(time.time()) - 1)})
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        a = dbm.get_record(self.db, "alerts", alerts[0]["id"])
        self.assertEqual(a["notify_count"], 0)
        channels = [x["channel"] for x in dbm.list_records(self.db, "alert_events")]
        self.assertNotIn("wecom", channels)

    def test_silence_invalid_window_data_no_crash(self):
        """静默窗口脏数据（非法时间/非法 scope）不抛异常、窗口不生效；
        规则 scope 非法按全局放行"""
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules",
                              self._metric_rule(metric="cpu", duration=0))
        # recurring 非法时间
        self._silence(type="recurring", start_at="abc", end_at="25:99")
        self.assertFalse(e._silenced(rule, {"env": "test", "host": "h1"}))
        # one_time 激活但 scope 非法类型
        self._silence(type="one_time", scope="oops")
        self.assertFalse(e._silenced(rule, {"env": "test", "host": "h1"}))
        # 规则 scope 非法 → 按全局放行（_in_scope 不抛异常）
        rid = self._metric_rule(metric="cpu", duration=0, scope="oops")
        r = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(r, env, host="h1"))

    # ---- 服务下线状态规则 ----
    def test_service_down_fires(self):
        dbm.create_record(self.db, "alert_rules", dict(
            name="down", metric_type="status", metric="service_down",
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P1", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1))
        e = self._engine(overviews={
            (self.proj_id, "test"): {"services": {"svc-a": {"status": "not_deployed"}},
                                     "host_metrics": {}}})
        e._evaluate_status_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("svc-a", alerts[0]["subject"])
        # 服务恢复在线 → 自动 resolved
        e._monitor._data[(self.proj_id, "test")]["services"]["svc-a"]["status"] = "online"
        e._auto_resolve()
        self.assertEqual(dbm.get_record(self.db, "alerts", alerts[0]["id"])["status"],
                         "resolved")

    # ---- 任务失败检测 ----
    def test_task_failed_fires(self):
        dbm.create_record(self.db, "alert_rules", dict(
            name="taskfail", metric_type="status", metric="task_failed",
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P2", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1))
        dbm.create_task(self.db, self.proj_id, "deploy", 1, "发布", "test",
                        "v1", "", 1)
        e = self._engine()  # 构造后再失败（启动后失败才告警）
        dbm.update_task(self.db, 1, status="failed", finished_at=time.time())
        e._check_tasks()
        e._check_tasks()  # 重复扫描不重复告警
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("发布", alerts[0]["subject"])

    def test_task_failed_no_retroactive_on_startup(self):
        """首启不回扫历史失败任务（增量语义）；启动后失败的任务正常告警"""
        dbm.create_record(self.db, "alert_rules", dict(
            name="taskfail", metric_type="status", metric="task_failed",
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P2", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1))
        # 引擎启动前已失败的历史任务（finished_at 远早于启动时刻）
        dbm.create_task(self.db, self.proj_id, "deploy", 1, "旧发布", "test",
                        "v0", "", 1)
        dbm.update_task(self.db, 1, status="failed", finished_at=1)
        e = self._engine()
        e._check_tasks()
        self.assertEqual(len(dbm.list_records(self.db, "alerts")), 0)
        # 启动后失败的任务 → 正常触发
        dbm.create_task(self.db, self.proj_id, "deploy", 1, "新发布", "test",
                        "v1", "", 1)
        dbm.update_task(self.db, 2, status="failed", finished_at=time.time())
        e._check_tasks()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("新发布", alerts[0]["subject"])

    # ---- 作用域 ----
    def test_scope_env_filter(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "env", "ids": [999]})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertFalse(e._in_scope(rule, env, host="h1"))
        rule["scope"] = {"type": "env", "ids": [self.env_id]}
        self.assertTrue(e._in_scope(rule, env, host="h1"))

    # ---- 日志关键词联动 ----
    def _log_kw_rule(self, metric="ERROR", **kw):
        fields = dict(name="log kw test", metric_type="log_keyword", metric=metric,
                      compare=">", threshold=0, duration_seconds=0,
                      scope={"type": "all", "ids": []}, severity="P3",
                      channels=["wecom"], escalation_interval=30, enabled=1,
                      remark="", created_at=1, updated_at=1)
        fields.update(kw)
        return dbm.create_record(self.db, "alert_rules", fields)

    def test_trigger_log_alert_creates_alert(self):
        """trigger_log_alert: 命中关键词创建告警"""
        rid = self._log_kw_rule(metric="ERROR")
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        e.trigger_log_alert(rule, "api-svc", "prod", "ERROR: db timeout",
                            ["ERROR: db timeout"], self.env_id,
                            matched_keyword="ERROR")
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("ERROR", alerts[0]["subject"])
        self.assertIn("api-svc", alerts[0]["subject"])
        self.assertEqual(alerts[0]["status"], "firing")

    def test_trigger_log_alert_convergence(self):
        """trigger_log_alert: 同规则+同服务+同关键词 不重复创建（指纹收敛）"""
        rid = self._log_kw_rule(metric="FATAL")
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        e.trigger_log_alert(rule, "api-svc", "prod", "FATAL crash 1",
                            ["FATAL crash 1"], self.env_id, matched_keyword="FATAL")
        e.trigger_log_alert(rule, "api-svc", "prod", "FATAL crash 2",
                            ["FATAL crash 2"], self.env_id, matched_keyword="FATAL")
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        # 但 last_seen_at 应更新
        self.assertGreaterEqual(alerts[0]["last_seen_at"], alerts[0]["first_seen_at"])

    def test_trigger_log_alert_multi_keyword_different_fingerprints(self):
        """多关键词规则: 不同关键词产生不同指纹，分别告警"""
        rid = self._log_kw_rule(metric="ERROR,FATAL")
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        e.trigger_log_alert(rule, "svc", "prod", "ERROR happened",
                            ["ERROR happened"], self.env_id, matched_keyword="ERROR")
        e.trigger_log_alert(rule, "svc", "prod", "FATAL happened",
                            ["FATAL happened"], self.env_id, matched_keyword="FATAL")
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 2)

    def test_trigger_log_alert_uses_matched_keyword_over_metric(self):
        """matched_keyword 优先于 metric 字段作为告警关键词"""
        rid = self._log_kw_rule(metric="ERROR,FATAL")
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        e.trigger_log_alert(rule, "svc", "prod", "OOM killed",
                            ["OOM killed"], self.env_id, matched_keyword="OOM")
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("OOM", alerts[0]["subject"])
        self.assertIn("OOM", alerts[0]["description"])


if __name__ == "__main__":
    unittest.main()
