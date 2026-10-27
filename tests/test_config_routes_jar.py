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


class JarHostsRoutesTest(unittest.TestCase):
    """每主机版本只读接口：逐用例独立项目，避免环境/主机互相串扰"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="jar_hosts_")
        cls.app = create_app(data_dir=Path(cls._tmp))
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.h = {"Authorization": f"Bearer {cls._login(cls.c, 'admin', 'test1234')}"}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def setUp(self):
        code = "hosts-" + self._testMethodName.lower().replace("_", "-")[:30]
        r = self.c.post("/api/projects", json={"name": code, "code": code},
                        headers=self.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        self.pid = r.get_json()["id"]

    def _create(self, **kw):
        body = dict(name="auth", build_type="maven", deploy_mode="jar_service",
                    image_name="", service_name="",
                    runtime_config={"app_home": "/opt/auth", "port": 8080})
        body.update(kw)
        return self.c.post(f"/api/projects/{self.pid}/services",
                           json=body, headers=self.h)

    def _hosts_ready(self, name, host):
        r = self._create(name=name)
        sid = r.get_json()["id"]
        r = self.c.post(f"/api/projects/{self.pid}/environments",
                        json={"name": "prod"}, headers=self.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        r = self.c.post("/api/servers", json={
            "host": host, "ssh_port": 22, "ssh_user": "root"}, headers=self.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        return sid

    def test_hosts_merges_snapshot_and_live(self):
        sid = self._hosts_ready("auth5", "10.0.0.11")
        dbm.upsert_host_state(self.app.config["DB_PATH"], self.pid, "prod",
                              "auth5", "10.0.0.11", "1.0.0", "0.9.0", 3)
        fake = mock.Mock()
        fake.get_overview.return_value = {"services": {"auth5": {"hosts": [
            {"host": "10.0.0.11", "status": "online", "version": "1.0.1",
             "pid": 42, "uptime_hours": 2.5, "restarts": 1, "cpu": 3.2,
             "rss_mb": 128.0, "unit": "auth5"}]}}}
        with mock.patch("webapp.monitor.get_monitor", return_value=fake):
            r = self.c.get(
                f"/api/projects/{self.pid}/services/{sid}/hosts?env=prod",
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["env"], "prod")
        self.assertEqual(data["unit"], "auth5")
        row = data["hosts"][0]
        self.assertEqual(row["host"], "10.0.0.11")
        self.assertEqual(row["status"], "online")
        self.assertEqual(row["version"], "1.0.1")
        self.assertEqual(row["previous_version"], "0.9.0")
        self.assertEqual(row["pid"], 42)
        self.assertTrue(row["live"])

    def test_hosts_falls_back_to_snapshot_when_monitor_off(self):
        sid = self._hosts_ready("auth6", "10.0.0.12")
        dbm.upsert_host_state(self.app.config["DB_PATH"], self.pid, "prod",
                              "auth6", "10.0.0.12", "2.0.0", "1.9.0", 4)
        with mock.patch("webapp.monitor.get_monitor", return_value=None):
            r = self.c.get(
                f"/api/projects/{self.pid}/services/{sid}/hosts?env=prod",
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = r.get_json()["hosts"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["version"], "2.0.0")
        self.assertFalse(row["live"])

    def test_hosts_rejects_non_jar_service(self):
        r = self.c.post(f"/api/projects/{self.pid}/services", json={
            "name": "web7", "image_name": "web7", "service_name": "web7"},
            headers=self.h)
        sid = r.get_json()["id"]
        r = self.c.get(f"/api/projects/{self.pid}/services/{sid}/hosts",
                       headers=self.h)
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
