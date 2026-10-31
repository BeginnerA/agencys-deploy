"""WAR 严格重启任务测试：执行器分流 + RestartContext 摘挂流 + 路由结束维护标记"""
import os
import tempfile
import unittest
from unittest import mock

from deploy_strategies.tomcat_lifecycle import HostResult
from deploy_strategies.tomcat_restart import RestartContext
from webapp import db as dbm

WAR_CFG = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}


def _make_war_svc(db, pid):
    return dbm.create_record(db, "services", dict(
        project_id=pid, name="orders", image_name="orders", service_name="orders",
        deploy_mode="war_tomcat", runtime_config=dict(WAR_CFG),
        docker_context="", dockerfile="", build_order="[]", nacos_groups="[]",
        dubbo_services="[]", snailjob_group="", health_url="", health_port=0,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


class FakeServer:
    def __init__(self, host, ssh_cfg):
        self.host = host
        self.ssh_cfg = ssh_cfg

    def close(self):
        pass


class WarRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.svc = _make_war_svc(self.db, self.pid)
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh='{"user":"root"}',
            build="{}", deploy="{}", monitor_enabled=1))
        for i, host in enumerate(["10.0.0.1", "10.0.0.2"]):
            sid = dbm.create_record(self.db, "servers", dict(
                host=host, ssh_port=22, ssh_user="", ssh_password="",
                ssh_key_file="", nacos_ip="", remark="", enabled=1))
            dbm.create_record(self.db, "env_servers", dict(
                env_id=self.env, server_id=sid, order_no=i))
        self.log = os.path.join(self.tmp, "restart.log")
        from webapp.task_manager import _RestartHandle
        self.handle = _RestartHandle()
        self.task = dict(id=1, project_id=self.pid, ref_id=self.svc,
                         env="prod", type="restart")

    def _run(self):
        from webapp.task_manager import _run_restart
        return _run_restart(self.db, self.task, self.log, self.handle)

    def _result(self, ok=True):
        return HostResult(ok, "restarted" if ok else "failed",
                          "done" if ok else "restart", "1.0.0", "",
                          "running" if ok else "stopped",
                          "healthy" if ok else "unhealthy", "not_needed",
                          "restored" if ok else "offline", "released",
                          "" if ok else "健康检查超时")

    def test_success_uses_strict_lifecycle(self):
        fake = mock.Mock()
        fake.restart.return_value = self._result(True)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer), \
                mock.patch("webapp.monitor._ssh_exec_multi") as generic:
            status = self._run()
        self.assertEqual(status, "success")
        self.assertEqual(fake.restart.call_count, 2)
        generic.assert_not_called()
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("2 台成功", text)
        self.assertIn("应用健康", text)

    def test_release_maintenance_flag_is_propagated(self):
        self.task["extra"] = ["--release-maintenance"]
        fake = mock.Mock()
        fake.restart.return_value = self._result(True)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "success")
        self.assertTrue(fake.restart.call_args.kwargs["release_maintenance"])

    def test_health_timeout_is_task_failure(self):
        fake = mock.Mock()
        fake.restart.return_value = self._result(False)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("应用=unhealthy", text)
        self.assertIn("健康检查超时", text)
        self.assertNotIn("仍视为成功", text)

    def test_lock_unknown_is_not_success(self):
        fake = mock.Mock()
        fake.restart.return_value = HostResult(
            False, "unknown", "restart_stop", "1.0.0", "",
            "unknown", "unknown", "not_needed", "unknown", "held",
            "停止失败: timeout")
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log, encoding="utf-8") as f:
            self.assertIn("锁=held", f.read())


class RestartContextTest(unittest.TestCase):
    def test_offline_and_online_use_db_nacos_and_service_groups(self):
        with mock.patch("deploy_strategies.tomcat_restart.NacosClient") as nc:
            client = nc.return_value
            client.configured = True
            client.group = "DEFAULT_GROUP"
            client.online_ip.return_value = True
            ctx = RestartContext(
                {"name": "prod", "nacos": {"url": "http://nacos"}},
                {"name": "orders", "service_name": "orders-svc",
                 "nacos_groups": '["G1"]'})
        nc.assert_called_once_with(config={"nacos": {"url": "http://nacos"}})
        ctx.nacos_offline("10.0.0.1")
        ctx.nacos_online("10.0.0.1", 1.0)
        client.offline_ip.assert_called_once_with(
            "orders-svc", "10.0.0.1", group="G1")
        client.wait_for_absence.assert_called_once()
        client.online_ip.assert_called_once_with(
            "orders-svc", "10.0.0.1", 1.0, group="G1")
        client.wait_for_healthy.assert_called_once()
        self.assertEqual(ctx._nacos_ip(
            {"host": "10.0.0.1", "nacos_ip": "10.1.1.1"}), "10.1.1.1")
        self.assertEqual(ctx._nacos_ip({"host": "10.0.0.1"}), "10.0.0.1")

    def test_capture_and_restore_preserve_weight_and_skip_disabled(self):
        with mock.patch("deploy_strategies.tomcat_restart.NacosClient") as nc:
            client = nc.return_value
            client.configured = True
            client.group = "DEFAULT_GROUP"
            client.list_instances.return_value = [
                {"ip": "10.0.0.1", "port": 8080, "enabled": True,
                 "weight": 7, "clusterName": "D1", "ephemeral": True},
                {"ip": "10.0.0.1", "port": 8081, "enabled": False,
                 "weight": 0, "clusterName": "D1", "ephemeral": True},
                {"ip": "10.0.0.2", "port": 8080, "enabled": True,
                 "weight": 3, "clusterName": "D1", "ephemeral": True},
            ]
            ctx = RestartContext({"name": "prod", "nacos": {}},
                                 {"name": "orders", "nacos_groups": "[]"})
            snap = ctx.capture_traffic("10.0.0.1")
            self.assertEqual(len(snap["orders|DEFAULT_GROUP"]), 2)
            ctx.restore_traffic("10.0.0.1", snap)
        client.set_instance.assert_called_once_with(
            "orders", "10.0.0.1", 8080, enabled=True, weight=7.0,
            cluster="D1", group="DEFAULT_GROUP", ephemeral=True)
        client.wait_for_healthy.assert_called_once()


class RestartReleaseMaintenanceApiTest(unittest.TestCase):
    """POST /api/tasks type=restart：release_maintenance 布尔校验与 extra 标记

    前端「结束维护并重启」显式勾选后传 true；路由为 true 时向 extra 追加
    --release-maintenance（仅 WAR 执行器消费，旧模式忽略），非布尔拒绝 400。
    """

    def setUp(self):
        import time
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        # 目录靠 create_app 注入：裸调用会让工厂在默认落点上跑一遍迁移/引导
        self.app = create_app(data_dir=self.tmp)
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        with self.app.app_context():
            now = int(time.time())
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="p1", code="p1", description="", git_org="",
                created_at=now))
            self.svc_id = _make_war_svc(self.db_path, self.proj_id)
            dbm.create_record(self.db_path, "environments", dict(
                project_id=self.proj_id, name="prod", is_default=1,
                registry="{}", nacos="{}", mysql_snailjob="{}",
                ssh='{"user":"root"}', build="{}", deploy="{}",
                monitor_enabled=1))
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=now))
        self.client = self.app.test_client()
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_release_maintenance_true_appends_flag(self):
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod",
                "release_maintenance": True})
        self.assertEqual(resp.status_code, 201)
        submit.assert_called_once()
        _, _, extra = submit.call_args.args  # (db_path, task, extra)
        self.assertIn("--release-maintenance", extra)
        task = dbm.get_task(self.db_path, resp.get_json()["id"])
        self.assertIn("--release-maintenance", task["extra"])

    def test_release_maintenance_default_absent(self):
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 201)
        _, _, extra = submit.call_args.args
        self.assertEqual(extra, [])

    def test_release_maintenance_non_bool_rejected(self):
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "prod",
            "release_maintenance": "yes"})
        self.assertEqual(resp.status_code, 400)
