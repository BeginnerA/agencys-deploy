"""DB 扩展测试：告警 5 张新表 + services 加列 + JSON 字段往返"""
import os
import shutil
import tempfile
import unittest

from webapp import db as dbm


class TestDbExtensions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_tables_exist(self):
        conn = dbm.get_conn(self.db)
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for t in ("alert_rules", "alerts", "alert_events",
                  "silence_windows", "platform_settings"):
            self.assertIn(t, names)

    def test_alerts_fingerprint_partial_unique(self):
        """同 fingerprint 只能有一条非 resolved 告警（收敛的 DB 层保证）"""
        conn = dbm.get_conn(self.db)
        try:
            idx = [r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'")
                if r["sql"] and "idx_alerts_active" in r["sql"]]
        finally:
            conn.close()
        self.assertEqual(len(idx), 1)
        self.assertIn("WHERE status != 'resolved'", idx[0])

    def test_services_new_columns(self):
        conn = dbm.get_conn(self.db)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        finally:
            conn.close()
        self.assertIn("log_retention_days", cols)
        self.assertIn("log_collect_interval", cols)

    def test_task_extra_persist_roundtrip(self):
        """任务运行参数（--skip-build/--rollback）必须持久化，供"重新运行"恢复"""
        pid = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", created_at=1))
        tid = dbm.create_task(self.db, pid, "project", 1, "svc", "test",
                              "", "", 1, extra=["--skip-build", "--rollback"])
        t = dbm.get_task(self.db, tid)
        self.assertEqual(t["extra"], ["--skip-build", "--rollback"])
        listed = dbm.list_tasks(self.db, project_id=pid, limit=10)
        self.assertEqual(listed[0]["extra"], ["--skip-build", "--rollback"])

    def test_alert_rules_json_roundtrip(self):
        rid = dbm.create_record(self.db, "alert_rules", dict(
            name="cpu", metric_type="metric", metric="cpu", compare=">",
            threshold=80, duration_seconds=300,
            scope={"type": "all", "ids": []}, severity="P2",
            channels=["email"], escalation_interval=30, enabled=1,
            remark="", created_at=1, updated_at=1))
        r = dbm.get_record(self.db, "alert_rules", rid)
        self.assertEqual(r["scope"]["type"], "all")
        self.assertEqual(r["channels"], ["email"])


class TestLogTables(unittest.TestCase):
    """日志中心表结构验证"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_entries_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_entries'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_scan_state_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_scan_state'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_fts_table_exists(self):
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='log_fts'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            conn.close()

    def test_log_entries_in_tables_whitelist(self):
        self.assertIn("log_entries", dbm.TABLES)
        self.assertIn("log_scan_state", dbm.TABLES)


if __name__ == "__main__":
    unittest.main()
