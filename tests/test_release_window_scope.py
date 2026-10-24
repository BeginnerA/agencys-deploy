"""发布窗口作用域：环境/服务器/服务维度必须在 API、调度、审批链路生效

背景：窗口 scope 已支持 env/server/service，但调用方此前只传 project_id，
导致按环境/服务器/服务配置的封网窗口实际不生效。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm


def _new_db() -> str:
    tmp = tempfile.mkdtemp(prefix="window_scope_")
    db = os.path.join(tmp, "test.db")
    dbm.init_db(db)
    return db


def _create_window(db, scope, level="block", name="封网"):
    now = int(time.time())
    return dbm.create_record(db, "release_windows", dict(
        project_id=0, name=name, start_at=now - 60, end_at=now + 3600,
        block_level=level, scope=scope, repeat_yearly=0,
        created_by=1, created_at=now))


class WindowScopeBase(unittest.TestCase):
    """两个环境、两个服务、两台服务器（仅 prod 绑定服务器）"""

    def setUp(self):
        self.db = _new_db()
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        self.prod = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1))
        self.test = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="test", is_default=0))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="auth",
            service_name="hqb-auth"))
        self.web = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="web", image_name="web",
            service_name="hqb-web"))
        self.srv_a = dbm.create_record(self.db, "servers", dict(
            host="10.0.0.1"))
        self.srv_b = dbm.create_record(self.db, "servers", dict(
            host="10.0.0.2"))
        dbm.bind_server(self.db, self.prod, self.srv_a, 0)
        dbm.bind_server(self.db, self.prod, self.srv_b, 1)


class TestWindowTargetResolution(WindowScopeBase):
    """目标解析：环境名 → 环境 ID，服务/流水线 → 服务 ID，环境 → 服务器列表"""

    def test_resolve_target_expands_env_servers_and_service(self):
        from webapp.deploy_scheduler import resolve_window_target
        t = resolve_window_target(self.db, self.pid, env="prod",
                                  ref_type="project", ref_id=self.auth)
        self.assertEqual(t["env_id"], self.prod)
        self.assertEqual(sorted(t["server_ids"]), sorted([self.srv_a, self.srv_b]))
        self.assertEqual(t["service_ids"], [self.auth])

    def test_resolve_schedule_uses_schedule_target(self):
        from webapp.deploy_scheduler import resolve_window_target
        now = int(time.time())
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.pid, type="once", name="nightly",
            ref_type="project", ref_id=self.auth, env="prod",
            version="1.0.0", previous_version="", extra=[],
            schedule_at=now, cron_expr="", enabled=1,
            last_run_at=0, next_run_at=now, created_by=1, created_at=now))
        t = resolve_window_target(self.db, self.pid, env="prod",
                                  ref_type="deploy_schedules", ref_id=sid)
        self.assertEqual(t["env_id"], self.prod)
        self.assertEqual(t["service_ids"], [self.auth])

    def test_resolve_target_expands_pipeline_stages(self):
        from webapp.deploy_scheduler import resolve_window_target
        stages = [{"name": "s1", "services": [
            {"service_id": self.auth, "name": "auth"},
            {"service_id": self.web, "name": "web"}]}]
        pl = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.pid, name="full", build_first=1, stages=stages))
        t = resolve_window_target(self.db, self.pid, env="prod",
                                  ref_type="pipeline", ref_id=pl)
        self.assertEqual(sorted(t["service_ids"]), sorted([self.auth, self.web]))


class TestWindowScopeMatching(WindowScopeBase):
    """不同作用域窗口只应命中对应目标"""

    def test_env_scope_only_blocks_matching_env(self):
        from webapp.deploy_scheduler import check_deploy_window
        _create_window(self.db, {"type": "env", "ids": [self.prod]})
        hit = check_deploy_window(self.db, self.pid, env="prod",
                                  ref_type="project", ref_id=self.auth)
        self.assertIsNotNone(hit)
        miss = check_deploy_window(self.db, self.pid, env="test",
                                   ref_type="project", ref_id=self.auth)
        self.assertIsNone(miss)

    def test_server_scope_blocks_deploy_hitting_bound_server(self):
        from webapp.deploy_scheduler import check_deploy_window
        _create_window(self.db, {"type": "server", "ids": [self.srv_a],
                                 "env_ids": [self.prod]})
        hit = check_deploy_window(self.db, self.pid, env="prod",
                                  ref_type="project", ref_id=self.auth)
        self.assertIsNotNone(hit)
        # 未绑定服务器的环境不命中
        miss = check_deploy_window(self.db, self.pid, env="test",
                                   ref_type="project", ref_id=self.auth)
        self.assertIsNone(miss)

    def test_service_scope_uses_service_id_and_respects_env_ids(self):
        from webapp.deploy_scheduler import check_deploy_window
        _create_window(self.db, {"type": "service", "ids": [self.auth],
                                 "env_ids": [self.prod]})
        hit = check_deploy_window(self.db, self.pid, env="prod",
                                  ref_type="project", ref_id=self.auth)
        self.assertIsNotNone(hit)
        other_svc = check_deploy_window(self.db, self.pid, env="prod",
                                        ref_type="project", ref_id=self.web)
        self.assertIsNone(other_svc)
        other_env = check_deploy_window(self.db, self.pid, env="test",
                                        ref_type="project", ref_id=self.auth)
        self.assertIsNone(other_env)

    def test_block_wins_over_warn(self):
        from webapp.deploy_scheduler import check_deploy_window
        _create_window(self.db, {"type": "env", "ids": [self.prod]},
                       level="warn", name="告警窗口")
        _create_window(self.db, {"type": "server", "ids": [self.srv_b]},
                       level="block", name="封网窗口")
        hit = check_deploy_window(self.db, self.pid, env="prod",
                                  ref_type="project", ref_id=self.auth)
        self.assertEqual(hit["name"], "封网窗口")


class TestWindowScopeScheduler(WindowScopeBase):
    """调度器必须按计划的环境/服务匹配窗口"""

    def _make_schedule(self):
        now = int(time.time())
        return dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.pid, type="once", name="nightly",
            ref_type="project", ref_id=self.auth, env="prod",
            version="1.0.0", previous_version="", extra=[],
            schedule_at=now - 1, cron_expr="", enabled=1,
            last_run_at=0, next_run_at=now - 1,
            created_by=1, created_at=now))

    def test_other_env_window_does_not_skip_schedule(self):
        from webapp.deploy_scheduler import DeployScheduler
        sid = self._make_schedule()
        _create_window(self.db, {"type": "env", "ids": [self.test]})
        with mock.patch("webapp.task_manager.submit_task") as submit:
            DeployScheduler(self.db)._process_due()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(dbm.get_record(self.db, "deploy_schedules", sid)["enabled"], 0)

    def test_matching_env_window_skips_schedule(self):
        from webapp.deploy_scheduler import DeployScheduler
        sid = self._make_schedule()
        _create_window(self.db, {"type": "env", "ids": [self.prod]})
        with mock.patch("webapp.task_manager.submit_task") as submit:
            DeployScheduler(self.db)._process_due()
        self.assertEqual(submit.call_count, 0)
        self.assertEqual(dbm.get_record(self.db, "deploy_schedules", sid)["enabled"], 0)


class TestWindowScopeApproval(WindowScopeBase):
    """审批通过前必须重新按完整作用域校验窗口"""

    def _plan(self):
        from agent import executor
        return executor.plan_operation(
            self.db, {"id": 1, "username": "admin"}, "deploy",
            target="auth", project_code="p1", env="prod", version="1.0.0")

    def test_block_window_created_after_plan_blocks_approval(self):
        from agent import executor
        run = self._plan()
        _create_window(self.db, {"type": "env", "ids": [self.prod]})
        with mock.patch.object(executor, "_submit_task") as submit:
            with self.assertRaises(executor.ExecutorError) as cm:
                executor.decide_run(self.db, run["id"],
                                    {"id": 1, "username": "admin"}, "approve")
        self.assertIn("封网", str(cm.exception))
        self.assertEqual(submit.call_count, 0)
        self.assertEqual(
            dbm.get_record(self.db, "agent_runs", run["id"])["status"],
            "awaiting_approval")

    def test_warn_window_created_after_plan_requires_force(self):
        from agent import executor
        run = self._plan()
        _create_window(self.db, {"type": "service", "ids": [self.auth]},
                       level="warn", name="重保")
        with mock.patch.object(executor, "_submit_task"):
            with self.assertRaises(executor.ExecutorError):
                executor.decide_run(self.db, run["id"],
                                    {"id": 1, "username": "admin"}, "approve")

    def test_other_env_window_does_not_block_approval(self):
        from agent import executor
        run = self._plan()
        _create_window(self.db, {"type": "env", "ids": [self.test]})
        with mock.patch.object(executor, "_submit_task", return_value=42), \
                mock.patch.object(executor, "_watch_run"):
            out = executor.decide_run(self.db, run["id"],
                                      {"id": 1, "username": "admin"}, "approve")
        self.assertEqual(out["status"], "executing")


class TestWindowScopeAPI(WindowScopeBase):
    """创建任务 API 必须按完整作用域拦截"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="window_scope_app_")
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
        cls.headers = cls._login("test1234")
        cls.db = cls.app.config["DB_PATH"]

    @classmethod
    def _login(cls, pw):
        r = cls.client.post("/api/auth/login", json={
            "username": "admin", "password": pw})
        return {"Authorization": f"Bearer {r.get_json()['token']}"}

    def setUp(self):
        # 复用模块内 app，但每个用例重建业务数据（删除保护会拒绝删项目，直接新建）
        ts = int(time.time() * 1000)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name=f"p{ts}", code=f"p{ts}", description="", git_org="",
            created_at=int(time.time())))
        self.prod = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="auth",
            service_name="hqb-auth"))
        self.srv = dbm.create_record(self.db, "servers", dict(
            host=f"10.9.{ts % 250}.{ts % 199}"))
        dbm.bind_server(self.db, self.prod, self.srv, 0)

    def _post_task(self):
        payload = {"project_id": self.pid, "type": "project",
                   "ref_id": self.auth, "env": "prod", "version": "1.0.0"}
        with mock.patch("webapp.routes.deploy_routes.submit_task"):
            return self.client.post("/api/tasks", json=payload,
                                    headers=self.headers)

    def test_server_scoped_window_blocks_api_task(self):
        _create_window(self.db, {"type": "server", "ids": [self.srv],
                                 "env_ids": [self.prod]})
        r = self._post_task()
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_service_scoped_window_blocks_api_task(self):
        _create_window(self.db, {"type": "service", "ids": [self.auth]})
        r = self._post_task()
        self.assertEqual(r.status_code, 403, r.get_data(as_text=True))

    def test_unrelated_service_window_allows_api_task(self):
        other = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="web", image_name="web",
            service_name="hqb-web"))
        _create_window(self.db, {"type": "service", "ids": [other]})
        r = self._post_task()
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
