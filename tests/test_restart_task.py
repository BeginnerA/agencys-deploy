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
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp2 = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod", "force_window": True})
            submit.assert_called_once()
            _, _, extra = submit.call_args.args  # (db_path, task, extra)
            self.assertEqual(extra, [])

    def test_create_project_task_no_git_tag(self):
        """no_git_tag=true → extra 追加 --no-git-tag（发布面板本次不打 tag）"""
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "project",
                "ref_id": self.svc_id, "env": "prod",
                "version": "1.0.0", "no_git_tag": True})
        self.assertEqual(resp.status_code, 201)
        submit.assert_called_once()
        _, _, extra = submit.call_args.args  # (db_path, task, extra)
        self.assertIn("--no-git-tag", extra)
        task = dbm.get_task(self.db_path, resp.get_json()["id"])
        self.assertIn("--no-git-tag", task["extra"])

    def test_create_project_task_no_git_tag_invalid(self):
        """no_git_tag 非布尔 → 400"""
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "project",
            "ref_id": self.svc_id, "env": "prod",
            "version": "1.0.0", "no_git_tag": "yes"})
        self.assertEqual(resp.status_code, 400)


class TestTaskManagerInfra(unittest.TestCase):
    """build_cli 对 restart 不产 CLI；kill_task 支持 restart 协作取消"""

    def test_build_cli_restart_empty(self):
        from webapp.task_manager import build_cli
        cmd = build_cli(dict(type="restart", project_id=1, name="auth",
                             env="prod", version="", previous_version=""))
        self.assertEqual(cmd, [])

    def test_build_cli_project_normal(self):
        """回归：project 类型仍组装 deploy.py CLI"""
        from webapp.task_manager import build_cli
        cmd = build_cli(dict(type="project", project_id=1, name="auth",
                             env="prod", version="1.0.0", previous_version=""))
        self.assertTrue(any("deploy.py" in c for c in cmd))

    def test_kill_task_restart_handle(self):
        from webapp.task_manager import _RestartHandle, kill_task, _task_registry
        handle = _RestartHandle()
        _task_registry[99] = handle
        try:
            ok = kill_task(99)
        finally:
            _task_registry.pop(99, None)
        self.assertTrue(ok)
        self.assertTrue(handle.cancel.is_set())


class TestRunRestart(unittest.TestCase):
    """_run_restart 执行器：多服务器并发 + 健康检查 + 汇总判定"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        self.svc_id = _make_svc(self.db, self.proj_id,
                                health_port=8080, health_url="/health")
        self.env_id = _make_env(self.db, self.proj_id)
        for i, host in enumerate(["10.0.0.1", "10.0.0.2"]):
            sid = dbm.create_record(self.db, "servers", dict(
                host=host, ssh_port=22, ssh_user="", ssh_password="",
                nacos_ip="", remark="", enabled=1))
            dbm.create_record(self.db, "env_servers", dict(
                env_id=self.env_id, server_id=sid, order_no=i))
        self.log_file = os.path.join(self.tmp, "task_1.log")
        from webapp.task_manager import _RestartHandle
        self.handle = _RestartHandle()
        self.task = dict(id=1, project_id=self.proj_id, ref_id=self.svc_id,
                         env="prod", type="restart")

    def _run(self):
        from webapp.task_manager import _run_restart
        return _run_restart(self.db, self.task, self.log_file, self.handle)

    def test_all_servers_restart_ok(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "hqb-auth"]) as m, \
             mock.patch("webapp.monitor._probe_health_url",
                        return_value={"ok": True, "ms": 12}):
            status = self._run()
        self.assertEqual(status, "success")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertEqual(m.call_args.args[3][0], "docker ps -a --format '{{.Names}}' | grep -Fx hqb-auth")
        self.assertEqual(m.call_args.args[3][1], "docker restart hqb-auth")
        self.assertIn("[10.0.0.1] 重启成功（健康检查 12ms）", log)
        self.assertIn("汇总：2 台成功", log)

    def test_one_server_restart_fails(self):
        def side_effect(host, port, ssh_cfg, cmds, timeout=10):
            if host == "10.0.0.1":
                return ["hqb-auth", "hqb-auth"]
            raise ConnectionError("ssh refused")
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        side_effect=side_effect), \
             mock.patch("webapp.monitor._probe_health_url",
                        return_value={"ok": True, "ms": 12}):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("[10.0.0.2] SSH/执行失败", log)
        self.assertIn("1 台失败", log)

    def test_container_missing_all_servers(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["", "hqb-auth"]):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("容器不存在，跳过", log)
        self.assertIn("未找到容器", log)

    def test_restart_output_mismatch_fails(self):
        """docker restart 输出不是容器名（stderr 错误文本）→ 判定失败"""
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "Error: No such container"]):
            status = self._run()
        self.assertEqual(status, "failed")

    def test_health_timeout_still_success(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "hqb-auth"]), \
             mock.patch("webapp.monitor._probe_health_url",
                        return_value={"ok": False, "ms": -1}), \
             mock.patch("webapp.task_manager._RESTART_HEALTH_TIMEOUT", 0.1), \
             mock.patch("webapp.task_manager._RESTART_HEALTH_INTERVAL", 0.01):
            status = self._run()
        self.assertEqual(status, "success")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("健康检查超时", log)

    def test_cancel_stops_before_servers(self):
        self.handle.cancel.set()
        with mock.patch("webapp.monitor._ssh_exec_multi") as exec_multi:
            status = self._run()
        self.assertEqual(status, "failed")
        exec_multi.assert_not_called()
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("任务已被终止", log)


class TestDispatch(unittest.TestCase):
    """_run_task 按类型分发：restart 线程内执行，project 子进程回归"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.task = dict(id=1, project_id=1, type="restart", name="auth",
                         env="prod", version="", previous_version="", extra=[])

    def test_run_task_restart_dispatches(self):
        with mock.patch("webapp.task_manager._run_restart",
                        return_value="success") as rr, \
             mock.patch("webapp.task_manager.update_task") as upd:
            from webapp.task_manager import _run_task
            _run_task(self.db, self.task, [])
        rr.assert_called_once()
        # 最终状态落库 success
        final = upd.call_args_list[-1]
        self.assertEqual(final.kwargs.get("status"), "success")

    def test_run_task_project_still_subprocess(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch("webapp.task_manager.subprocess.Popen",
                        return_value=proc) as popen, \
             mock.patch("webapp.task_manager.update_task") as upd:
            from webapp.task_manager import _run_task
            task = dict(self.task, type="project", name="auth")
            _run_task(self.db, task, [])
        popen.assert_called_once()
        final = upd.call_args_list[-1]
        self.assertEqual(final.kwargs.get("status"), "success")


# ===== 后续任务追加的测试类锚点 =====


class TestDeploySnapshot(unittest.TestCase):
    """部署成功写 deployment_snapshots（快照对比的数据生产点）

    覆盖：固定版本单服务 / 自动版本日志解析（单服务无前缀行 + 流水线每服务
    前缀行）/ 失败与 restart 不落快照。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=now))
        self.svc_id = _make_svc(self.db, self.proj_id)
        self.svc_b = dbm.create_record(self.db, "services", dict(
            project_id=self.proj_id, name="api", image_name="api",
            service_name="hqb-api", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))

    def _make_task(self, **over):
        base = dict(type="project", ref_id=self.svc_id, name="auth", env="prod",
                    version="1.0.0", previous_version="", action="deploy")
        base.update(over)
        return dbm.create_task(self.db, self.proj_id, base["type"], base["ref_id"],
                                base["name"], base["env"], base["version"],
                                base["previous_version"], 0,
                                extra=base.get("extra", []), action=base["action"])

    def _snapshots(self):
        return dbm.list_records(self.db, "deployment_snapshots", "project_id=?",
                                (self.proj_id,), "deployed_at")

    def test_fixed_version_service_task(self):
        task_id = self._make_task()
        log = os.path.join(self.tmp, "task.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[platform] 执行命令: deploy.py ...\n全部服务器发布完成\n")
        from webapp.task_manager import _save_deploy_snapshot
        _save_deploy_snapshot(self.db, task_id, log)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["version"], "1.0.0")
        self.assertEqual(snaps[0]["image_versions"], {"auth": "1.0.0"})
        self.assertEqual(snaps[0]["task_id"], task_id)
        self.assertEqual(snaps[0]["env"], "prod")

    def test_auto_version_from_log(self):
        # auto 场景：任务建时 version 空，回填后为 1.0.1（日志无前缀行归属任务 name）
        task_id = self._make_task(version="")
        dbm.update_task(self.db, task_id, version="1.0.1", previous_version="1.0.0")
        log = os.path.join(self.tmp, "task.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("自动版本: 当前线上 1.0.0 → 新版本 1.0.1\n全部服务器发布完成\n")
        from webapp.task_manager import _save_deploy_snapshot
        _save_deploy_snapshot(self.db, task_id, log)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["version"], "1.0.1")
        self.assertEqual(snaps[0]["image_versions"], {"auth": "1.0.1"})

    def test_pipeline_auto_versions(self):
        # 流水线 auto：每服务一行 [name] 前缀；version 空则快照 version 空
        pl_id = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj_id, name="pl1", build_first=1,
            stages=[{"services": [{"service_id": self.svc_id},
                                   {"service_id": self.svc_b}]}]))
        task_id = self._make_task(type="pipeline", ref_id=pl_id, name="pl1",
                                  version="")
        log = os.path.join(self.tmp, "task.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[auth] 自动版本: 当前线上 1.0.0 → 新版本 1.0.2\n"
                    "[api] 自动版本: 当前线上 2.0.0 → 新版本 2.0.1\n")
        from webapp.task_manager import _save_deploy_snapshot
        _save_deploy_snapshot(self.db, task_id, log)
        snaps = self._snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["image_versions"],
                         {"auth": "1.0.2", "api": "2.0.1"})

    def test_pipeline_fixed_version_expands_stages(self):
        # 流水线固定版本：展开 stages 全部服务同版本
        pl_id = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj_id, name="pl1", build_first=1,
            stages=[{"services": [{"service_id": self.svc_id},
                                   {"service_id": self.svc_b}]}]))
        task_id = self._make_task(type="pipeline", ref_id=pl_id, name="pl1",
                                  version="3.0.0")
        from webapp.task_manager import _save_deploy_snapshot
        _save_deploy_snapshot(self.db, task_id, os.path.join(self.tmp, "none.log"))
        snaps = self._snapshots()
        self.assertEqual(snaps[0]["image_versions"],
                         {"auth": "3.0.0", "api": "3.0.0"})

    def test_run_task_success_writes_snapshot(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        task_id = self._make_task()
        task = dbm.get_task(self.db, task_id)
        with mock.patch("webapp.task_manager.subprocess.Popen",
                        return_value=proc), \
             mock.patch("webapp.task_manager.update_task"):
            from webapp.task_manager import _run_task
            _run_task(self.db, task, [])
        self.assertEqual(len(self._snapshots()), 1)

    def test_run_task_failure_no_snapshot(self):
        proc = mock.Mock()
        proc.wait.return_value = 1
        task_id = self._make_task()
        task = dbm.get_task(self.db, task_id)
        with mock.patch("webapp.task_manager.subprocess.Popen",
                        return_value=proc), \
             mock.patch("webapp.task_manager.update_task"):
            from webapp.task_manager import _run_task
            _run_task(self.db, task, [])
        self.assertEqual(self._snapshots(), [])

    def test_restart_no_snapshot(self):
        task_id = self._make_task(type="restart", version="", action="restart")
        log = os.path.join(self.tmp, "task.log")
        from webapp.task_manager import _save_deploy_snapshot
        _save_deploy_snapshot(self.db, task_id, log)
        self.assertEqual(self._snapshots(), [])