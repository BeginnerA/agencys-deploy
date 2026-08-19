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

    def _setup_scope_data(self):
        """插入 scope API 测试所需的项目/环境/服务器/服务数据（幂等）"""
        h = self._h(self.token)
        from webapp import db as dbm
        from pathlib import Path
        db_path = str(Path(_TMP) / "platform.db")
        # 查找或创建项目
        projects = self.c.get("/api/projects", headers=h).get_json()
        existing = [p for p in projects if p["code"] == "scope-p1"]
        if existing:
            pid = existing[0]["id"]
        else:
            r = self.c.post("/api/projects", json={
                "name": "scope-proj", "code": "scope-p1", "description": ""},
                headers=h)
            pid = r.get_json()["id"]
        # 查找或创建环境
        envs = self.c.get(f"/api/projects/{pid}/environments", headers=h).get_json()
        env_names = {e["name"]: e["id"] for e in envs}
        if "prod" in env_names:
            env1 = env_names["prod"]
        else:
            r = self.c.post(f"/api/projects/{pid}/environments", json={
                "name": "prod", "is_default": True}, headers=h)
            env1 = r.get_json()["id"]
        if "test" in env_names:
            env2 = env_names["test"]
        else:
            r = self.c.post(f"/api/projects/{pid}/environments", json={
                "name": "test", "is_default": False}, headers=h)
            env2 = r.get_json()["id"]
        # 创建服务器（直接写 DB，幂等：查找或创建）
        conn = dbm.get_conn(db_path)
        try:
            row = conn.execute("SELECT id FROM servers WHERE host='10.0.0.1'").fetchone()
            if row:
                sid = row[0]
            else:
                sid = dbm.create_record(db_path, "servers", {"host": "10.0.0.1"})
        finally:
            conn.close()
        dbm.bind_server(db_path, env1, sid)
        # 创建服务（幂等：查找或创建）
        svcs = dbm.list_records(db_path, "services", "project_id=? AND name=?",
                                (pid, "scope-svc"))
        if svcs:
            svc_id = svcs[0]["id"]
        else:
            svc_id = dbm.create_record(db_path, "services", dict(
                project_id=pid, name="scope-svc", image_name="scope-svc",
                service_name="scope-svc"))
        return env1, env2, sid, svc_id, pid

    def test_scope_environments(self):
        self._setup_scope_data()
        r = self.c.get("/api/alerts/scope/environments",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertGreaterEqual(len(data), 2)
        self.assertIn("id", data[0])
        self.assertIn("name", data[0])
        self.assertIn("project_name", data[0])

    def test_scope_servers_by_env(self):
        env1, env2, sid, _, _ = self._setup_scope_data()
        r = self.c.get(f"/api/alerts/scope/servers?env_ids={env1}",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], sid)
        self.assertEqual(data[0]["host"], "10.0.0.1")
        # 另一个环境无服务器
        r = self.c.get(f"/api/alerts/scope/servers?env_ids={env2}",
                        headers=self._h(self.token))
        self.assertEqual(r.get_json(), [])

    def test_scope_services_by_env(self):
        env1, env2, _, svc_id, pid = self._setup_scope_data()
        r = self.c.get(f"/api/alerts/scope/services?env_ids={env1}",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], svc_id)
        self.assertEqual(data[0]["name"], "scope-svc")

    def test_scope_no_env_ids_returns_empty(self):
        r = self.c.get("/api/alerts/scope/servers",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), [])

    def test_create_rule_scope_invalid_type(self):
        r = self._create_rule(scope={"type": "bogus", "ids": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("scope", r.get_json()["error"])

    def test_create_rule_scope_env_ids_must_be_list(self):
        r = self._create_rule(scope={"type": "server", "ids": [1], "env_ids": "bad"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("env_ids", r.get_json()["error"])

    def test_create_rule_scope_metric_cannot_use_service(self):
        """指标规则不能选「服务」作用域"""
        r = self._create_rule(
            metric_type="metric", metric="cpu",
            scope={"type": "service", "ids": [1]})
        self.assertEqual(r.status_code, 400)

    def test_create_rule_scope_status_cannot_use_server(self):
        """状态规则不能选「服务器」作用域"""
        r = self._create_rule(
            metric_type="status", metric="service_down",
            scope={"type": "server", "ids": [1]})
        self.assertEqual(r.status_code, 400)

    def test_create_rule_scope_with_env_ids_ok(self):
        r = self._create_rule(scope={
            "type": "server", "ids": [1, 2], "env_ids": [1]})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
