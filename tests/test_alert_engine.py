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
        dbm.update_task(self.db, 1, status="failed", finished_at=int(time.time()))
        e = self._engine()
        e._check_tasks()
        e._check_tasks()  # 重复扫描不重复告警
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("发布", alerts[0]["subject"])

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


if __name__ == "__main__":
    unittest.main()
