"""服务配置路由对 jar_service 的校验与单元预览接口"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="jar_routes_")

from webapp import create_app
from webapp import db as dbm


class JarConfigRoutesTest(unittest.TestCase):
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
        r = cls.c.post("/api/projects", json={"name": "jar-test", "code": "jartest"},
                       headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.pid = r.get_json()["id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _create(self, **kw):
        body = dict(name="auth", build_type="maven", deploy_mode="jar_service",
                    image_name="", service_name="",
                    runtime_config={"app_home": "/opt/auth", "port": 8080})
        body.update(kw)
        return self.c.post(f"/api/projects/{self.pid}/services",
                           json=body, headers=self.h)

    def test_unknown_deploy_mode_rejected(self):
        r = self._create(deploy_mode="k8s")
        self.assertEqual(r.status_code, 400)
        self.assertIn("deploy_mode", r.get_json()["error"])

    def test_jar_requires_app_home(self):
        r = self._create(runtime_config={"port": 8080})
        self.assertEqual(r.status_code, 400)
        self.assertIn("app_home", r.get_json()["error"])

    def test_jar_created_without_image_required(self):
        r = self._create(name="auth2")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        svcs = self.c.get(f"/api/projects/{self.pid}/services",
                          headers=self.h).get_json()
        rec = next(x for x in svcs if x["name"] == "auth2")
        # runtime_config 必须按 JSON 对象往返（写库序列化 / 读取反序列化）
        self.assertEqual(rec["runtime_config"]["app_home"], "/opt/auth")

    def test_unit_preview_returns_content(self):
        r = self._create(name="auth3")
        sid = r.get_json()["id"]
        r = self.c.get(f"/api/projects/{self.pid}/services/{sid}/unit",
                       headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["unit_name"], "auth3")  # 未配置 unit_name 时回退服务名
        self.assertIn("CURRENT/app.jar", data["content"])

    def test_unit_onboard_writes_audit(self):
        """管理员逐次接入：调用 unit 写入并对每个主机留审计（SSH 全桩）"""
        r = self._create(name="auth4")
        sid = r.get_json()["id"]
        r = self.c.post(f"/api/projects/{self.pid}/environments",
                        json={"name": "prod"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        # 新建服务器会自动绑定到已启用监控的环境，无需再手工 bind
        r = self.c.post("/api/servers", json={
            "host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        with mock.patch("deploy.RemoteServer"), \
                mock.patch("service_unit.onboard_unit") as onboard:
            r = self.c.post(f"/api/projects/{self.pid}/services/{sid}/unit",
                            json={"env": "prod"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["hosts"], ["10.0.0.9"])
        onboard.assert_called_once()
        logs = dbm.list_audit_logs(self.app.config["DB_PATH"], limit=10)
        self.assertTrue(any(x["action"] == "onboard_service_unit" for x in logs))


if __name__ == "__main__":
    unittest.main()
