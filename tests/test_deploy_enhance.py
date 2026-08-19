# tests/test_deploy_enhance.py
"""发布流程增强测试：数据模型 + 窗口检查 + 调度器 + API"""
import json
import os
import tempfile
import time
import unittest

from webapp import db as dbm


class TestDeployEnhanceSchema(unittest.TestCase):
    """新增 5 张表的建表与 CRUD"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))

    def test_deploy_schedules_crud(self):
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="test schedule",
            ref_type="project", ref_id=1, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=int(time.time()) + 3600, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=int(time.time()) + 3600,
            created_by=1, created_at=int(time.time())))
        rec = dbm.get_record(self.db, "deploy_schedules", sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["name"], "test schedule")
        self.assertEqual(rec["type"], "once")

    def test_release_windows_crud(self):
        wid = dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="春节封网",
            start_at=1700000000, end_at=1700100000,
            block_level="block", repeat_yearly=1,
            created_by=1, created_at=int(time.time())))
        rec = dbm.get_record(self.db, "release_windows", wid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["block_level"], "block")

    def test_service_dependencies_crud(self):
        did = dbm.create_record(self.db, "service_dependencies", dict(
            project_id=self.proj_id, source_service="gateway",
            target_service="auth", dep_type="dubbo",
            created_at=int(time.time())))
        rec = dbm.get_record(self.db, "service_dependencies", did)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["dep_type"], "dubbo")

    def test_deployment_snapshots_crud(self):
        sid = dbm.create_record(self.db, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=0,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="abc123", env_vars_hash="def456",
            deployed_at=int(time.time())))
        rec = dbm.get_record(self.db, "deployment_snapshots", sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["version"], "1.0.0")

    def test_canary_configs_crud(self):
        steps = [{"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True}]
        cid = dbm.create_record(self.db, "canary_configs", dict(
            project_id=self.proj_id, name="default",
            steps=json.dumps(steps), is_default=1,
            created_at=int(time.time())))
        rec = dbm.get_record(self.db, "canary_configs", cid)
        self.assertIsNotNone(rec)
        # steps 在 JSON_FIELDS 中，get_record 已自动反序列化为 list
        parsed = rec["steps"] if isinstance(rec["steps"], list) else json.loads(rec["steps"])
        self.assertEqual(len(parsed), 1)


class TestReleaseWindowCheck(unittest.TestCase):
    """发布窗口检查逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))

    def test_no_windows_returns_none(self):
        from webapp.deploy_scheduler import check_release_window
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNone(result)

    def test_block_window_detected(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="封网", start_at=now - 100, end_at=now + 100,
            block_level="block", repeat_yearly=0, created_by=1, created_at=now))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_level"], "block")
        self.assertEqual(result["name"], "封网")

    def test_warn_window_detected(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=self.proj_id, name="警告窗口",
            start_at=now - 50, end_at=now + 50,
            block_level="warn", repeat_yearly=0,
            created_by=1, created_at=now))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_level"], "warn")

    def test_repeat_yearly_matches(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        # 创建去年同时间段的窗口（repeat_yearly=1）
        one_year = 365 * 24 * 3600
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="春节封网",
            start_at=now - one_year - 100, end_at=now - one_year + 100,
            block_level="block", repeat_yearly=1,
            created_by=1, created_at=now - one_year))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "春节封网")

    def test_window_not_active(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="过期窗口",
            start_at=now - 200, end_at=now - 100,
            block_level="block", repeat_yearly=0,
            created_by=1, created_at=now - 200))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNone(result)
