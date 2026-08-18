"""告警路由测试：静默窗口输入校验 / 规则跨字段一致性（Flask 客户端 + 临时 DB）"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg

_TMP = tempfile.mkdtemp(prefix="alert_routes_test_")
webapp_pkg.DATA_DIR = Path(_TMP)

from webapp import create_app


class TestAlertRoutes(unittest.TestCase):
    """基于真实 create_app + 独立临时 DB 的 API 冒烟（登录→改密→操作）"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers=cls._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.token = cls._login(cls.c, "admin", "test1234")

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

    def _post_silence(self, body):
        return self.c.post("/api/alerts/silences", json=body,
                           headers=self._h(self.token))

    def _create_rule(self, **kw):
        fields = dict(name="CPU 过高", metric_type="metric", metric="cpu",
                      compare=">", threshold=80, duration_seconds=0,
                      scope={"type": "all", "ids": []}, severity="P2",
                      channels=["wecom"])
        fields.update(kw)
        return self.c.post("/api/alerts/rules", json=fields,
                           headers=self._h(self.token))

    def test_create_silence_recurring_invalid_time(self):
        r = self._post_silence(dict(name="周维护", type="recurring",
                                    start_at="abc", end_at="25:99",
                                    weekdays=[1],
                                    scope={"rules": [], "envs": [], "servers": []}))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("HH:MM", r.get_json()["error"])

    def test_create_silence_invalid_scope(self):
        r = self._post_silence(dict(name="窗口", type="one_time",
                                    start_at=100, end_at=200, scope="oops"))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("scope", r.get_json()["error"])

    def test_create_silence_ok(self):
        r = self._post_silence(dict(name="窗口", type="one_time",
                                    start_at=100, end_at=200,
                                    scope={"rules": [], "envs": [], "servers": []}))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertGreater(r.get_json()["id"], 0)

    def test_update_silence_recurring_invalid_time(self):
        r = self._post_silence(dict(name="窗口", type="one_time",
                                    start_at=100, end_at=200,
                                    scope={"rules": [], "envs": [], "servers": []}))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        sid = r.get_json()["id"]
        r = self.c.put(f"/api/alerts/silences/{sid}",
                       json={"type": "recurring", "start_at": "9:5",
                             "end_at": "18:00"},
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("HH:MM", r.get_json()["error"])

    def test_update_rule_cross_field_mismatch(self):
        r = self._create_rule()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rid = r.get_json()["id"]
        # 只改 metric_type=status，metric 仍为 cpu → 跨字段失配 → 400
        r = self.c.put(f"/api/alerts/rules/{rid}", json={"metric_type": "status"},
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("metric", r.get_json()["error"])
        # 规则未被污染
        rule = next(x for x in self.c.get("/api/alerts/rules",
                                          headers=self._h(self.token)).get_json()
                    if x["id"] == rid)
        self.assertEqual(rule["metric_type"], "metric")

    def test_update_rule_scalar_types_rejected(self):
        r = self._create_rule()
        rid = r.get_json()["id"]
        r = self.c.put(f"/api/alerts/rules/{rid}", json={"threshold": "high"},
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertIn("threshold", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
