"""日志检索 API 单元测试"""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from webapp import db as dbm
from webapp.app import create_app
from werkzeug.security import generate_password_hash


class TestLogRoutes(unittest.TestCase):
    """日志检索 API 测试"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "test.db")
        import webapp
        cls._orig_data_dir = webapp.DATA_DIR
        webapp.DATA_DIR = Path(cls.tmp)
        cls.app = create_app()
        cls.app.config["DB_PATH"] = cls.db
        dbm.init_db(cls.db)
        # 在测试 DB 中直接创建 admin 用户（create_app 的 admin 建在原始 platform.db 中）
        dbm.create_record(cls.db, "users", dict(
            username="admin",
            password_hash=generate_password_hash("admin"),
            role="admin", must_change_password=1, enabled=1,
            created_at=time.time()))
        cls.client = cls.app.test_client()
        # 首次登录 admin/admin（must_change_password=1）
        resp = cls.client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        token = resp.get_json()["token"]
        # 强制改密（需带旧密码 + Bearer token）
        cls.client.post("/api/auth/password",
                        json={"old_password": "admin", "new_password": "test1234"},
                        headers={"Authorization": f"Bearer {token}"})
        # 用新密码重新登录
        resp = cls.client.post("/api/auth/login", json={"username": "admin", "password": "test1234"})
        cls.token = resp.get_json()["token"]

    @classmethod
    def tearDownClass(cls):
        import webapp
        webapp.DATA_DIR = cls._orig_data_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _insert_log_entries(self):
        conn = dbm.get_conn(self.db)
        try:
            for i in range(5):
                cursor = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, 0, 1, ?, '[]')",
                    (int(time.time()) - i * 60, "prod", "api-service",
                     f"/logs/prod/api-service/2026-08-18.log",
                     f"ERROR something failed line {i}")
                )
                rowid = cursor.lastrowid
                conn.execute(
                    "INSERT INTO log_fts (rowid, ts, env, service, content) VALUES (?,?,?,?,?)",
                    (rowid, str(int(time.time()) - i * 60), "prod", "api-service",
                     f"ERROR something failed line {i}")
                )
            conn.commit()
        finally:
            conn.close()

    def test_search_logs_empty(self):
        resp = self.client.get("/api/logs?env=prod&service=api-service&keyword=notfound",
                               headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total"], 0)

    def test_search_logs_with_results(self):
        self._insert_log_entries()
        resp = self.client.get("/api/logs?env=prod&service=api-service&keyword=ERROR",
                               headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreater(data["total"], 0)
        self.assertIn("rows", data)

    def test_search_logs_pagination(self):
        self._insert_log_entries()
        resp = self.client.get("/api/logs?env=prod&service=api-service&page=1&page_size=2",
                               headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertLessEqual(len(data["rows"]), 2)

    def test_context_api_missing_params(self):
        resp = self.client.get("/api/logs/context", headers=self._auth_headers())
        self.assertEqual(resp.status_code, 400)

    def test_services_list(self):
        self._insert_log_entries()
        resp = self.client.get("/api/logs/services", headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, list)
