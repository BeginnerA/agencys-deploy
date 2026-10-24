"""到期监控 CRUD API + 全局配置测试"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="custom_monitors_test_")

from webapp import create_app


class TestCustomMonitorRoutes(unittest.TestCase):
    """到期监控 API 冒烟测试"""

    @classmethod
    def setUpClass(cls):
        # 目录靠 create_app 注入：改写 webapp.DATA_DIR 会污染同进程其它测试模块
        cls.app = create_app(data_dir=Path(_TMP))
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers=cls._h(token))
        assert r.status_code == 200
        cls.token = cls._login(cls.c, "admin", "test1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        return r.get_json()["token"]

    @staticmethod
    def _h(token):
        return {"Authorization": f"Bearer {token}"}

    def _post(self, body):
        return self.c.post("/api/alerts/monitors", json=body,
                           headers=self._h(self.token))

    def test_create_ssl_monitor(self):
        r = self._post({"type": "ssl", "name": "测试证书", "target": "api.example.com"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertGreater(r.get_json()["id"], 0)

    def test_create_cloud_renewal(self):
        import time
        r = self._post({
            "type": "cloud_renewal", "name": "阿里云 ECS",
            "target": "ecs-bp1xxx",
            "expiry_at": int(time.time()) + 86400 * 90,
            "detail": {"provider": "阿里云", "product": "ECS"}
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_create_invalid_type(self):
        r = self._post({"type": "invalid", "name": "X", "target": "x"})
        self.assertEqual(r.status_code, 400)

    def test_create_ssl_missing_target(self):
        r = self._post({"type": "ssl", "name": "X"})
        self.assertEqual(r.status_code, 400)

    def test_list_monitors(self):
        r = self.c.get("/api/alerts/monitors", headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)

    def test_days_left_recomputed_on_list(self):
        """回归：库中 days_left 为陈旧快照时，列表接口必须按 expiry_at 实时重算

        复现场景：创建时 days_left=30，库中被改写成陈旧值 100（模拟时间流逝后
        快照未刷新），再次读取列表应返回按当前时间重算的天数而非陈旧值。
        """
        import time
        from webapp import db as dbm
        now = int(time.time())
        expiry = now + 86400 * 30
        r = self._post({"type": "cloud_renewal", "name": "刷新测试",
                        "target": "ecs-refresh-test", "expiry_at": expiry})
        mid = r.get_json()["id"]
        # 模拟陈旧快照：把 days_left 写成与 expiry_at 不符的旧值
        dbm.update_record(str(self.app.config["DB_PATH"]),
                          "custom_monitors", mid, {"days_left": 100})
        r2 = self.c.get("/api/alerts/monitors", headers=self._h(self.token))
        data = r2.get_json()
        item = next(m for m in data if m["id"] == mid)
        self.assertNotEqual(item["days_left"], 100)
        # 30 天前后误差允许 ±1 天（时区/执行耗时）
        self.assertLessEqual(abs(item["days_left"] - 30), 1)

    def test_toggle_monitor(self):
        r = self._post({"type": "ssl", "name": "Toggle", "target": "toggle.com"})
        mid = r.get_json()["id"]
        r2 = self.c.post(f"/api/alerts/monitors/{mid}/toggle",
                         headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)
        self.assertIn(r2.get_json()["enabled"], [0, 1])

    def test_delete_monitor(self):
        r = self._post({"type": "ssl", "name": "Del", "target": "del.com"})
        mid = r.get_json()["id"]
        r2 = self.c.delete(f"/api/alerts/monitors/{mid}",
                           headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)

    def test_global_config_get_put(self):
        # 获取默认配置
        r = self.c.get("/api/alerts/monitors/config",
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        # 更新配置
        r2 = self.c.put("/api/alerts/monitors/config",
                        json={"default_warn_days": 15, "default_severity": "P2",
                              "default_channels": ["feishu", "email"]},
                        headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)
        # 验证更新生效
        r3 = self.c.get("/api/alerts/monitors/config",
                        headers=self._h(self.token))
        cfg = r3.get_json()
        self.assertEqual(cfg["default_warn_days"], 15)


if __name__ == "__main__":
    unittest.main()
