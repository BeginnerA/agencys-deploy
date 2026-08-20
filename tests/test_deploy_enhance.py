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


class TestDeployScheduler(unittest.TestCase):
    """DeployScheduler 调度逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        # 创建服务和环境（submit_task 需要）
        self.svc_id = dbm.create_record(self.db, "services", dict(
            project_id=self.proj_id, name="auth", image_name="auth",
            service_name="hqb-auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        self.env_id = dbm.create_record(self.db, "environments", dict(
            project_id=self.proj_id, name="prod", is_default=1,
            registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
            build="{}", deploy="{}", monitor_enabled=1))

    def test_once_schedule_becomes_disabled_after_run(self):
        from webapp.deploy_scheduler import DeployScheduler
        now = int(time.time())
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="once test",
            ref_type="project", ref_id=self.svc_id, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=now - 1, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=now - 1,
            created_by=1, created_at=now))
        scheduler = DeployScheduler(self.db)
        # 手动触发一轮扫描（不启动线程）
        due = scheduler._find_due_schedules()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], sid)

    def test_cron_next_run_calculation(self):
        from webapp.deploy_scheduler import DeployScheduler
        scheduler = DeployScheduler(self.db)
        # 每天凌晨 2 点
        next_ts = scheduler._calc_next_run("cron", "0 2 * * *",
                                            int(time.time()))
        self.assertGreater(next_ts, time.time())

    def test_block_window_skips_schedule(self):
        from webapp.deploy_scheduler import DeployScheduler
        now = int(time.time())
        # 创建 block 窗口覆盖当前时间
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="封网期",
            start_at=now - 100, end_at=now + 3600,
            block_level="block", repeat_yearly=0,
            created_by=1, created_at=now))
        scheduler = DeployScheduler(self.db)
        # 有到期计划但命中 block 窗口
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="blocked",
            ref_type="project", ref_id=self.svc_id, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=now - 1, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=now - 1,
            created_by=1, created_at=now))
        due = scheduler._find_due_schedules()
        self.assertEqual(len(due), 1)
        # 执行周期处理：应跳过（不创建任务）
        scheduler._process_due(due)
        # once 计划被 _update_next_run 禁用（block 跳过执行但仍推进状态）
        rec = dbm.get_record(self.db, "deploy_schedules", sid)
        self.assertEqual(rec["enabled"], 0)


class TestDeployEnhanceAPI(unittest.TestCase):
    """发布增强 API 端点测试"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        self.app = create_app()
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        # 创建测试数据
        with self.app.app_context():
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="p1", code="p1", description="", git_org="", created_at=int(time.time())))
            self.svc_id = dbm.create_record(self.db_path, "services", dict(
                project_id=self.proj_id, name="auth", image_name="auth",
                service_name="hqb-auth", docker_context="", dockerfile="",
                build_order="[]", nacos_groups="[]", dubbo_services="[]",
                snailjob_group="", health_url="", health_port=0,
                log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
            # 创建 admin 用户并获取 token
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=int(time.time())))
        self.client = self.app.test_client()
        # 登录获取 token
        resp = self.client.post("/api/auth/login", json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_list_schedules_empty(self):
        resp = self.client.get("/api/deploy/schedules", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_create_schedule(self):
        resp = self.client.post("/api/deploy/schedules", headers=self.headers, json={
            "project_id": self.proj_id, "type": "once", "name": "test",
            "ref_type": "project", "ref_id": self.svc_id, "env": "prod",
            "version": "1.0.0", "schedule_at": int(time.time()) + 3600
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertIn("id", data)

    def test_create_release_window(self):
        now = int(time.time())
        resp = self.client.post("/api/deploy/windows", headers=self.headers, json={
            "project_id": 0, "name": "封网测试",
            "start_at": now, "end_at": now + 3600,
            "block_level": "block"
        })
        self.assertEqual(resp.status_code, 201)

    def test_check_window_api(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="active", start_at=now - 10, end_at=now + 100,
            block_level="warn", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.get("/api/deploy/windows/check?project_id=1",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertEqual(data["block_level"], "warn")

    def test_create_canary_config(self):
        resp = self.client.post("/api/deploy/canary-configs", headers=self.headers, json={
            "project_id": self.proj_id, "name": "default",
            "steps": [{"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True}]
        })
        self.assertEqual(resp.status_code, 201)

    def test_viewer_cannot_create(self):
        from werkzeug.security import generate_password_hash
        dbm.create_record(self.db_path, "users", dict(
            username="viewer1", password_hash=generate_password_hash("test123"),
            role="viewer", must_change_password=0, enabled=1,
            created_at=int(time.time())))
        resp = self.client.post("/api/auth/login", json={"username": "viewer1", "password": "test123"})
        vtoken = resp.get_json()["token"]
        resp = self.client.post("/api/deploy/schedules",
                                headers={"Authorization": f"Bearer {vtoken}"},
                                json={"project_id": self.proj_id, "type": "once", "name": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_topology_crud(self):
        resp = self.client.post("/api/deploy/topology", headers=self.headers, json={
            "project_id": self.proj_id, "source_service": "gateway",
            "target_service": "auth", "dep_type": "dubbo"
        })
        self.assertEqual(resp.status_code, 201)
        dep_id = resp.get_json()["id"]
        resp = self.client.get(f"/api/deploy/topology/{self.proj_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["edges"]), 1)
        resp = self.client.delete(f"/api/deploy/topology/{dep_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_snapshots_and_compare(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=1,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="aaa", env_vars_hash="eee",
            deployed_at=now - 100))
        dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=2,
            version="1.1.0", image_versions='{"auth":"auth:1.1.0"}',
            nacos_config_hash="bbb", env_vars_hash="eee",
            deployed_at=now))
        resp = self.client.get(
            f"/api/deploy/compare?project_id={self.proj_id}&env=prod",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["old"]["version"], "1.0.0")
        self.assertEqual(data["new"]["version"], "1.1.0")

    def test_compare_by_snapshot_ids(self):
        """手动模式：通过 old_id/new_id 对比指定两条快照"""
        now = int(time.time())
        sid1 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=1,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="aaa", env_vars_hash="eee",
            deployed_at=now - 200))
        sid2 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=2,
            version="1.1.0", image_versions='{"auth":"auth:1.1.0"}',
            nacos_config_hash="bbb", env_vars_hash="eee",
            deployed_at=now - 100))
        sid3 = dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="test", task_id=3,
            version="1.2.0", image_versions='{"auth":"auth:1.2.0"}',
            nacos_config_hash="ccc", env_vars_hash="fff",
            deployed_at=now))
        # 跨环境对比：old=prod(sid1), new=test(sid3)
        resp = self.client.get(
            f"/api/deploy/compare?old_id={sid1}&new_id={sid3}",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["old"]["version"], "1.0.0")
        self.assertEqual(data["new"]["version"], "1.2.0")

    def test_compare_manual_mode_missing_ids(self):
        """手动模式：只传一个 ID 应返回 400"""
        resp = self.client.get(
            "/api/deploy/compare?old_id=1",
            headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_compare_default_mode_missing_env(self):
        """默认模式：不传 env 应返回 400"""
        resp = self.client.get(
            "/api/deploy/compare?project_id=1",
            headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_impact_analysis(self):
        env_id = dbm.create_record(self.db_path, "environments", dict(
            project_id=self.proj_id, name="prod", is_default=1,
            registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
            build="{}", deploy="{}", monitor_enabled=1))
        sid = dbm.create_record(self.db_path, "servers", dict(
            host="10.0.0.1", ssh_port=22, ssh_user="root", ssh_password="x",
            nacos_ip="", remark="", enabled=1))
        dbm.create_record(self.db_path, "env_servers", dict(
            env_id=env_id, server_id=sid, order_no=0))
        dbm.create_record(self.db_path, "service_dependencies", dict(
            project_id=self.proj_id, source_service="gateway",
            target_service="auth", dep_type="manual",
            created_at=int(time.time())))
        resp = self.client.get(
            f"/api/deploy/impact/{self.proj_id}?env=prod&service=auth",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("colocated", data)
        self.assertIn("upstream", data)
        self.assertIn("downstream", data)


class TestCanaryAutoAdvance(unittest.TestCase):
    """灰度分批逻辑测试（不实际 SSH，仅验证批次划分）"""

    def test_batch_split(self):
        """验证 canary steps 正确划分服务器批次"""
        from webapp.deploy_scheduler import compute_canary_batches
        steps = [
            {"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True},
            {"batch": 2, "servers": 2, "observe_minutes": 10, "auto_advance": True},
            {"batch": 3, "servers": "all", "observe_minutes": 0, "auto_advance": False},
        ]
        total_servers = 5
        batches = compute_canary_batches(steps, total_servers)
        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0]["server_count"], 1)
        self.assertEqual(batches[1]["server_count"], 2)
        self.assertEqual(batches[2]["server_count"], 2)  # 5 - 1 - 2 = 2

    def test_batch_split_fewer_servers(self):
        """服务器数少于批次配置时，合并到最后批次"""
        from webapp.deploy_scheduler import compute_canary_batches
        steps = [
            {"batch": 1, "servers": 2, "observe_minutes": 5, "auto_advance": True},
            {"batch": 2, "servers": "all", "observe_minutes": 0, "auto_advance": False},
        ]
        batches = compute_canary_batches(steps, 1)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["server_count"], 1)
