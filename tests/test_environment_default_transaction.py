"""默认环境切换：必须先完成全部校验再原子切换，失败不得留下“无默认”状态"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm


class TestEnvironmentSwitchRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="env_default_app_")
        from webapp import create_app
        # 目录靠 create_app 注入：改写 webapp.DATA_DIR 会污染同进程其它测试模块
        cls.app = create_app(data_dir=Path(cls.tmp))
        cls.client = cls.app.test_client()
        r = cls.client.post("/api/auth/login", json={
            "username": "admin", "password": "admin"})
        token = r.get_json()["token"]
        r = cls.client.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.get_data(as_text=True)
        r = cls.client.post("/api/auth/login", json={
            "username": "admin", "password": "test1234"})
        cls.headers = {"Authorization": f"Bearer {r.get_json()['token']}"}
        cls.db = cls.app.config["DB_PATH"]

    def setUp(self):
        ts = int(time.time() * 1000)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name=f"p{ts}", code=f"p{ts}", description="", git_org="",
            created_at=int(time.time())))
        self.prod = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1))
        self.stage = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="stage", is_default=0))

    def _put(self, eid, body):
        return self.client.put(
            f"/api/projects/{self.pid}/environments/{eid}",
            json=body, headers=self.headers)

    def _flags(self):
        return {r["name"]: r["is_default"] for r in
                dbm.list_environments(self.db, self.pid)}

    def test_invalid_field_leaves_previous_default(self):
        r = self._put(self.stage, {"is_default": True,
                                   "monitor_enabled": "yes"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._flags(), {"prod": 1, "stage": 0})

    def test_invalid_block_leaves_previous_default(self):
        r = self._put(self.stage, {"is_default": True,
                                   "registry": ["not", "an", "object"]})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._flags(), {"prod": 1, "stage": 0})

    def test_duplicate_name_rejected_without_switching_default(self):
        r = self._put(self.stage, {"is_default": True, "name": "prod"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        self.assertEqual(self._flags(), {"prod": 1, "stage": 0})

    def test_unsafe_name_rejected(self):
        r = self._put(self.stage, {"name": "../evil"})
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))

    def test_valid_switch_moves_default(self):
        r = self._put(self.stage, {"is_default": True})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(self._flags(), {"prod": 0, "stage": 1})


class TestSaveEnvironmentAtomic(unittest.TestCase):
    """db.save_environment：建/改与默认切换在同一事务内"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="env_default_db_")
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        self.prod = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1))
        self.stage = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="stage", is_default=0))

    def test_create_default_in_single_call(self):
        eid = dbm.save_environment(
            self.db, self.pid, {"name": "uat", "is_default": 1},
            make_default=True)
        flags = {r["name"]: r["is_default"]
                 for r in dbm.list_environments(self.db, self.pid)}
        self.assertEqual(flags, {"prod": 0, "stage": 0, "uat": 1})
        self.assertIsNotNone(dbm.get_record(self.db, "environments", eid))

    def test_failed_update_rolls_back_default_clear(self):
        # 名称唯一约束触发事务失败：默认环境必须保持原状
        with self.assertRaises(Exception):
            dbm.save_environment(
                self.db, self.pid,
                {"name": "prod", "is_default": 1},
                env_id=self.stage, make_default=True)
        flags = {r["name"]: r["is_default"]
                 for r in dbm.list_environments(self.db, self.pid)}
        self.assertEqual(flags, {"prod": 1, "stage": 0})
