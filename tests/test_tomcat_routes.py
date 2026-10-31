"""WAR 管理路由：预览/接入/受控更新/恢复的权限、摘要与 207 语义"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="war_routes_")

from webapp import create_app
from webapp import db as dbm

RUNTIME = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}
PLAN = {
    "digest": "a" * 64, "unit": "orders-war", "unit_content": "[Service]\n",
    "files": {"instance/conf/server.xml": "<Server/>"},
    "manifest": {"app_home": "/opt/apps/orders", "unit": "orders-war"},
    "deletes": ["instance/webapps/*"], "templates": ["server.xml"],
}


class TomcatRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(data_dir=Path(_TMP))
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.h = {"Authorization": f"Bearer {cls._login(cls.c, 'admin', 'test1234')}"}
        r = cls.c.post("/api/projects", json={"name": "war-test", "code": "wartest"},
                       headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.pid = r.get_json()["id"]
        r = cls.c.post(f"/api/projects/{cls.pid}/services", json={
            "name": "orders", "build_type": "maven", "deploy_mode": "war_tomcat",
            "image_name": "", "service_name": "", "runtime_config": dict(RUNTIME)},
            headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.sid = r.get_json()["id"]
        r = cls.c.post(f"/api/projects/{cls.pid}/environments",
                       json={"name": "prod"}, headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.env_id = r.get_json()["id"]
        for host in ("10.0.0.1", "10.0.0.2"):
            r = cls.c.post("/api/servers", json={
                "host": host, "ssh_port": 22, "ssh_user": "root"}, headers=cls.h)
            assert r.status_code == 201, r.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _url(self, tail):
        return f"/api/projects/{self.pid}/services/{self.sid}/tomcat/{tail}"

    def _hosts(self, digest="a" * 64, iid="i1"):
        return [{"host": h, "instance_id": iid, "digest": digest}
                for h in ("10.0.0.1", "10.0.0.2")]

    def test_preview_reuses_manifest_id_and_reports_partial(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "existing"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="managed"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           side_effect=[dict(PLAN), RuntimeError("模板读取失败")]):
            r = self.c.get(self._url("preview?env=prod"), headers=self.h)
        self.assertEqual(r.status_code, 207, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["hosts"][0]["instance_id"], "existing")
        self.assertEqual(len(data["failed"]), 1)
        self.assertNotIn("token", r.get_data(as_text=True))

    def test_onboard_requires_confirm_and_exact_hosts(self):
        r = self.c.post(self._url("onboard"),
                        json={"env": "prod", "hosts": self._hosts()},
                        headers=self.h)
        self.assertEqual(r.status_code, 400)
        r = self.c.post(self._url("onboard"),
                        json={"env": "prod", "confirm": True,
                              "hosts": self._hosts()[:1]}, headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_onboard_stale_digest_does_not_write(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN, digest="b" * 64)), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.onboard") as ob:
            r = self.c.post(self._url("onboard"), json={
                "env": "prod", "confirm": True, "hosts": self._hosts()},
                headers=self.h)
        self.assertEqual(r.status_code, 207, r.get_data(as_text=True))
        ob.assert_not_called()

    def test_onboard_writes_audit_and_never_starts(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN)), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.onboard") as ob:
            r = self.c.post(self._url("onboard"), json={
                "env": "prod", "confirm": True, "hosts": self._hosts()},
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(ob.call_count, 2)
        logs = dbm.list_audit_logs(self.app.config["DB_PATH"], limit=20)
        self.assertTrue(any(x["action"] == "war_tomcat_onboard" for x in logs))

    def test_config_update_is_one_owner_and_never_starts(self):
        class Ctx:
            def capture_traffic(self, host):
                return {"orders|G1": [{"port": 18080, "enabled": True,
                                       "weight": 5, "cluster": "D",
                                       "ephemeral": True}]}
            def nacos_offline(self, host):
                return None

        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "i1"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="managed"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.verify_instance",
                           return_value=[]), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN)), \
                mock.patch("webapp.routes.tomcat_routes._traffic_context",
                           return_value=Ctx()), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.acquire") as acq, \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.release") as rel, \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.maintenance_stop",
                           return_value={"active": True}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.apply_config") as ap:
            r = self.c.post(self._url("config"), json={
                "env": "prod", "action": "update", "confirm": True,
                "hosts": self._hosts()}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(acq.call_count, 2)
        self.assertEqual(ap.call_count, 2)
        self.assertFalse(r.get_json()["hosts"][0]["started"])
        self.assertEqual(rel.call_args.args[3]["status"], "maintenance")
        self.assertEqual([h["status"] for h in r.get_json()["hosts"]],
                         ["maintenance", "maintenance"])
        joined = " ".join(str(x) for x in ap.call_args_list)
        self.assertNotIn("systemctl start", joined)

    def test_config_requires_update_and_confirm(self):
        r = self.c.post(self._url("config"), json={
            "env": "prod", "confirm": True, "hosts": self._hosts()},
            headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_recovery_sanitizes_owner_and_returns_digest(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "i1"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="maintenance"), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.inspect",
                           return_value={"owner": {"token": "secret",
                                                   "operation": "config_update"},
                                         "last_result": {"status": "failed"}}), \
                mock.patch("webapp.routes.tomcat_routes._owner_digest",
                           return_value="b" * 64):
            r = self.c.get(self._url("recovery?env=prod"), headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("secret", body)
        self.assertEqual(r.get_json()["hosts"][0]["owner_digest"], "b" * 64)
        self.assertEqual(r.get_json()["hosts"][0]["config_status"], "maintenance")

    def test_revoke_rejects_bad_digest_and_owner_mismatch(self):
        r = self.c.post(self._url("recovery/revoke"), json={
            "env": "prod", "host": "10.0.0.1", "instance_id": "i1",
            "confirm": True, "confirm_digest": "bad"}, headers=self.h)
        self.assertEqual(r.status_code, 400)
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "other"}), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.revoke") as rv:
            r = self.c.post(self._url("recovery/revoke"), json={
                "env": "prod", "host": "10.0.0.1", "instance_id": "i1",
                "confirm": True, "confirm_digest": "b" * 64}, headers=self.h)
        self.assertEqual(r.status_code, 409)
        rv.assert_not_called()

    def test_release_maintenance_gate_rejects_non_war_restart(self):
        """restart 携带 release_maintenance：非 war_tomcat 模式整单 400（守门回归）"""
        r = self.c.post(f"/api/projects/{self.pid}/services", json={
            "name": "orders-ct", "build_type": "maven",
            "deploy_mode": "container", "image_name": "orders-ct",
            "service_name": "orders-ct", "git_repo": "",
            "runtime_config": {}}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        ct_sid = r.get_json()["id"]
        r = self.c.post("/api/tasks", json={
            "project_id": self.pid, "type": "restart", "ref_id": ct_sid,
            "env": "prod", "release_maintenance": True}, headers=self.h)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["error"],
                         "release_maintenance 仅支持 WAR/Tomcat 服务重启")
        # 正向对照：同一请求换 WAR 服务放行 201（打桩 submit_task，不触发执行）
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            r = self.c.post("/api/tasks", json={
                "project_id": self.pid, "type": "restart", "ref_id": self.sid,
                "env": "prod", "release_maintenance": True}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        submit.assert_called_once()
