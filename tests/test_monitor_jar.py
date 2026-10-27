"""监控环境级 jar_service：进程视图、版本写穿、健康检查端口回退"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.monitor import MonitorScheduler


def _raw(active="active", pid=4242, cpu_ticks=1000, version="1.0.1"):
    return {"load_state": "loaded" if active != "not-found" else "not-found",
            "active_state": active, "sub_state": "running" if active == "active" else "dead",
            "pid": pid if active == "active" else 0, "restarts": 1,
            "active_enter_mono": 9600_000_000.0, "version": version,
            "link_version": "", "cpu_ticks": cpu_ticks, "rss_pages": 25600}


class MonitorJarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mon_jar_")
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
        self.cpu_ticks = 1000
        self.active = "active"
        self.version = "1.0.1"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_probe(self, host, port, ssh_cfg, service_names, timeout=10,
                    skip_extended=False, units=None):
        return {"ssh_ok": True, "containers": {}, "containers_stopped": {},
                "container_list": [], "container_status": {}, "resources": {},
                "host_metrics": {"cpu": 10.0, "mem": 20.0, "disk": 30.0, "load": 1.0},
                "extended": {}, "error": "",
                "jar_probe": {"uptime": 10000.0, "clk_tck": 100,
                              "units": {"auth": _raw(self.active, cpu_ticks=self.cpu_ticks,
                                                     version=self.version)}}}

    def test_online_view_and_single_write(self):
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]), \
                mock.patch("webapp.db.record_host_state_live") as live:
            sched._cycle()
            ov = sched.get_overview(self.pid, "prod")
            st = ov["services"]["auth"]
            self.assertEqual(st["status"], "online")
            self.assertEqual(st["version"], "1.0.1")
            self.assertEqual(st["unit"], "auth")
            self.assertEqual(st["online_hosts"], 1)
            self.assertEqual(len(st["hosts"]), 1)
            self.assertEqual(st["hosts"][0]["pid"], 4242)
            self.assertEqual(st["hosts"][0]["rss_mb"], 100.0)
            self.assertEqual(st["hosts"][0]["uptime_hours"], 0.11)
            self.assertEqual(st["instances"], 1)  # Nacos 不可用时回退在线主机数
            live.assert_called_once()
            args = live.call_args[0]
            self.assertEqual(args[1:], (self.pid, "prod", "auth", "1.2.3.4", "1.0.1"))

            # 第二轮版本不变 → 不再写库
            sched._cycle()
            self.assertEqual(live.call_count, 1)

    def test_second_round_computes_cpu(self):
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]):
            sched._cycle()
            self.cpu_ticks = 3000
            sched._cycle()
        host = sched.get_overview(self.pid, "prod")["services"]["auth"]["hosts"][0]
        self.assertIsNotNone(host["cpu"])
        self.assertGreater(host["cpu"], 0)

    def test_stopped_version_still_written(self):
        self.active = "failed"
        sched = MonitorScheduler(self.db, 30)
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_health_via_ssh", return_value=[]), \
                mock.patch("webapp.db.record_host_state_live") as live:
            sched._cycle()
        self.assertEqual(sched.get_overview(self.pid, "prod")["services"]["auth"]["status"],
                         "stopped")
        live.assert_called_once()


if __name__ == "__main__":
    unittest.main()
