"""监控主机探测：jar 单元命令并入同一 SSH 批次 + 环境作业收集单元"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.monitor import MonitorScheduler, _probe_server_full


class ProbeServerFullUnitsTest(unittest.TestCase):
    def test_units_use_third_command(self):
        calls = []

        def fake_multi(host, port, ssh_cfg, cmds, timeout=10):
            calls.append(list(cmds))
            return ["svc|Up 1 hour|img\n@@STATS@@\n", "cpu 1 2 3 4\n", (
                "UPTIME:100.00 0.00\nCLK:100\n"
                "@@JARUNIT:tok:0\n"
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                "MainPID=42\nNRestarts=0\nActiveEnterTimestampMonotonic=0\n"
                "VER:1.0.0\nLINK:/opt/auth/releases/1.0.0\n")]

        with mock.patch("webapp.monitor._ssh_exec_multi", side_effect=fake_multi), \
                mock.patch("webapp.jar_probe.secrets.token_hex",
                           return_value="tok"):
            res = _probe_server_full("1.2.3.4", 22, {}, set(),
                                     skip_extended=True,
                                     units={"auth": {"app_home": "/opt/auth"}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]), 3)
        self.assertIn("systemctl show", calls[0][2])
        self.assertEqual(res["jar_probe"]["units"]["auth"]["version"], "1.0.0")
        self.assertEqual(res["jar_probe"]["units"]["auth"]["pid"], 42)

    def test_no_units_keeps_two_commands(self):
        calls = []

        def fake_multi(host, port, ssh_cfg, cmds, timeout=10):
            calls.append(list(cmds))
            return ["", "cpu 1 2 3 4\n"]

        with mock.patch("webapp.monitor._ssh_exec_multi", side_effect=fake_multi):
            res = _probe_server_full("1.2.3.4", 22, {}, set(), skip_extended=True)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(res["jar_probe"], {})


class EnvJobsCollectUnitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_host_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}", deploy="{}",
            monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, self.env, srv)
        self.seen = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_jar_units_go_to_host_job(self):
        def fake_probe(host, port, ssh_cfg, service_names, timeout=10,
                       skip_extended=False, units=None):
            self.seen["units"] = units
            return {"ssh_ok": False, "containers": {}, "containers_stopped": {},
                    "container_list": [], "container_status": {},
                    "resources": {}, "host_metrics": {}, "extended": {},
                    "jar_probe": {}, "error": "down"}

        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=fake_probe):
            sched._cycle()
        self.assertEqual(self.seen["units"], {"auth": {"app_home": "/opt/auth"}})


if __name__ == "__main__":
    unittest.main()
