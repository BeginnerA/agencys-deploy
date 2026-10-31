"""WAR 监控与日志采集观测测试：归属/维护/操作状态、版本权威与单元派生口径"""
import os
import tempfile
import unittest
from unittest import mock

from deploy_strategies import tomcat_instance as ti
from webapp import db as dbm
from webapp.log_collector import LogCollector
from webapp.monitor import MonitorScheduler

WAR_CFG = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}


def _raw(active="active"):
    return {"load_state": "loaded", "active_state": active,
            "sub_state": "running" if active == "active" else "dead",
            "pid": 4242 if active == "active" else 0, "restarts": 1,
            "active_enter_mono": 9600_000_000.0, "version": "1.0.0",
            "link_version": "", "cpu_ticks": 1000, "rss_pages": 25600}


class WarObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
            deploy="{}", monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="orders", image_name="orders",
            service_name="orders", deploy_mode="war_tomcat",
            runtime_config=dict(WAR_CFG), docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, self.env, srv)
        self.iid = "a" * 32

    def _fake_probe(self, host, port, ssh_cfg, service_names, timeout=10,
                    skip_extended=False, units=None):
        return {"ssh_ok": True, "containers": {}, "containers_stopped": {},
                "container_list": [], "container_status": {}, "resources": {},
                "host_metrics": {}, "extended": {}, "error": "",
                "jar_probe": {"uptime": 10000.0, "clk_tck": 100,
                              "units": {"orders-war": _raw()}}}

    def _evidence(self, instance_id=None, maintenance=False, owner=None,
                  phase=""):
        manifest = {"instance_id": instance_id or self.iid,
                    "project_id": self.pid, "env": "prod", "service": "orders",
                    "maintenance": {"active": maintenance}}
        # 平台侧期望的实例身份固定为 setUp 派生的 iid；manifest.instance_id
        # 可传 "other" 模拟「归属文件声称的是别台机器的实例」→ owner_mismatch
        return {"orders": {"instance_id": self.iid,
                           "manifest": manifest, "owner": owner or {},
                           "phase": phase}}

    def _cycle(self, evidence, health_ok=True):
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_war_evidence",
                           return_value=evidence), \
                mock.patch("webapp.monitor._probe_war_health",
                           return_value=("1.2.3.4", [
                               ("orders", {"ok": health_ok, "ms": 5,
                                           "method": "http_strict"})])), \
                mock.patch("webapp.db.save_response_time"):
            sched = MonitorScheduler(self.db, 30)
            sched._cycle()
        return sched.get_overview(self.pid, "prod")

    def test_war_unit_and_unmanaged_status(self):
        ov = self._cycle({})
        st = ov["services"]["orders"]
        # 进程在跑、应用健康，但未接入归属 → 不能显示为 online
        self.assertEqual(st["status"], "unmanaged")
        self.assertEqual(st["app_health"], "ok")
        self.assertEqual(st["unit"], "orders-war")
        self.assertEqual(st["config_status"], "unmanaged")
        self.assertEqual(st["operation_state"], "idle")
        self.assertEqual(st["process_status"], "online")
        self.assertEqual(st["recovery_status"], "not_needed")
        self.assertEqual(st["hosts"][0]["app_health"], "ok")
        self.assertEqual(st["hosts"][0]["process_status"], "online")
        self.assertEqual(st["hosts"][0]["recovery_status"], "not_needed")
        self.assertTrue(ov["health_checks"]["orders"]["ok"])

    def test_recovery_status_comes_from_last_result(self):
        ev = self._evidence()
        ev["orders"]["last_result"] = {"recovery_status": "succeeded"}
        st = self._cycle(ev)["services"]["orders"]
        self.assertEqual(st["recovery_status"], "succeeded")
        self.assertEqual(st["hosts"][0]["recovery_status"], "succeeded")
        # 未知不能等同于成功
        ev["orders"]["last_result"] = {}
        st = self._cycle(ev)["services"]["orders"]
        self.assertEqual(st["recovery_status"], "unknown")
        self.assertNotEqual(st["recovery_status"], "succeeded")

    def test_war_version_uses_current_link_and_reports_drift(self):
        ev = self._evidence()
        ev["orders"].update({"current_version": "2.0.0",
                             "deploy_version": "1.0.0",
                             "version_drift": True})
        row = self._cycle(ev)["services"]["orders"]["hosts"][0]
        # CURRENT 是权威；.deploy_version 落后时只报漂移，不静默采用旧值
        self.assertEqual(row["version"], "2.0.0")
        self.assertTrue(row["version_drift"])

        ev["orders"].update({"current_version": "2.0.0",
                             "deploy_version": "2.0.0",
                             "version_drift": False})
        row = self._cycle(ev)["services"]["orders"]["hosts"][0]
        self.assertEqual(row["version"], "2.0.0")
        self.assertFalse(row["version_drift"])

    def test_maintenance_and_in_progress(self):
        ov = self._cycle(self._evidence(maintenance=True,
                                        owner={"token": "t"},
                                        phase="health"))
        st = ov["services"]["orders"]
        self.assertEqual(st["config_status"], "maintenance")
        self.assertEqual(st["operation_state"], "in_progress")
        self.assertEqual(st["operation_phase"], "health")
        self.assertEqual(st["hosts"][0]["operation_phase"], "health")

    def test_idle_has_no_operation_phase(self):
        st = self._cycle(self._evidence(phase="health"))["services"]["orders"]
        self.assertEqual(st["operation_state"], "idle")
        self.assertEqual(st["operation_phase"], "")

    def test_owner_mismatch_is_not_managed(self):
        ov = self._cycle(self._evidence(instance_id="other"))
        self.assertEqual(ov["services"]["orders"]["config_status"],
                         "owner_mismatch")

    def test_war_health_failure_is_recorded(self):
        ov = self._cycle(self._evidence(), health_ok=False)
        self.assertNotIn("orders", ov["health_checks"])
        st = ov["services"]["orders"]
        self.assertEqual(st["status"], "unhealthy")
        self.assertEqual(st["app_health"], "failed")
        # 进程仍在运行，只是应用健康失败：process_status 保持 online
        self.assertEqual(st["process_status"], "online")
        self.assertEqual(st["hosts"][0]["app_health"], "failed")
        self.assertEqual(st["hosts"][0]["process_status"], "online")
        self.assertEqual(st["hosts"][0]["status"], "unhealthy")

    def test_config_status_precedence(self):
        self.assertEqual(ti.config_status({}, "i", 1, "prod", "orders"),
                         "unmanaged")
        self.assertEqual(ti.aggregate_config_status([
            {"config_status": "managed"},
            {"config_status": "owner_mismatch"},
            {"config_status": "maintenance"}]), "owner_mismatch")
        self.assertEqual(ti.aggregate_config_status([
            {"config_status": "managed"}, {"config_status": "maintenance"}]),
            "maintenance")

    def test_log_collector_derives_war_unit(self):
        collector = LogCollector(self.db, os.path.join(self.tmp, "logs"),
                                 ssh_func=lambda *a, **k: "")
        data = collector._load_process_services()
        self.assertEqual(data[self.pid][0]["unit"], "orders-war")

    def test_default_war_unit_without_unit_name(self):
        # 未显式配置 unit_name 时，监控与日志都必须按 WAR 规则得
        # smartops-{service}-war；漏传 deploy_mode 会退回服务名并长期误报
        cfg = dict(WAR_CFG)
        cfg.pop("unit_name")
        rec = dbm.get_service_by_name(self.db, self.pid, "orders")
        dbm.update_record(self.db, "services", rec["id"],
                          {"runtime_config": cfg})
        collector = LogCollector(self.db, os.path.join(self.tmp, "logs"),
                                 ssh_func=lambda *a, **k: "")
        data = collector._load_process_services()
        self.assertEqual(data[self.pid][0]["unit"], "smartops-orders-war")

        orig = self._fake_probe

        def fake(host, port, ssh_cfg, service_names, timeout=10,
                 skip_extended=False, units=None):
            r = orig(host, port, ssh_cfg, service_names, timeout,
                     skip_extended, units)
            r["jar_probe"]["units"] = {"smartops-orders-war": _raw()}
            return r

        # 归属证据给空 dict：与 test_war_unit_and_unmanaged_status 同构的
        # 「未接入归属」场景，归属聚合后服务按 unmanaged 降级展示
        with mock.patch("webapp.monitor._probe_server_full", side_effect=fake), \
                mock.patch("webapp.monitor._probe_war_evidence",
                           return_value={}), \
                mock.patch("webapp.monitor._probe_war_health",
                           return_value=("1.2.3.4", [
                               ("orders", {"ok": True, "ms": 5,
                                           "method": "http_strict"})])), \
                mock.patch("webapp.db.save_response_time"):
            sched = MonitorScheduler(self.db, 30)
            sched._cycle()
        ov = sched.get_overview(self.pid, "prod")
        self.assertEqual(ov["services"]["orders"]["unit"],
                         "smartops-orders-war")
        self.assertEqual(ov["services"]["orders"]["app_health"], "ok")
        self.assertEqual(ov["services"]["orders"]["status"], "unmanaged")
