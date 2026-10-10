"""日志检索 API 单元测试"""
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
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

    # ---- 存储概况 + 手动清理（Web 端「立即清理」入口的后端）----

    def _today_str(self, offset_days=0):
        return datetime.fromtimestamp(
            time.time() + offset_days * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%d")

    def _insert_entry(self, ts, content, file_path):
        """插入 log_entries + 同步 FTS（与采集器写入路径一致）"""
        conn = dbm.get_conn(self.db)
        try:
            cur = conn.execute(
                "INSERT INTO log_entries (ts, env, service, container, file_path, "
                "line_offset, line_count, content, keywords) "
                "VALUES (?, 'prod', 'api', '', ?, 0, 1, ?, '[]')",
                (ts, file_path, content))
            conn.execute(
                "INSERT INTO log_fts (rowid, ts, env, service, content) "
                "VALUES (?,?,?,?,?)",
                (cur.lastrowid, str(ts), "prod", "api", content))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def test_storage_stats_api(self):
        """存储概况：返回行数/目录占用/DB 大小与保留策略"""
        self._insert_entry(int(time.time()), "storage probe line", "/tmp/f")
        resp = self.client.get("/api/logs/storage", headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreaterEqual(data["log_entries"], 1)
        self.assertIn("db_size", data)
        self.assertIn("db_freelist_bytes", data)
        self.assertIn("collect", data)
        self.assertIn("archive", data)
        self.assertEqual(data["retention_days"], 7)
        self.assertEqual(data["archive_days"], 30)
        self.assertIn("collector_running", data)

    def test_storage_stats_requires_admin(self):
        """存储概况需要登录：未登录 401（admin 角色限制由 require_role 把守）"""
        resp = self.client.get("/api/logs/storage")
        self.assertEqual(resp.status_code, 401)

    def test_manual_cleanup_deletes_expired(self):
        """手动清理：过期索引行删除 + FTS 一致 + 过期文件归档 + 保留期内数据完好"""
        now = int(time.time())
        # 过期行（20 天前）+ 保留行（当前）
        old_file = f"{self.tmp}/logs/collect/prod/api/{self._today_str(-20)}.log"
        self._insert_entry(now - 20 * 86400, "expired line should go", old_file)
        self._insert_entry(now, "recent line stays", f"{self.tmp}/logs/x.log")
        # 过期 collect 文件（20 天前）+ 保留期内文件（今天）
        collect_dir = Path(self.tmp) / "logs" / "collect" / "prod" / "api"
        collect_dir.mkdir(parents=True, exist_ok=True)
        (collect_dir / f"{self._today_str(-20)}.log").write_text(
            "old log content\n", encoding="utf-8")
        (collect_dir / f"{self._today_str()}.log").write_text(
            "recent log content\n", encoding="utf-8")

        resp = self.client.post("/api/logs/maintenance",
                                headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["deleted_entries"], 1)
        self.assertEqual(data["archived_files"], 1)
        self.assertGreaterEqual(data["db_size_after"], 0)

        conn = dbm.get_conn(self.db)
        try:
            # 过期行删除、保留行完好，FTS 与表一致（断言不依赖其他测试插入的行）
            contents = [r["content"] for r in conn.execute(
                "SELECT content FROM log_entries").fetchall()]
            self.assertNotIn("expired line should go", contents)
            self.assertIn("recent line stays", contents)
            old_hit = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'expired'"
            ).fetchone()[0]
            new_hit = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'recent'"
            ).fetchone()[0]
            self.assertEqual(old_hit, 0)
            self.assertEqual(new_hit, 1)
            # 当日维护标记已写入（当日自动维护跳过）
            flag = conn.execute(
                "SELECT value FROM platform_settings"
                " WHERE key='log_fts_optimize_date'"
            ).fetchone()
            self.assertEqual(json.loads(flag["value"]),
                             datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        finally:
            conn.close()
        # 文件侧：过期 .log 已归档为 .gz，保留期内文件仍在原地
        self.assertFalse((collect_dir / f"{self._today_str(-20)}.log").exists())
        self.assertTrue(
            (Path(self.tmp) / "logs" / "archive" / "prod" / "api"
             / f"{self._today_str(-20)}.log.gz").exists())
        self.assertTrue((collect_dir / f"{self._today_str()}.log").exists())

    def test_manual_cleanup_idempotent(self):
        """手动清理幂等：无过期数据时返回零统计且保留现有数据"""
        now = int(time.time())
        self._insert_entry(now, "keep me", f"{self.tmp}/logs/y.log")
        resp = self.client.post("/api/logs/maintenance",
                                headers=self._auth_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["deleted_entries"], 0)
        self.assertEqual(data["archived_files"], 0)
        conn = dbm.get_conn(self.db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
            self.assertGreaterEqual(n, 1)
        finally:
            conn.close()

    def test_manual_cleanup_requires_admin(self):
        """手动清理仅 admin：未登录 401"""
        resp = self.client.post("/api/logs/maintenance")
        self.assertEqual(resp.status_code, 401)
