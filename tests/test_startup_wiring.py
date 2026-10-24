"""后台服务启动编排：实时监控开关不得连带关闭发布/Agent/SSL/日志服务

背景：webapp/app.py 曾把所有守护线程都放在 `monitor_interval > 0` 分支里，
`--monitor-interval 0` 会同时停掉发布调度、AI 建议、SSL 探测与日志采集；
且命令行默认值硬编码 60，未遵循 DASHBOARD_MONITOR_INTERVAL。
"""
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeDeployScheduler:
    calls = []
    sink = None

    def __init__(self, db_path, interval=30):
        self.db_path = db_path
        self.interval = interval
        self.started = False

    def start(self):
        self.started = True
        _FakeDeployScheduler.calls.append((self.db_path, self.interval))
        if _FakeDeployScheduler.sink is not None:
            _FakeDeployScheduler.sink.append(("deploy-sched", self.interval))


class StartupWiringTest(unittest.TestCase):
    def setUp(self):
        _FakeDeployScheduler.calls = []
        self.calls = []
        _FakeDeployScheduler.sink = self.calls
        self.app = types.SimpleNamespace(config={
            "DB_PATH": "/tmp/platform-test.db",
            "MONITOR_INTERVAL": 30,
        })
        self._patches = [
            mock.patch("webapp.monitor.init_monitor",
                       side_effect=lambda app: self.calls.append(
                           ("monitor", app.config.get("MONITOR_INTERVAL")))),
            mock.patch("webapp.alert_engine.init_engine",
                       side_effect=lambda app: self.calls.append(
                           ("alert-engine", app.config.get("MONITOR_INTERVAL")))),
            mock.patch("webapp.ssl_monitor.init_ssl_monitor",
                       side_effect=lambda app: self.calls.append(("ssl-monitor", None))),
            mock.patch("webapp.log_collector.init_collector",
                       side_effect=lambda app: self.calls.append(("log-collector", None))),
            mock.patch("webapp.deploy_scheduler.DeployScheduler", _FakeDeployScheduler),
            mock.patch("agent.scheduler.start_scheduler",
                       side_effect=lambda db, interval: self.calls.append(
                           ("agent-sched", interval))),
        ]
        for p in self._patches:
            p.start()
        self.start = __import__("webapp.services", fromlist=["start_background_services"]) \
            .start_background_services

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def names(self):
        return [name for name, _ in self.calls]

    def test_monitor_zero_keeps_other_services_running(self):
        started = self.start(self.app, monitor_interval=0, agent_interval=30,
                             deploy_interval=25)
        self.assertNotIn("monitor", self.names())
        self.assertNotIn("alert-engine", self.names())
        self.assertIn("ssl-monitor", self.names())
        self.assertIn("log-collector", self.names())
        self.assertIn("deploy-sched", self.names())
        self.assertIn("agent-sched", self.names())
        self.assertEqual(_FakeDeployScheduler.calls, [("/tmp/platform-test.db", 25)])
        self.assertEqual(self.app.config["MONITOR_INTERVAL"], 0)
        self.assertIn("deploy-sched", started)

    def test_monitor_positive_starts_monitor_and_alert_engine(self):
        self.start(self.app, monitor_interval=15, agent_interval=0,
                   deploy_interval=30)
        self.assertIn(("monitor", 15), self.calls)
        self.assertIn(("alert-engine", 15), self.calls)
        self.assertNotIn("agent-sched", self.names())

    def test_defaults_follow_config_and_env(self):
        self.app.config["MONITOR_INTERVAL"] = 45
        with mock.patch.dict(os.environ, {
                "DASHBOARD_DEPLOY_INTERVAL": "70",
                "DASHBOARD_AGENT_INTERVAL": "11"}):
            self.start(self.app, monitor_interval=None, agent_interval=None,
                       deploy_interval=None)
        self.assertIn(("monitor", 45), self.calls)
        self.assertEqual(_FakeDeployScheduler.calls, [("/tmp/platform-test.db", 70)])
        self.assertIn(("agent-sched", 11), self.calls)

    def test_negative_monitor_interval_is_treated_as_disabled(self):
        self.start(self.app, monitor_interval=-5)
        self.assertNotIn("monitor", self.names())
        self.assertEqual(self.app.config["MONITOR_INTERVAL"], 0)


class LogCollectorIntervalTest(unittest.TestCase):
    def setUp(self):
        from webapp import log_collector
        self.mod = log_collector
        self._old = log_collector._collector
        log_collector._collector = None

    def tearDown(self):
        self.mod._collector = self._old

    def _init_with_monitor_interval(self, interval):
        app = types.SimpleNamespace(config={
            "DB_PATH": "/tmp/platform-test.db",
            "LOG_DIR": "/tmp/logs",
            "MONITOR_INTERVAL": interval,
        })
        with mock.patch.object(self.mod, "LogCollector") as lc, \
                mock.patch("webapp.monitor.get_monitor", return_value=None):
            self.mod.init_collector(app)
        return lc.call_args.kwargs.get("interval")

    def test_log_interval_stays_positive_when_monitor_disabled(self):
        self.assertEqual(self._init_with_monitor_interval(0), 120)

    def test_log_interval_is_double_monitor_when_enabled(self):
        self.assertEqual(self._init_with_monitor_interval(30), 60)


if __name__ == "__main__":
    unittest.main()
