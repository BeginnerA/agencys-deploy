"""任务并行：目标互斥与跨任务 SnailJob 互斥

仓库、模块和 Compose 锁只覆盖实际共享操作，不能再把不同服务整任务排队。
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
        svc = self._svc("auth")
        prod = self._prefixed(self._keys(self._task("project", svc, "auth", "prod")), "service:")
        test = self._prefixed(self._keys(self._task("project", svc, "auth", "test")), "service:")
        self.assertEqual(prod, test)

    def test_target_lane_matches_dedup_guard_keys(self):
        svc = self._svc("auth")
        self.assertEqual(self._prefixed(self._keys(self._task("project", svc, "auth")), "service:"),
                         dbm.task_target_keys(self.db, "project", svc))


class TestOperationResourcesDoNotBlockTaskStart(_DbCase):
    def test_shared_repo_module_and_compose_do_not_block_other_service(self):
        a = self._svc("auth", git_repo="saas-backend",
                      build_order=[{"path": "shared-api", "goal": "install"},
                                   {"path": "auth", "goal": "package"}])
        b = self._svc("web", git_repo="saas-backend",
                      build_order=[{"path": "shared-api", "goal": "install"},
                                   {"path": "web", "goal": "package"}])
        ta = self._task("project", a, "auth")
        tb = self._task("project", b, "web")
        self.assertFalse(lanes.task_lanes(self.db, ta) & lanes.task_lanes(self.db, tb))

    def test_task_lanes_never_contain_operation_resource_keys(self):
        svc = self._svc("auth", git_repo="repo-a",
                        build_order=[{"path": "shared", "goal": "package"}])
        keys = self._keys(self._task("project", svc, "auth"))
        for prefix in ("repo:", "mod:", "compose:"):
            self.assertFalse(self._prefixed(keys, prefix), prefix)

    def test_skip_build_and_restart_keep_only_target_lanes(self):
        svc = self._svc("auth", git_repo="repo-a")
        restart_svc = self._svc("restart-auth", git_repo="repo-a")
        skip_keys = self._keys(self._task("project", svc, "auth"), ["--skip-build"])
        restart_keys = self._keys(self._task("restart", restart_svc, "restart-auth", version=""))
        self.assertEqual(skip_keys, {f"service:{svc}"})
        self.assertEqual(restart_keys, {f"service:{restart_svc}"})


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

    def test_skip_build_keeps_snailjob_lane(self):
        svc = self._svc("auth", snailjob_group="same")
        keys = self._keys(self._task("project", svc, "auth"), ["--skip-build"])
        self.assertEqual(self._prefixed(keys, "snailjob:"), {"snailjob:same"})

    def test_skip_snailjob_drops_snailjob_lane(self):
        svc = self._svc("auth", snailjob_group="same")
        keys = self._keys(self._task("project", svc, "auth"), ["--skip-snailjob"])
        self.assertFalse(self._prefixed(keys, "snailjob:"))

    def test_pipeline_without_build_first_keeps_snailjob_lane(self):
        svc = self._svc("auth", snailjob_group="same")
        pl = dbm.create_record(self.db, "pipelines", dict(
            project_id=self.proj, name="pl1", build_first=0,
            stages=[{"services": [{"service_id": svc}]}]))
        keys = self._keys(self._task("pipeline", pl, "pl1"))
        self.assertEqual(self._prefixed(keys, "snailjob:"), {"snailjob:same"})


class TestLaneRobustness(_DbCase):
    def test_lanes_are_never_empty(self):
        for task in (self._task("project", 9999, "ghost"),
                     self._task("pipeline", 9999, "ghost-pl"),
                     self._task("legacy", 5, "legacy-one"),
                     self._task("project", self._svc("auth"), "auth", env="不存在的環境")):
            self.assertTrue(self._keys(task), f"{task['type']}/{task['ref_id']} 键集为空")

    def test_unknown_type_keeps_its_own_namespace(self):
        t1 = self._task("legacy", 7, "one")
        t2 = dict(t1, id=999, name="two")
        self.assertTrue(self._keys(t1) & self._keys(t2))
        svc = self._svc("auth")
        self.assertFalse(self._keys(t1) & self._keys(self._task("project", svc, "auth")))


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
        q.submit(1, {"shared:x"}, r.make(1, block_on_gate=True))
        q.submit(2, {"shared:x"}, r.make(2))
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
        r = _Runner()
        q = self._queue(2)
        r.gate.clear()
        q.submit(1, {"shared:x"}, r.make(1, block_on_gate=True))
        q.submit(2, {"shared:x"}, r.make(2))
        q.submit(3, {"shared:y"}, r.make(3))
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
            q.submit(i, {"shared:x"}, r.make(i))
        self.assertTrue(self._wait_idle(q))
        self.assertEqual(r.order, [1, 2, 3])

    def test_exception_releases_lane(self):
        q = self._queue(2)
        done = threading.Event()

        def boom():
            raise RuntimeError("构建子进程异常")

        def ok():
            done.set()

        q.submit(1, {"shared:x"}, boom)
        q.submit(2, {"shared:x"}, ok)
        self.assertTrue(done.wait(5), "前一个任务异常后同道任务必须能继续")

    def test_cancel_before_start_prevents_execution(self):
        r = _Runner()
        q = self._queue(1)
        r.gate.clear()
        q.submit(1, {"shared:x"}, r.make(1, block_on_gate=True))
        item = q.submit(2, {"shared:y"}, r.make(2))
        self.assertTrue(item.cancel())
        self.assertFalse(item.cancel(), "重复取消不得算成功")
        r.gate.set()
        self.assertTrue(self._wait_idle(q))
        self.assertNotIn(2, r.order)

    def test_cancel_running_task_reports_false(self):
        r = _Runner()
        q = self._queue(1)
        item = q.submit(1, {"shared:x"}, r.make(1))
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
        self.gate.clear()
        return self.ran

    def _submit(self, task, extra=None):
        self.tm.submit_task(self.db, task, extra or [], log_dir=self.tmp)
        return task

    def _shared_snailjob_task(self):
        web = self._svc("web", snailjob_group="same")
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
        ran = self._hold_lane()
        a = self._submit(self._task("project", self._svc("auth", snailjob_group="same"), "auth"))
        self._wait_busy()
        b = self._submit(self._shared_snailjob_task())
        self.assertEqual(self.q.pending_count(), 1)
        self.assertEqual(dbm.get_task(self.db, b["id"])["status"], "pending")
        self.gate.set()
        self.assertTrue(self._wait_idle())
        self.assertEqual(ran, [a["id"], b["id"]], "同道任务按提交顺序跑完")

    def test_kill_queued_task_releases_lane_and_marks_failed(self):
        from webapp import task_manager
        ran = self._hold_lane()
        self._submit(self._task("project", self._svc("auth", snailjob_group="same"), "auth"))
        self._wait_busy()
        b = self._submit(self._shared_snailjob_task())
        self.assertTrue(task_manager.kill_task(b["id"], self.db))
        self.assertEqual(self.q.pending_count(), 0)
        self.assertEqual(dbm.get_task(self.db, b["id"])["status"], "failed")
        self.gate.set()
        self.assertTrue(self._wait_idle())
        self.assertNotIn(b["id"], ran, "排队中被终止的任务不得再被执行")

    def test_serial_mode_when_concurrency_is_one(self):
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
