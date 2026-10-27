"""jar_service 数据层：runtime_config JSON 列与 service_host_state 幂等写入"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm


class JarDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_db_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runtime_config_roundtrip_as_dict(self):
        sid = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="auth",
            service_name="auth", runtime_config={"app_home": "/opt/auth"}))
        rec = dbm.get_record(self.db, "services", sid)
        self.assertEqual(rec["runtime_config"], {"app_home": "/opt/auth"})

    def test_runtime_config_defaults_to_empty_object(self):
        sid = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="web", image_name="web",
            service_name="web"))
        self.assertEqual(dbm.get_record(self.db, "services", sid)["runtime_config"], {})

    def test_host_state_upsert_is_single_row(self):
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.1", "1.0.0", 7)
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.2", "1.0.1", 8)
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_version"], "1.0.2")
        self.assertEqual(rows[0]["last_task_id"], 8)

    def test_host_state_live_keeps_rollback_info(self):
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.1", "1.0.0", 7)
        dbm.record_host_state_live(self.db, self.pid, "prod", "auth",
                                   "10.0.0.1", "1.0.2")
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_version"], "1.0.2")
        self.assertEqual(rows[0]["previous_version"], "1.0.0")  # 直播写穿不清回滚基线
        self.assertEqual(rows[0]["last_task_id"], 7)

    def test_host_state_live_inserts_when_missing(self):
        dbm.record_host_state_live(self.db, self.pid, "prod", "auth",
                                   "10.0.0.2", "2.0.0")
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual([r["host"] for r in rows], ["10.0.0.2"])
        self.assertEqual(rows[0]["current_version"], "2.0.0")

    def test_migrate_adds_column_to_legacy_db(self):
        """存量库（无该列）init_db 后必须自动补列，且旧行读到 {}"""
        import sqlite3
        legacy = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE services (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "project_id INTEGER, name TEXT, image_name TEXT, service_name TEXT)")
        conn.execute("INSERT INTO services (project_id, name, image_name, service_name) "
                     "VALUES (1, 'old', 'old', 'old')")
        conn.commit()
        conn.close()
        dbm.init_db(legacy)
        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT runtime_config FROM services WHERE name='old'").fetchone()
        conn.close()
        self.assertEqual(row["runtime_config"], "{}")


if __name__ == "__main__":
    unittest.main()
