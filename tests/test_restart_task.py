# tests/test_restart_task.py
"""服务重启任务测试：路由创建 + task_manager 基建 + 执行器 + 分发"""
import os
import tempfile
import time
import unittest
from unittest import mock

from webapp import db as dbm


def _make_svc(db, proj_id, service_name="hqb-auth", health_port=0, health_url=""):
    return dbm.create_record(db, "services", dict(
        project_id=proj_id, name="auth", image_name="auth",
        service_name=service_name, docker_context="", dockerfile="",
        build_order="[]", nacos_groups="[]", dubbo_services="[]",
        snailjob_group="", health_url=health_url, health_port=health_port,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


def _make_env(db, proj_id, name="prod"):
    return dbm.create_record(db, "environments", dict(
        project_id=proj_id, name=name, is_default=1,
        registry="{}", nacos="{}", mysql_snailjob="{}",
        ssh='{"user":"root","password":"envpw"}',
        build="{}", deploy="{}", monitor_enabled=1))


class TestRestartAPI(unittest.TestCase):
    """POST /api/tasks type=restart 创建接口"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        self.app = create_app()
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        with self.app.app_context():
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="p1", code="p1", description="", git_org="",
                created_at=int(time.time())))
            self.svc_id = _make_svc(self.db_path, self.proj_id)
            _make_env(self.db_path, self.proj_id)
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=int(time.time())))
        self.client = self.app.test_client()
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_create_restart_task(self):
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 201)
        task = dbm.get_task(self.db_path, resp.get_json()["id"])
        self.assertEqual(task["type"], "restart")
        self.assertEqual(task["action"], "restart")
        self.assertEqual(task["env"], "prod")
        self.assertEqual(task["version"], "")
        submit.assert_called_once()

    def test_create_restart_env_not_belonging(self):
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "other"})
        self.assertEqual(resp.status_code, 400)

    def test_create_restart_ref_not_found(self):
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": 9999, "env": "prod"})
        self.assertEqual(resp.status_code, 404)

    def test_create_restart_block_window(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="封网", start_at=now - 100, end_at=now + 100,
            block_level="block", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 403)

    def test_create_restart_warn_window(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="警告", start_at=now - 100, end_at=now + 100,
            block_level="warn", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "window_warn")
        # force_window=true 二次确认放行
        with mock.patch("webapp.routes.deploy_routes.submit_task"):
            resp2 = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod", "force_window": True})
        self.assertEqual(resp2.status_code, 201)


# ===== 后续任务追加的测试类锚点 =====
