# tests/test_task_dedup.py
"""同一目标（服务/流水线 + 环境）同时只能有一个活动任务

活动任务 = status ∈ (pending, running)。守卫在 db.create_task 内：
Web / 定时计划 / 手动跑计划 / webhook / Agent 审批 / 调度器全部经本函数建任务，
一处收口即覆盖所有提交入口。
"""
import os
import shutil
import tempfile
import time
import unittest

from webapp import db as dbm


def _proj(db_path, code="p1"):
    now = int(time.time())
    return dbm.create_record(db_path, "projects", dict(
        name=code, code=code, description="", git_org="", created_at=now))


def _svc(db_path, project_id, name="auth"):
    return dbm.create_record(db_path, "services", dict(
        project_id=project_id, name=name, image_name=name, service_name=name,
        docker_context="", dockerfile="", build_order="[]", nacos_groups="[]",
        dubbo_services="[]", snailjob_group="", health_url="", health_port=0,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


class TestActiveTaskConflict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=now))
        self.auth = self._svc("auth")
        self.web = self._svc("web")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _svc(self, name):
        return dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name=name, image_name=name,
            service_name=name, docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))

    def _pl(self, name, *service_ids):
        return dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj, name=name, build_first=1,
            stages=[{"services": [{"service_id": sid} for sid in service_ids]}]))

    def _task(self, type_, ref_id, name, env="prod", version="1.0.0"):
        return dbm.create_task(self.db, self.proj, type_, ref_id, name,
                               env, version, "", 1)

    # ---- 阻塞 ----
    def test_pending_task_blocks_second_deploy_of_same_service(self):
        first = self._task("project", self.auth, "auth")
        with self.assertRaises(dbm.ActiveTaskConflict) as ctx:
            self._task("project", self.auth, "auth", version="1.0.1")
        self.assertEqual(ctx.exception.conflict["task_id"], first)

    def test_running_task_blocks_second_deploy_of_same_service(self):
        dbm.update_task(self.db, self._task("project", self.auth, "auth"),
                        status="running")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("project", self.auth, "auth")

    def test_restart_blocked_by_pending_deploy_of_same_service(self):
        """发布与重启操作的是同一目标，互相排队没有意义，只能二选一"""
        self._task("project", self.auth, "auth")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("restart", self.auth, "auth", version="")

    def test_deploy_blocked_by_pending_restart_of_same_service(self):
        self._task("restart", self.auth, "auth", version="")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("project", self.auth, "auth")

    def test_pipeline_blocked_by_member_service_deploy(self):
        self._task("project", self.auth, "auth")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("pipeline", self._pl("pl1", self.auth, self.web), "pl1")

    def test_member_service_deploy_blocked_by_running_pipeline(self):
        pl = self._pl("pl1", self.auth, self.web)
        dbm.update_task(self.db, self._task("pipeline", pl, "pl1"), status="running")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("project", self.web, "web")

    def test_two_pipelines_sharing_a_service_conflict(self):
        pl_a = self._pl("pl-a", self.auth, self.web)
        pl_b = self._pl("pl-b", self.web)
        self._task("pipeline", pl_a, "pl-a")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("pipeline", pl_b, "pl-b")

    def test_conflict_carries_active_task_identity(self):
        """409 要能告诉操作员"谁占着"：任务号 / 名称 / 状态都要在异常里"""
        first = self._task("project", self.auth, "auth")
        dbm.update_task(self.db, first, status="running")
        with self.assertRaises(dbm.ActiveTaskConflict) as ctx:
            self._task("project", self.auth, "auth")
        info = ctx.exception.conflict
        self.assertEqual(info["task_id"], first)
        self.assertEqual(info["name"], "auth")
        self.assertEqual(info["status"], "running")
        self.assertEqual(info["type"], "project")
        self.assertEqual(info["env"], "prod")

    # ---- 放行 ----
    def test_other_env_allowed(self):
        self._task("project", self.auth, "auth", env="prod")
        self.assertTrue(self._task("project", self.auth, "auth", env="test"))

    def test_other_service_allowed(self):
        self._task("project", self.auth, "auth")
        self.assertTrue(self._task("project", self.web, "web"))

    def test_finished_task_does_not_block(self):
        tid = self._task("project", self.auth, "auth")
        dbm.update_task(self.db, tid, status="success", finished_at=time.time())
        self.assertTrue(self._task("project", self.auth, "auth", version="1.0.1"))

    def test_failed_task_does_not_block(self):
        tid = self._task("project", self.auth, "auth")
        dbm.update_task(self.db, tid, status="failed", finished_at=time.time())
        self.assertTrue(self._task("project", self.auth, "auth", version="1.0.1"))

    def test_pipeline_without_overlap_allowed(self):
        pl = self._pl("pl-web-only", self.web)
        self._task("project", self.auth, "auth")
        self.assertTrue(self._task("pipeline", pl, "pl-web-only"))

    def test_other_project_allowed(self):
        proj2 = dbm.create_record(self.db, "projects", dict(
            name="p2", code="p2", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "1.0.0", "", 1)
        # 另一项目的同名 ref_id 不受影响（跨项目 ref_id 会复用）
        self.assertTrue(dbm.create_task(self.db, proj2, "project", self.auth,
                                        "auth", "prod", "1.0.0", "", 1))

    # ---- 键空间 ----
    def test_unknown_type_keeps_its_own_key_space(self):
        """历史/测试用 type（如 deploy）不并入服务键空间：与 project/restart 互不占用，
        但同 type 同目标之间仍然互斥"""
        self._task("project", self.auth, "auth")
        self.assertTrue(self._task("deploy", self.auth, "auth"))
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("deploy", self.auth, "auth")

    def test_schedule_service_type_shares_service_namespace(self):
        """定时计划的 ref_type=service 会原样写入任务 type，
        必须与手工发布（project）占同一个目标"""
        self._task("project", self.auth, "auth")
        with self.assertRaises(dbm.ActiveTaskConflict):
            self._task("service", self.auth, "auth")


class TestInterruptedTaskRecovery(unittest.TestCase):
    """平台重启后的残留活动任务：必须收敛为终态，否则目标被一行幽灵记录永久占用"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.proj = _proj(self.db)
        self.auth = _svc(self.db, self.proj, "auth")
        self.web = _svc(self.db, self.proj, "web")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stale(self, status, with_log=False, db=None, svc=None):
        db = db or self.db
        svc = svc or self.auth
        tid = dbm.create_task(db, self.proj, "project", svc, "auth",
                              "prod", "1.0.0", "", 1)
        fields = {"status": status}
        if with_log:
            log = os.path.join(self.log_dir, f"task_{tid}.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write("[platform] 执行命令: python deploy.py --project auth\n")
            fields["log_file"] = log
        dbm.update_task(db, tid, **fields)
        return tid

    def test_reap_marks_pending_and_running_failed(self):
        from webapp.task_manager import reap_interrupted_tasks
        # 两个目标各留一行：同一目标的活动任务本来就不允许并存（见互斥闸）
        t1 = self._stale("pending")
        t2 = self._stale("running", svc=self.web)
        self.assertEqual(reap_interrupted_tasks(self.db), 2)
        for tid in (t1, t2):
            task = dbm.get_task(self.db, tid)
            self.assertEqual(task["status"], "failed")
            self.assertTrue(task["finished_at"])

    def test_reap_skips_tasks_owned_by_this_process(self):
        """_task_registry 里有句柄 = 本进程真的在跑/排队，不能被回收
        （同进程可能 create_app 多个实例，回收范围只能是无人认领的行）"""
        from unittest import mock

        from webapp.task_manager import reap_interrupted_tasks
        tid = self._stale("running")
        with mock.patch.dict("webapp.task_manager._task_registry", {tid: object()}):
            self.assertEqual(reap_interrupted_tasks(self.db), 0)
        self.assertEqual(dbm.get_task(self.db, tid)["status"], "running")

    def test_reap_appends_reason_to_task_log(self):
        from webapp.task_manager import reap_interrupted_tasks
        tid = self._stale("running", with_log=True)
        reap_interrupted_tasks(self.db)
        with open(dbm.get_task(self.db, tid)["log_file"], encoding="utf-8") as f:
            content = f.read()
        self.assertIn("平台重启", content)
        self.assertIn("deploy.py", content)  # 原日志不得被覆盖

    def test_terminal_tasks_untouched(self):
        from webapp.task_manager import reap_interrupted_tasks
        tid = self._stale("pending")
        dbm.update_task(self.db, tid, status="success", finished_at=time.time())
        self.assertEqual(reap_interrupted_tasks(self.db), 0)
        self.assertEqual(dbm.get_task(self.db, tid)["status"], "success")

    def test_reaped_target_accepts_new_task(self):
        """回收后目标立即释放：这是"幽灵行永久占用"的实际修复点"""
        from webapp.task_manager import reap_interrupted_tasks
        self._stale("running")
        with self.assertRaises(dbm.ActiveTaskConflict):
            dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                            "prod", "1.0.1", "", 1)
        reap_interrupted_tasks(self.db)
        self.assertTrue(dbm.create_task(self.db, self.proj, "project", self.auth,
                                        "auth", "prod", "1.0.1", "", 1))

    def test_create_app_reaps_on_startup(self):
        """回收必须落在 create_app 实际打开的那份库上（实例库名固定 platform.db）"""
        from webapp import create_app
        data_dir = os.path.join(self.tmp, "instance")
        os.makedirs(data_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "platform.db")
        dbm.init_db(db_path)
        self.proj = _proj(db_path)
        self.auth = _svc(db_path, self.proj)
        tid = self._stale("running", db=db_path)
        create_app(data_dir=data_dir, log_dir=self.log_dir)
        self.assertEqual(dbm.get_task(db_path, tid)["status"], "failed")


class _AppCase(unittest.TestCase):
    """带登录态的 app 夹具（各提交入口共用）"""

    def setUp(self):
        from werkzeug.security import generate_password_hash

        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        from webapp import create_app
        # 目录靠 create_app 注入：裸调用会让工厂在默认落点上跑一遍迁移/引导
        self.app = create_app(data_dir=self.tmp)
        self.app.config["DB_PATH"] = self.db
        self.app.config["TESTING"] = True
        now = int(time.time())
        with self.app.app_context():
            self.proj = dbm.create_record(self.db, "projects", dict(
                name="p1", code="p1", description="", git_org="", created_at=now))
            self.auth = self._svc("auth")
            self.web = self._svc("web")
            dbm.create_record(self.db, "environments", dict(
                project_id=self.proj, name="prod", is_default=1, registry="{}",
                nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
                deploy="{}", monitor_enabled=1))
            dbm.create_record(self.db, "environments", dict(
                project_id=self.proj, name="test", is_default=0, registry="{}",
                nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
                deploy="{}", monitor_enabled=1))
            dbm.create_record(self.db, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1, created_at=now))
        self.client = self.app.test_client()
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin", "password": "test123"})
        self.headers = {"Authorization": f"Bearer {resp.get_json()['token']}"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _svc(self, name):
        return dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name=name, image_name=name,
            service_name=name, docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))

    def _pl(self, name, *service_ids):
        return dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj, name=name, build_first=1,
            stages=[{"services": [{"service_id": sid} for sid in service_ids]}]))

    def _task(self, type_, ref_id, name, env="prod", version="1.0.0"):
        return dbm.create_task(self.db, self.proj, type_, ref_id, name,
                               env, version, "", 1)


class TestWebEntry(_AppCase):
    """POST /api/tasks：重复提交回 409，并把占用方回显给操作员"""

    def _post(self, payload):
        from unittest import mock
        with mock.patch("webapp.routes.deploy_routes.submit_task"):
            return self.client.post("/api/tasks", headers=self.headers,
                                    json=payload)

    def test_second_deploy_of_same_service_returns_409(self):
        first = self._post({"project_id": self.proj, "type": "project",
                            "ref_id": self.auth, "env": "prod",
                            "version": "1.0.0"})
        self.assertEqual(first.status_code, 201)
        second = self._post({"project_id": self.proj, "type": "project",
                             "ref_id": self.auth, "env": "prod",
                             "version": "1.0.1"})
        self.assertEqual(second.status_code, 409)
        body = second.get_json()
        self.assertEqual(body["error"], "active_task")
        self.assertEqual(body["active_task"]["task_id"], first.get_json()["id"])
        self.assertIn("已有活动任务", body["message"])

    def test_no_task_row_written_for_rejected_submit(self):
        """被拒的提交不得留下 pending 幽灵行（否则占用判定会被自己污染）"""
        self._post({"project_id": self.proj, "type": "project",
                    "ref_id": self.auth, "env": "prod", "version": "1.0.0"})
        self._post({"project_id": self.proj, "type": "project",
                    "ref_id": self.auth, "env": "prod", "version": "1.0.1"})
        rows = [t for t in dbm.list_tasks(self.db, self.proj) if t["type"] == "project"]
        self.assertEqual(len(rows), 1)

    def test_restart_of_deploying_service_returns_409(self):
        self._post({"project_id": self.proj, "type": "project",
                    "ref_id": self.auth, "env": "prod", "version": "1.0.0"})
        resp = self._post({"project_id": self.proj, "type": "restart",
                           "ref_id": self.auth, "env": "prod"})
        self.assertEqual(resp.status_code, 409)

    def test_other_env_still_accepted(self):
        self._post({"project_id": self.proj, "type": "project",
                    "ref_id": self.auth, "env": "prod", "version": "1.0.0"})
        resp = self._post({"project_id": self.proj, "type": "project",
                           "ref_id": self.auth, "env": "test", "version": "1.0.0"})
        self.assertEqual(resp.status_code, 201)

    def test_pipeline_member_conflict_returns_409(self):
        self._post({"project_id": self.proj, "type": "project",
                    "ref_id": self.auth, "env": "prod", "version": "1.0.0"})
        pl = self._pl("pl1", self.auth, self.web)
        resp = self._post({"project_id": self.proj, "type": "pipeline",
                           "ref_id": pl, "env": "prod", "version": "1.0.0"})
        self.assertEqual(resp.status_code, 409)


class TestRunScheduleEntry(_AppCase):
    """POST /api/deploy/schedules/<id>/run：手动立即执行同样受互斥约束"""

    def _run(self, sid):
        from unittest import mock
        with mock.patch("webapp.task_manager.submit_task"):
            return self.client.post(f"/api/deploy/schedules/{sid}/run",
                                    headers=self.headers)

    def _schedule(self):
        now = int(time.time())
        return dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj, type="once", name="once-1",
            ref_type="project", ref_id=self.auth, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=now, cron_expr="", enabled=1, last_run_at=0,
            next_run_at=now, created_by=1, created_at=now))

    def test_run_schedule_twice_returns_409(self):
        sid = self._schedule()
        self.assertEqual(self._run(sid).status_code, 201)
        resp = self._run(sid)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "active_task")

    def test_run_schedule_conflict_keeps_schedule_untouched(self):
        """被拒不推进 last_run_at：计划本身没执行过"""
        sid = self._schedule()
        self._task("project", self.auth, "auth")
        self._run(sid)
        self.assertEqual(dbm.get_record(self.db, "deploy_schedules", sid)["last_run_at"], 0)


class TestWebhookEntry(_AppCase):
    """POST /api/deploy/webhook：外部 CI 触发也要拿到机器可读的冲突码"""

    def _hook(self, payload):
        from unittest import mock
        with mock.patch("webapp.routes.deploy_enhance_routes._get_webhook_token",
                        return_value="tok"), \
                mock.patch("webapp.task_manager.submit_task"):
            return self.client.post("/api/deploy/webhook",
                                    headers={"X-Webhook-Token": "tok"},
                                    json=payload)

    def test_webhook_conflict_returns_409(self):
        body = {"project_id": self.proj, "service": "auth", "env": "prod",
                "version": "1.0.0"}
        self.assertEqual(self._hook(body).status_code, 201)
        resp = self._hook(dict(body, version="1.0.1"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "active_task")


class TestSchedulerEntry(unittest.TestCase):
    """调度器：目标被占用时按计划跳过（与封网窗口同语义），不刷重试"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=now))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name="auth", image_name="auth",
            service_name="auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "1.0.0", "", 1)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _due_schedule(self):
        now = int(time.time())
        return dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj, type="cron", name="nightly",
            ref_type="project", ref_id=self.auth, env="prod",
            version="2.0.0", previous_version="", extra="[]",
            schedule_at=0, cron_expr="0 2 * * *", enabled=1, last_run_at=0,
            next_run_at=now - 1, created_by=1, created_at=now))

    def test_conflict_skips_without_creating_task(self):
        from unittest import mock

        from webapp.deploy_scheduler import DeployScheduler
        sid = self._due_schedule()
        scheduler = DeployScheduler(self.db)
        with mock.patch("webapp.task_manager.submit_task") as submit:
            scheduler._process_due()
        submit.assert_not_called()
        rows = dbm.list_records(self.db, "tasks", "version=?", ("2.0.0",))
        self.assertEqual(rows, [])
        # 跳过即推进（同 block 窗口语义）：不推进会让每轮 tick 重复撞同一个占用
        rec = dbm.get_record(self.db, "deploy_schedules", sid)
        self.assertGreater(rec["next_run_at"], int(time.time()))

    def test_conflict_skip_is_logged(self):
        from unittest import mock

        from webapp.deploy_scheduler import DeployScheduler
        self._due_schedule()
        scheduler = DeployScheduler(self.db)
        with mock.patch("webapp.task_manager.submit_task"), \
                mock.patch("builtins.print") as out:
            scheduler._process_due()
        printed = " ".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("已有活动任务", printed)


class TestAgentPlanEntry(unittest.TestCase):
    """Agent 通道：目标占用时不生成注定跑不起来的审批单"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="ynlt", code="ynlt", description="", git_org="", created_at=now))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name="auth", image_name="auth",
            service_name="hqb-auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        dbm.create_record(self.db, "environments", dict(
            project_id=self.proj, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
            deploy="{}", monitor_enabled=1))
        self.user = {"id": 1, "username": "admin"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self, op="deploy", version="1.0.0"):
        from agent import executor
        return executor.plan_operation(self.db, self.user, op, target="auth",
                                       env="prod", version=version)

    def test_plan_rejected_while_target_busy(self):
        from agent import executor
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "1.0.0", "", 1)
        with self.assertRaises(executor.ExecutorError) as ctx:
            self._plan()
        self.assertIn("已有活动任务", str(ctx.exception))

    def test_no_run_record_left_by_rejected_plan(self):
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "1.0.0", "", 1)
        with self.assertRaises(Exception):
            self._plan()
        self.assertEqual(dbm.list_records(self.db, "agent_runs"), [])

    def test_plan_allowed_when_idle(self):
        run = self._plan()
        self.assertEqual(run["status"], "awaiting_approval")


class TestAgentApproveEntry(unittest.TestCase):
    """审批通过后提交冲突：沿用 decide_run 既有失败回退（释放领取 + 可读原因）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="ynlt", code="ynlt", description="", git_org="", created_at=now))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name="auth", image_name="auth",
            service_name="hqb-auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        dbm.create_record(self.db, "environments", dict(
            project_id=self.proj, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
            deploy="{}", monitor_enabled=1))
        self.user = {"id": 1, "username": "admin"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_approve_reports_conflict_and_releases_claim(self):
        from unittest import mock

        from agent import executor
        run = executor.plan_operation(self.db, self.user, "deploy",
                                      target="auth", env="prod", version="1.0.0")
        # 占用发生在计划生成之后（审批前）：提交阶段兜住并给出可读原因
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "0.9.0", "", 1)
        with mock.patch("agent.executor.task_manager.submit_task"), \
                self.assertRaises(executor.ExecutorError) as ctx:
            executor.decide_run(self.db, run["id"], self.user, "approve")
        self.assertIn("已有活动任务", str(ctx.exception))
        self.assertNotEqual(
            dbm.get_record(self.db, "agent_runs", run["id"])["status"], "executing")



class TestFindActiveTask(unittest.TestCase):
    """守卫的查询侧：入口在提交前也要能主动查占用（Agent 预检 / 大屏提示）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="",
            created_at=int(time.time())))
        self.auth = dbm.create_record(self.db, "services", dict(
            project_id=self.proj, name="auth", image_name="auth",
            service_name="auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_none_when_idle(self):
        self.assertIsNone(dbm.find_active_task(self.db, self.proj, "project",
                                               self.auth, env="prod"))

    def test_returns_the_active_task(self):
        tid = dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                              "prod", "1.0.0", "", 1)
        found = dbm.find_active_task(self.db, self.proj, "restart", self.auth,
                                     env="prod")
        self.assertEqual(found["task_id"], tid)

    def test_env_blank_matches_blank_only(self):
        dbm.create_task(self.db, self.proj, "project", self.auth, "auth",
                        "prod", "1.0.0", "", 1)
        self.assertIsNone(dbm.find_active_task(self.db, self.proj, "project",
                                               self.auth, env="test"))


if __name__ == "__main__":
    unittest.main()
