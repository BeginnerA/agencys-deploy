"""配置读接口凭据脱敏：环境/服务器的密码只写不回显（含 viewer 角色）"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="config_mask_test_")

from webapp import create_app


class TestConfigSecretMasking(unittest.TestCase):
    """真实 create_app + 独立临时 DB：验证所有读接口不返回明文凭据"""

    @classmethod
    def setUpClass(cls):
        # 目录靠 create_app 注入：改写 webapp.DATA_DIR 会污染同进程其它测试模块
        cls.app = create_app(data_dir=Path(_TMP))
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers=cls._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.admin = cls._login(cls.c, "admin", "test1234")

        r = cls.c.post("/api/users", json={
            "username": "viewer1", "password": "viewer123", "role": "viewer"},
            headers=cls._h(cls.admin))
        assert r.status_code == 201, r.get_data(as_text=True)
        viewer_token = cls._login(cls.c, "viewer1", "viewer123")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "viewer123", "new_password": "viewer456"},
            headers=cls._h(viewer_token))
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.viewer = cls._login(cls.c, "viewer1", "viewer456")

        r = cls.c.post("/api/projects", json={
            "name": "脱敏测试项目", "code": "mask_test"},
            headers=cls._h(cls.admin))
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.pid = r.get_json()["id"]

        r = cls.c.post(f"/api/projects/{cls.pid}/environments", json={
            "name": "prod",
            "registry": {"host": "harbor.local", "username": "robot",
                         "password": "registry-SECRET"},
            "nacos": {"url": "http://nacos.local:8848", "username": "nacos",
                      "password": "nacos-SECRET"},
            "mysql_snailjob": {"host": "mysql.local", "username": "snail",
                               "password": "mysql-SECRET"},
            "ssh": {"user": "deploy", "port": 2222,
                    "password": "ssh-SECRET"},
        }, headers=cls._h(cls.admin))
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.eid = r.get_json()["id"]

        r = cls.c.post("/api/servers", json={
            "host": "10.0.0.99", "ssh_user": "deploy",
            "ssh_password": "server-SECRET"}, headers=cls._h(cls.admin))
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.sid = r.get_json()["id"]
        r = cls.c.post(f"/api/environments/{cls.eid}/servers",
                       json={"server_id": cls.sid},
                       headers=cls._h(cls.admin))
        assert r.status_code == 200, r.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    @staticmethod
    def _h(token):
        return {"Authorization": f"Bearer {token}"}

    def test_environment_detail_masks_all_credentials(self):
        r = self.c.get(f"/api/projects/{self.pid}/environments/{self.eid}",
                       headers=self._h(self.viewer))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["ssh"]["password"], "***")
        self.assertEqual(data["registry"]["password"], "***")
        self.assertEqual(data["nacos"]["password"], "***")
        self.assertEqual(data["mysql_snailjob"]["password"], "***")
        # 非敏感字段必须保留，前端编辑/克隆仍可用
        self.assertEqual(data["ssh"]["user"], "deploy")
        self.assertEqual(data["ssh"]["port"], 2222)
        self.assertEqual(data["registry"]["host"], "harbor.local")
        body = r.get_data(as_text=True)
        for secret in ("ssh-SECRET", "registry-SECRET",
                       "nacos-SECRET", "mysql-SECRET"):
            self.assertNotIn(secret, body)

    def test_environment_list_masks_all_credentials(self):
        r = self.c.get(f"/api/projects/{self.pid}/environments",
                       headers=self._h(self.viewer))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        env = next(e for e in r.get_json() if e["id"] == self.eid)
        for block in ("ssh", "registry", "nacos", "mysql_snailjob"):
            self.assertEqual(env[block]["password"], "***", block)
        body = r.get_data(as_text=True)
        for secret in ("ssh-SECRET", "registry-SECRET",
                       "nacos-SECRET", "mysql-SECRET"):
            self.assertNotIn(secret, body)

    def test_server_lists_mask_ssh_password(self):
        for url in ("/api/servers", f"/api/environments/{self.eid}/servers"):
            r = self.c.get(url, headers=self._h(self.viewer))
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            srv = next(s for s in r.get_json() if s["id"] == self.sid)
            self.assertEqual(srv["ssh_password"], "***", url)
            self.assertNotIn("server-SECRET", r.get_data(as_text=True))

    def test_update_with_masked_passwords_keeps_stored_values(self):
        r = self.c.get(f"/api/projects/{self.pid}/environments/{self.eid}",
                       headers=self._h(self.admin))
        body = r.get_json()
        body["is_default"] = bool(body["is_default"])
        body["monitor_enabled"] = bool(body["monitor_enabled"])
        r = self.c.put(f"/api/projects/{self.pid}/environments/{self.eid}",
                       json=body, headers=self._h(self.admin))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        from webapp import db as dbm
        rec = dbm.get_record(self.app.config["DB_PATH"], "environments",
                             self.eid)
        self.assertEqual(rec["ssh"]["password"], "ssh-SECRET")
        self.assertEqual(rec["registry"]["password"], "registry-SECRET")
        self.assertEqual(rec["nacos"]["password"], "nacos-SECRET")
        self.assertEqual(rec["mysql_snailjob"]["password"], "mysql-SECRET")

    def test_create_with_masked_passwords_never_stores_placeholder(self):
        r = self.c.get(f"/api/projects/{self.pid}/environments/{self.eid}",
                       headers=self._h(self.admin))
        clone = r.get_json()
        clone["name"] = "staging"
        clone["is_default"] = False
        clone["monitor_enabled"] = True
        r = self.c.post(f"/api/projects/{self.pid}/environments", json=clone,
                        headers=self._h(self.admin))
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        from webapp import db as dbm
        rec = dbm.get_record(self.app.config["DB_PATH"], "environments",
                             r.get_json()["id"])
        for block in ("ssh", "registry", "nacos", "mysql_snailjob"):
            self.assertNotIn("password", rec[block], block)


if __name__ == "__main__":
    unittest.main()
