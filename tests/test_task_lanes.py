# tests/test_task_lanes.py
"""并行执行道：哪些任务可以同时跑，哪些必须排队

单 worker 串行队列把一次 20 分钟的 Maven 构建放大成全平台停机；无脑并发又会破坏
读-改-写型的共享状态。真正决定"能不能并行"的不是主机，而是任务会碰到的共享资源：

  svc/pl   发布目标本身（与提交互斥闸同一套键，见 db.task_target_keys）
  repo     构建工作目录（git clone/checkout/pull 的目标目录）
  mod      build_order 每个模块的 pom/package 目录（mvn clean 会删 target/）
  compose  宿主机上的 docker-compose.yml（平台先下载改镜像再上传，同文件并发即丢更新）
  snailjob SnailJob 分组（部署前暂停任务、结束后恢复，两个任务共用一组会互相提前恢复）

键集相交 → 排队串行；不相交 → 并行。互斥下沉到这里，队列只认键不认业务。
"""
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from webapp import db as dbm
from webapp import lanes


class _DbCase(unittest.TestCase):
    """最小可用配置库：一个项目、两个环境（各绑不同主机）、若干服务"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "platform.db")
        dbm.init_db(self.db)
        now = int(time.time())
        self.proj = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=now))
        self.root = (self.tmp.replace("\\", "/") + "/repos").rstrip("/")
        self.s1 = self._server("10.0.0.1")
        self.s2 = self._server("10.0.0.2")
        self.prod = self._env("prod", self.root, "/opt/app/docker-compose.yml", [self.s1])
        self.test = self._env("test", self.root, "/opt/app/docker-compose.yml", [self.s2])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _server(self, host):
        return dbm.create_record(self.db, "servers", dict(
            host=host, ssh_port=22, ssh_user="root", nacos_ip="", remark="", enabled=1))

    def _env(self, name, project_root, compose_path, server_ids):
        env_id = dbm.create_record(self.db, "environments", dict(
            project_id=self.proj, name=name, is_default=1 if name == "prod" else 0,
            registry={}, nacos={}, mysql_snailjob={}, ssh={}, build={},
            deploy={"project_root": project_root},
            docker_compose_path=compose_path, maven_profile="", monitor_enabled=1))
        for i, sid in enumerate(server_ids):
            dbm.bind_server(self.db, env_id, sid, i)
        return env_id

    def _svc(self, name, **kw):
        fields = dict(project_id=self.proj, name=name, image_name=name,
                      service_name=name, docker_context="", dockerfile="",
                      build_order="[]", nacos_groups="[]", dubbo_services="[]",
                      snailjob_group="", health_url="", health_port=0,
                      log_error_keywords="[]", clear_log_dir="", git_repo="",
                      deploy_mode="container", enabled=1)
        fields.update(kw)
        return dbm.create_record(self.db, "services", fields)

    def _task(self, type_, ref_id, name, env="prod", **kw):
        return dbm.get_task(self.db, dbm.create_task(
            self.db, self.proj, type_, ref_id, name, env,
            kw.pop("version", "1.0.0"), "", 1, **kw))

    def _keys(self, task, extra=None):
        return lanes.task_lanes(self.db, task, extra)

    def _prefixed(self, keys, prefix):
        return {k for k in keys if k.startswith(prefix)}


class TestTargetLanes(_DbCase):
    """目标道复用提交互斥闸的同一套键：闸与队列必须说同一种语言"""

    def test_service_task_carries_its_service_lane(self):
        svc = self._svc("auth")
        keys = self._keys(self._task("project", svc, "auth"))
        self.assertIn(f"service:{svc}", keys)

    def test_schedule_created_task_uses_service_lane(self):
        """定时计划落库的任务 type 是 ref_type（service），不能落到自有命名空间"""
        svc = self._svc("auth")
        keys = self._keys(self._task("service", svc, "auth"))
        self.assertIn(f"service:{svc}", keys)

    def test_pipeline_task_covers_itself_and_all_members(self):
        auth, web = self._svc("auth"), self._svc("web")
        pl = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj, name="pl1", build_first=1,
            stages=[{"services": [{"service_id": auth}, {"service_id": web}]}]))
        keys = self._keys(self._task("pipeline", pl, "pl1"))
        self.assertIn(f"pipeline:{pl}", keys)
        self.assertIn(f"service:{auth}", keys)
        self.assertIn(f"service:{web}", keys)

    def test_same_target_different_env_share_target_lane(self):
        """目标是服务本身，跨环境同样只能有一个在跑（与互斥闸的环境维度互补）"""
        svc = self._svc("auth")
        self.assertEqual(self._prefixed(self._keys(self._task("project", svc, "auth", "prod")), "service:"),
                         self._prefixed(self._keys(self._task("project", svc, "auth", "test")), "service:"))

    def test_target_lane_matches_dedup_guard_keys(self):
        """队列键与提交闸键同源：两处算法分叉会让'能提交却永远排不上'或反之"""
        svc = self._svc("auth")
        self.assertEqual(self._prefixed(self._keys(self._task("project", svc, "auth")), "service:"),
                         dbm.task_target_keys(self.db, "project", svc))


class TestBuildWorkspaceLanes(_DbCase):
    """构建工作目录：同一份源码不能同时被两个任务 checkout + mvn clean"""

    def test_same_git_repo_shares_one_repo_lane(self):
        a = self._svc("auth", git_repo="saas-backend",
                      build_order=[{"path": "auth", "goal": "package"}])
        b = self._svc("gateway", git_repo="saas-backend",
                      build_order=[{"path": "gateway", "goal": "package"}])
        ka, kb = self._keys(self._task("project", a, "auth")), self._keys(self._task("project", b, "gateway"))
        self.assertEqual(self._prefixed(ka, "repo:"), self._prefixed(kb, "repo:"))
        self.assertEqual(len(self._prefixed(ka, "repo:")), 1)

    def test_different_git_repos_do_not_share_repo_lane(self):
        a = self._svc("auth", git_repo="repo-a")
        b = self._svc("web", git_repo="repo-b")
        self.assertFalse(self._prefixed(self._keys(self._task("project", a, "auth")), "repo:")
                         & self._prefixed(self._keys(self._task("project", b, "web")), "repo:"))

    def test_module_paths_are_lanes_of_their_own(self):
        """无 git_repo（人工同步的本地仓库）时，同模块目录仍必须互斥：mvn clean 会删同一个 target/"""
        a = self._svc("auth", build_order=[{"path": "shared-api", "goal": "package"}])
        b = self._svc("web", build_order=[{"path": "shared-api", "goal": "package"}])
        c = self._svc("job", build_order=[{"path": "other", "goal": "package"}])
        ka = self._keys(self._task("project", a, "auth"))
        kb = self._keys(self._task("project", b, "web"))
        kc = self._keys(self._task("project", c, "job"))
        self.assertEqual(self._prefixed(ka, "mod:"), self._prefixed(kb, "mod:"))
        self.assertEqual(len(self._prefixed(ka, "mod:")), 1)
        self.assertFalse(self._prefixed(ka, "mod:") & self._prefixed(kc, "mod:"))

    def test_pipeline_covers_member_repos(self):
        a = self._svc("auth", git_repo="repo-a")
        b = self._svc("web", git_repo="repo-b")
        pl = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj, name="pl1", build_first=1,
            stages=[{"services": [{"service_id": a}, {"service_id": b}]}]))
        keys = self._keys(self._task("pipeline", pl, "pl1"))
        self.assertEqual(len(self._prefixed(keys, "repo:")), 2)

    def test_skip_build_drops_workspace_lanes(self):
        """"仅重启镜像不改代码"的发布（--skip-build）不碰源码目录，不该被构建道挡住"""
        svc = self._svc("auth", git_repo="repo-a")
        task = self._task("project", svc, "auth")
        keys = self._keys(task, extra=["--skip-build"])
        self.assertFalse(self._prefixed(keys, "repo:"))
        self.assertFalse(self._prefixed(keys, "mod:"))
        self.assertTrue(self._prefixed(keys, "compose:"))

    def test_restart_never_builds(self):
        svc = self._svc("auth", git_repo="repo-a")
        keys = self._keys(self._task("restart", svc, "auth", version=""))
        self.assertFalse(self._prefixed(keys, "repo:"))
        self.assertFalse(self._prefixed(keys, "mod:"))

    def test_remote_git_url_lane_matches_its_clone_dir(self):
        """远程 URL clone 到 project_root/末段目录名：与写同名本地路径的服务必须同一条道"""
        url = self._svc("auth", git_repo="https://git.example.com/team/saas-backend.git")
        local = self._svc("web", git_repo="saas-backend")
        self.assertEqual(self._prefixed(self._keys(self._task("project", url, "auth")), "repo:"),
                         self._prefixed(self._keys(self._task("project", local, "web")), "repo:"))

    def test_repo_lane_is_case_and_separator_insensitive(self):
        """同一目录的两种写法必须落到同一个键，否则互斥静默失效"""
        a = self._svc("auth", git_repo="Repo/Sub")
        b = self._svc("web", git_repo="repo\\sub")
        self.assertEqual(self._prefixed(self._keys(self._task("project", a, "auth")), "repo:"),
                         self._prefixed(self._keys(self._task("project", b, "web")), "repo:"))

    def test_relative_project_root_is_stable_across_cwd(self):
        """键在平台进程里算、子进程在别的工作目录里跑：相对根必须锚定成绝对路径，
        否则同一目录在不同调用点算出不同键，互斥静默失效"""
        svc = self._svc("auth", git_repo="x")
        dbm.create_record(self.db, "environments", dict(
            project_id=self.proj, name="rel", is_default=0, registry={}, nacos={},
            mysql_snailjob={}, ssh={}, build={}, deploy={"project_root": "repos"},
            docker_compose_path="", maven_profile="", monitor_enabled=1))
        task = dbm.get_task(self.db, dbm.create_task(self.db, self.proj, "project", svc,
                                                     "auth", "rel", "1.0.0", "", 1))
        here = os.getcwd()
        try:
            keys_here = self._keys(task)
            os.chdir(tempfile.gettempdir())
            keys_there = self._keys(task)
        finally:
            os.chdir(here)
        repo_keys = self._prefixed(keys_here, "repo:")
        self.assertEqual(len(repo_keys), 1)
        self.assertEqual(keys_here, keys_there)
        self.assertTrue(os.path.isabs(next(iter(repo_keys)).split(":", 1)[1]))

    def test_no_project_root_config_yields_no_workspace_lanes(self):
        """没配 project_root 就无从判断目录是否相同：只保留目标道，不得凭空造键"""
        svc = self._svc("auth", git_repo="repo-a")
        dbm.update_record(self.db, "environments", self.prod,
                          {"deploy": {"project_root": ""}})
        keys = self._keys(self._task("project", svc, "auth"))
        self.assertFalse(self._prefixed(keys, "repo:"))
        self.assertIn(f"service:{svc}", keys)


class TestComposeLanes(_DbCase):
    """compose 文件：平台要改写它（换镜像 tag），同一文件同机并发即丢更新"""

    def test_container_service_holds_its_env_hosts_compose_lane(self):
        svc = self._svc("auth")
        keys = self._keys(self._task("project", svc, "auth"))
        self.assertEqual(self._prefixed(keys, "compose:"),
                         {"compose:10.0.0.1:/opt/app/docker-compose.yml"})

    def test_second_host_adds_its_own_lane(self):
        svc = self._svc("auth")
        dbm.bind_server(self.db, self.prod, self.s2, 1)
        self.assertEqual(len(self._prefixed(self._keys(self._task("project", svc, "auth")), "compose:")), 2)

    def test_different_hosts_same_path_are_different_lanes(self):
        """prod 与 test 各在一台机器上，用同一个 compose 路径：可以并行"""
        svc = self._svc("auth")
        prod_keys = self._keys(self._task("project", svc, "auth", "prod"))
        test_keys = self._keys(self._task("project", svc, "auth", "test"))
        self.assertFalse(self._prefixed(prod_keys, "compose:") & self._prefixed(test_keys, "compose:"))

    def test_same_host_and_path_is_one_lane(self):
        """两个环境共用同一台机同一个 compose 文件：物理上就是同一份文件，必须串行"""
        self._env("gray", self.root, "/opt/app/docker-compose.yml", [self.s1])
        svc = self._svc("auth")
        prod_keys = self._keys(self._task("project", svc, "auth", "prod"))
        gray_keys = dbm.get_task(self.db, dbm.create_task(
            self.db, self.proj, "project", svc, "auth", "gray", "1.0.0", "", 1))
        self.assertTrue(prod_keys & self._keys(gray_keys))

    def test_non_container_modes_have_no_compose_lane(self):
        """WAR 有自己的远端 gate.lock，进程族按 systemd 单元操作：都不碰 compose"""
        for mode in ("war_tomcat", "jar_service", "static_archive"):
            svc = self._svc(f"s-{mode}", deploy_mode=mode)
            self.assertFalse(self._prefixed(self._keys(self._task("project", svc, "s")), "compose:"),
                             f"{mode} 不该占 compose 道")

    def test_missing_compose_path_yields_no_compose_lane(self):
        svc = self._svc("auth")
        dbm.update_record(self.db, "environments", self.prod, {"docker_compose_path": ""})
        keys = self._keys(self._task("project", svc, "auth"))
        self.assertFalse(self._prefixed(keys, "compose:"))
        self.assertIn(f"service:{svc}", keys)

    def test_restart_uses_docker_restart_not_compose(self):
        """restart 走 docker restart（不改 compose 文件），不该被 compose 道挡住"""
        svc = self._svc("auth")
        keys = self._keys(self._task("restart", svc, "auth", version=""))
        self.assertFalse(self._prefixed(keys, "compose:"))
        self.assertIn(f"service:{svc}", keys)


class TestSnailjobLanes(_DbCase):
    def test_group_is_a_lane(self):
        svc = self._svc("auth", snailjob_group="hqb-job")
        self.assertEqual(self._prefixed(self._keys(self._task("project", svc, "auth")), "snailjob:"),
                         {"snailjob:hqb-job"})

    def test_shared_group_serializes_two_services(self):
        a = self._svc("auth", snailjob_group="same")
        b = self._svc("web", snailjob_group="same")
        self.assertTrue(self._keys(self._task("project", a, "auth"))
                        & self._keys(self._task("project", b, "web")))

    def test_no_group_no_lane(self):
        svc = self._svc("auth")
        self.assertFalse(self._prefixed(self._keys(self._task("project", svc, "auth")), "snailjob:"))


class TestLaneRobustness(_DbCase):
    def test_lanes_are_never_empty(self):
        """空键集 = 无约束并行，任何解析失败都必须退回目标道"""
        for task in (self._task("project", 9999, "ghost"),
                     self._task("pipeline", 9999, "ghost-pl"),
                     self._task("legacy", 5, "legacy-one"),
                     self._task("project", self._svc("auth"), "auth", env="不存在的環境")):
            self.assertTrue(self._keys(task), f"{task['type']}/{task['ref_id']} 键集为空")

    def test_unknown_type_keeps_its_own_namespace(self):
        """未知 type（历史遗留/测试构造）之间互斥，但不与 svc/pl 键空间相互占用"""
        t1 = self._task("legacy", 7, "one")
        t2 = dict(t1, id=999, name="two")  # 同目标的第二条建不出来（互斥闸），只取键集语义
        self.assertTrue(self._keys(t1) & self._keys(t2))
        svc = self._svc("auth")
        self.assertFalse(self._keys(t1) & self._keys(self._task("project", svc, "auth")))

    def test_deleted_env_falls_back_to_default_env(self):
        """任务 env 为空时按默认环境解析（与 export_runtime 同口径）"""
        svc = self._svc("auth")
        task = self._task("project", svc, "auth", env="")
        self.assertIn("compose:10.0.0.1:/opt/app/docker-compose.yml", self._keys(task))


# ---------- 执行队列 ----------

class _Runner:
    """可控假执行体：记录同时在跑的任务数，按事件门放行"""

    def __init__(self, hold=0.05):
        self.hold = hold
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.order = []
        self.gate = threading.Event()
        self.gate.set()

    def make(self, task_id, block_on_gate=False):
        def run():
            with self.lock:
                self.inside += 1
                self.peak = max(self.peak, self.inside)
                self.order.append(task_id)
            if block_on_gate:
                self.gate.wait(5)
            time.sleep(self.hold)
            with self.lock:
                self.inside -= 1
        return run


class TestTaskQueue(unittest.TestCase):
    def setUp(self):
        self.q = None

    def tearDown(self):
        if self.q is not None:
            self.q.shutdown()

    def _queue(self, size):
        self.q = lanes.TaskQueue(size)
        return self.q

    def _wait_idle(self, q, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if q.busy_count() == 0 and q.pending_count() == 0:
                return True
            time.sleep(0.01)
        return False

    def test_disjoint_lanes_run_concurrently(self):
        q = self._queue(3)
        barrier = threading.Barrier(2, timeout=5)

        def rendezvous():
            def run():
                barrier.wait()
            return run

        q.submit(1, {"service:1"}, rendezvous())
        q.submit(2, {"service:2"}, rendezvous())
        self.assertTrue(self._wait_idle(q), "两个不同道的任务应会合后各自结束")

    def test_shared_lane_never_overlaps(self):
        r = _Runner()
        q = self._queue(3)
        r.gate.clear()
        q.submit(1, {"repo:x"}, r.make(1, block_on_gate=True))
        q.submit(2, {"repo:x"}, r.make(2))
        deadline = time.time() + 3
        while r.order != [1] and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(r.order, [1], "第二个任务不得与第一个同时在跑")
        self.assertEqual(q.pending_count(), 1, "同道任务应在道队列里等待，而不是占着执行槽")
        r.gate.set()
        self.assertTrue(self._wait_idle(q))
        self.assertEqual(r.order, [1, 2])
        self.assertEqual(r.peak, 1)

    def test_blocked_task_does_not_eat_a_slot(self):
        """槽位有限时，等同道的任务不得占着执行槽：否则并行池退化回 FIFO，
        一条繁忙道就能把后面所有道堵死（正是本次要消灭的"一堵全停"）"""
        r = _Runner()
        q = self._queue(2)
        r.gate.clear()
        q.submit(1, {"repo:x"}, r.make(1, block_on_gate=True))
        q.submit(2, {"repo:x"}, r.make(2))
        q.submit(3, {"repo:y"}, r.make(3))
        deadline = time.time() + 3
        while 3 not in r.order and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn(3, r.order, "另一条道的任务必须能绕开同道队列用上空闲槽位")
        r.gate.set()
        self.assertTrue(self._wait_idle(q))
        self.assertEqual(r.peak, 2)

    def test_pool_size_is_a_hard_cap(self):
        r = _Runner(hold=0.1)
        q = self._queue(2)
        for i in range(6):
            q.submit(i, {f"service:{i}"}, r.make(i))
        self.assertTrue(self._wait_idle(q))
        self.assertEqual(r.peak, 2)
        self.assertEqual(len(r.order), 6)

    def test_same_lane_preserves_submission_order(self):
        r = _Runner(hold=0.02)
        q = self._queue(3)
        for i in (1, 2, 3):
            q.submit(i, {"repo:x"}, r.make(i))
        self.assertTrue(self._wait_idle(q))
        self.assertEqual(r.order, [1, 2, 3])

    def test_exception_releases_lane(self):
        q = self._queue(2)
        done = threading.Event()

        def boom():
            raise RuntimeError("构建子进程异常")

        def ok():
            done.set()

        q.submit(1, {"repo:x"}, boom)
        q.submit(2, {"repo:x"}, ok)
        self.assertTrue(done.wait(5), "前一个任务异常后同道任务必须能继续")

    def test_cancel_before_start_prevents_execution(self):
        r = _Runner()
        q = self._queue(1)
        r.gate.clear()
        q.submit(1, {"repo:x"}, r.make(1, block_on_gate=True))
        item = q.submit(2, {"repo:y"}, r.make(2))
        self.assertTrue(item.cancel())
        self.assertFalse(item.cancel(), "重复取消不得算成功")
        r.gate.set()
        self.assertTrue(self._wait_idle(q))
        self.assertNotIn(2, r.order)

    def test_cancel_running_task_reports_false(self):
        r = _Runner()
        q = self._queue(1)
        item = q.submit(1, {"repo:x"}, r.make(1))
        deadline = time.time() + 3
        while not r.order and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(item.cancel(), "已开始的任务由 kill_task 走进程树终止")

    def test_pool_size_from_env(self):
        with mock.patch.dict(os.environ, {"PLATFORM_TASK_CONCURRENCY": "5"}):
            self.assertEqual(lanes.pool_size_from_env(), 5)

    def test_pool_size_defaults_to_three(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLATFORM_TASK_CONCURRENCY", None)
            self.assertEqual(lanes.pool_size_from_env(), 3)

    def test_pool_size_rejects_garbage_and_zero(self):
        for raw in ("0", "-2", "", "abc", "1e9"):
            with mock.patch.dict(os.environ, {"PLATFORM_TASK_CONCURRENCY": raw}):
                self.assertGreaterEqual(lanes.pool_size_from_env(), 1, raw)


class TestSubmitTaskWiring(_DbCase):
    """submit_task 必须把道算好交给队列，并保持排队期可终止的既有契约"""

    def setUp(self):
        super().setUp()
        from webapp import task_manager
        self.tm = task_manager
        self.q = lanes.TaskQueue(2)
        self.gate = threading.Event()
        self.gate.set()
        self.ran = []

        def fake_run(db_path, task, extra, log_dir=None):
            self.ran.append(task["id"])
            self.gate.wait(5)

        patchers = [mock.patch.object(task_manager, "_queue", self.q),
                    mock.patch.object(task_manager, "_run_task", fake_run)]
        # 假执行体不会 pop 注册表，而任务号每个用例都从 1 重来：逐用例清空再还原，
        # 否则上个用例遗留的占位会让 kill_task 找到别的队列里的同名任务
        self._saved_registry = dict(task_manager._task_registry)
        task_manager._task_registry.clear()
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.gate.set()
        self.q.shutdown()
        from webapp import task_manager
        task_manager._task_registry.clear()
        task_manager._task_registry.update(self._saved_registry)
        super().tearDown()

    def _hold_lane(self):
        """占住执行道：假执行体进门后等 gate，用于构造"一个在跑、一个在等道" """
        self.gate.clear()
        return self.ran

    def _submit(self, task, extra=None):
        self.tm.submit_task(self.db, task, extra or [], log_dir=self.tmp)
        return task

    def _shared_repo_task(self):
        """与 auth 共用构建目录（repo-a）的另一个服务：目标道不同、构建道相同"""
        web = self._svc("web", git_repo="repo-a")
        return dbm.get_task(self.db, dbm.create_task(self.db, self.proj, "project", web,
                                                     "web", "prod", "1.0.0", "", 1))

    def _wait_busy(self, timeout=3.0):
        deadline = time.time() + timeout
        while self.q.busy_count() == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreater(self.q.busy_count(), 0, "第一个任务应已占用执行槽")
        return deadline

    def _wait_idle(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.q.busy_count() == 0 and self.q.pending_count() == 0:
                return True
            time.sleep(0.01)
        return False

    def test_submit_passes_task_lanes_to_queue(self):
        svc = self._svc("auth", git_repo="repo-a")
        task = self._task("project", svc, "auth")
        expected = lanes.task_lanes(self.db, task, [])
        seen = {}
        real_submit = self.q.submit

        def spy(task_id, keys, fn):
            seen[task_id] = keys
            return real_submit(task_id, keys, fn)

        with mock.patch.object(self.q, "submit", side_effect=spy):
            self._submit(task)
        self.assertEqual(seen[task["id"]], expected)

    def test_blocked_task_stays_pending_in_db(self):
        """排队中的任务状态仍是 pending：大屏与 409 判定都以活动状态为准，
        把"等道"显示成"运行中"会让操作员误判进度"""
        ran = self._hold_lane()
        a = self._submit(self._task("project", self._svc("auth", git_repo="repo-a"), "auth"))
        self._wait_busy()
        # 同服务第二个任务提交不了（互斥闸），换一个共用构建目录的服务来排队
        b = self._submit(self._shared_repo_task())
        self.assertEqual(self.q.pending_count(), 1)
        self.assertEqual(dbm.get_task(self.db, b["id"])["status"], "pending")
        self.gate.set()
        self.assertTrue(self._wait_idle())
        self.assertEqual(ran, [a["id"], b["id"]], "同道任务按提交顺序跑完")

    def test_kill_queued_task_releases_lane_and_marks_failed(self):
        from webapp import task_manager
        ran = self._hold_lane()
        self._submit(self._task("project", self._svc("auth", git_repo="repo-a"), "auth"))
        self._wait_busy()
        b = self._submit(self._shared_repo_task())
        self.assertTrue(task_manager.kill_task(b["id"], self.db))
        self.assertEqual(self.q.pending_count(), 0)
        self.assertEqual(dbm.get_task(self.db, b["id"])["status"], "failed")
        self.gate.set()
        self.assertTrue(self._wait_idle())
        self.assertNotIn(b["id"], ran, "排队中被终止的任务不得再被执行")

    def test_serial_mode_when_concurrency_is_one(self):
        """PLATFORM_TASK_CONCURRENCY=1 必须复现旧的单 worker 串行语义"""
        r = _Runner()
        q = lanes.TaskQueue(1)
        self.addCleanup(q.shutdown)
        r.gate.clear()
        q.submit(1, {"service:1"}, r.make(1, block_on_gate=True))
        q.submit(2, {"service:2"}, r.make(2))
        deadline = time.time() + 2
        while r.order != [1] and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(r.order, [1])
        r.gate.set()
        deadline = time.time() + 3
        while q.busy_count() + q.pending_count() > 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(r.peak, 1)


class TestRuntimeExportIsolation(_DbCase):
    """db 模式导出的运行时 YAML 落在 data/runtime/p{pid}/{env}/：并行后同项目两个任务同目录"""

    def test_export_writes_by_atomic_replace(self):
        """就地截断写会让另一任务读到半份 YAML（解析失败或按旧配置发布），必须临时文件 + replace"""
        from webapp import exporter
        replaced = []
        real_replace = os.replace

        def spy(src, dst, *a, **kw):
            replaced.append(os.path.basename(str(dst)))
            return real_replace(src, dst, *a, **kw)

        env = dbm.get_record(self.db, "environments", self.prod)
        target = os.path.join(self.tmp, "runtime", "p1", "prod")
        with mock.patch("webapp.exporter.os.replace", side_effect=spy):
            exporter.export_runtime(self.db, self.proj, env["name"], target)
        self.assertEqual(sorted(replaced),
                         ["deploy.yaml", "pipelines.yaml", "projects.yaml", "servers.yaml"])
        leftovers = [n for n in os.listdir(target) if not n.endswith(".yaml")]
        self.assertEqual(leftovers, [], "临时文件必须已被改名带走")


if __name__ == "__main__":
    unittest.main()
