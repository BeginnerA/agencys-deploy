import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import resource_locks  # noqa: E402


def _process_lock_worker(lock_dir, key, attempting_path, started_path, release_path,
                         shared=False):
    Path(attempting_path).write_text("waiting", encoding="utf-8")
    with resource_locks.resource_lock(key, data_dir=lock_dir, shared=shared):
        Path(started_path).write_text("acquired", encoding="utf-8")
        deadline = time.time() + 5
        while not Path(release_path).exists() and time.time() < deadline:
            time.sleep(0.01)


class ResourceLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self.tmp.name)
        self.threads = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for thread, _ in self.threads:
            thread.join(3)
        self.tmp.cleanup()

    def _start_thread(self, target, *args):
        errors = []

        def run():
            try:
                target(*args)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        self.threads.append((thread, errors))
        thread.start()
        return thread, errors

    def _assert_finished(self, *workers):
        for thread, errors in workers:
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def _release_on_teardown(self, event):
        self.releases.append(event)
        return event

    def _wait_for_path(self, path, timeout=3):
        deadline = time.time() + timeout
        while not path.exists() and time.time() < deadline:
            time.sleep(0.01)
        return path.exists()

    def test_same_key_serializes_threads(self):
        first_entered = threading.Event()
        release_first = self._release_on_teardown(threading.Event())
        second_entered = threading.Event()
        order = []

        def first():
            with resource_locks.resource_lock("module:shared", data_dir=self.lock_dir):
                order.append("first")
                first_entered.set()
                release_first.wait(2)

        def second():
            with resource_locks.resource_lock("module:shared", data_dir=self.lock_dir):
                order.append("second")
                second_entered.set()

        t1 = self._start_thread(first)
        self.assertTrue(first_entered.wait(1))
        t2 = self._start_thread(second)
        self.assertFalse(second_entered.wait(0.1))
        release_first.set()
        self._assert_finished(t1, t2)
        self.assertEqual(order, ["first", "second"])

    def test_different_keys_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=2)

        def worker(key):
            with resource_locks.resource_lock(key, data_dir=self.lock_dir):
                barrier.wait()

        workers = [self._start_thread(worker, key)
                   for key in ("module:a", "module:b")]
        self._assert_finished(*workers)

    def test_shared_repo_lock_allows_readers_and_blocks_writer(self):
        barrier = threading.Barrier(3, timeout=2)
        release_readers = self._release_on_teardown(threading.Event())
        writer_entered = threading.Event()

        def reader():
            with resource_locks.resource_lock("repo:shared", data_dir=self.lock_dir,
                                              shared=True):
                barrier.wait()
                release_readers.wait(2)

        def writer():
            with resource_locks.resource_lock("repo:shared", data_dir=self.lock_dir):
                writer_entered.set()

        readers = [self._start_thread(reader) for _ in range(2)]
        barrier.wait()
        writer_worker = self._start_thread(writer)
        self.assertFalse(writer_entered.wait(0.1))
        release_readers.set()
        self._assert_finished(*readers, writer_worker)
        self.assertTrue(writer_entered.is_set())

    def test_shared_repo_lock_allows_process_readers_and_blocks_writer(self):
        ctx = multiprocessing.get_context("spawn")
        key = "repo:process-shared"
        processes = []
        releases = []
        try:
            for name in ("reader-a", "reader-b"):
                release = self.lock_dir / f"{name}.release"
                releases.append(release)
                process = ctx.Process(
                    target=_process_lock_worker,
                    args=(str(self.lock_dir), key,
                          str(self.lock_dir / f"{name}.attempting"),
                          str(self.lock_dir / f"{name}.started"),
                          str(release), True))
                process.start()
                processes.append((name, process))
                self.assertTrue(self._wait_for_path(self.lock_dir / f"{name}.started"))

            release = self.lock_dir / "writer.release"
            release.write_text("go", encoding="utf-8")
            writer = ctx.Process(
                target=_process_lock_worker,
                args=(str(self.lock_dir), key,
                      str(self.lock_dir / "writer.attempting"),
                      str(self.lock_dir / "writer.started"), str(release)))
            writer.start()
            processes.append(("writer", writer))
            self.assertTrue(self._wait_for_path(self.lock_dir / "writer.attempting"))
            self.assertFalse(self._wait_for_path(self.lock_dir / "writer.started", 0.15))

            for release in releases:
                release.write_text("go", encoding="utf-8")
            writer.join(3)
            self.assertFalse(writer.is_alive())
            self.assertEqual(writer.exitcode, 0)
            self.assertTrue((self.lock_dir / "writer.started").exists())
        finally:
            for release in releases:
                release.write_text("go", encoding="utf-8")
            for name, process in processes:
                release = self.lock_dir / f"{name}.release"
                release.write_text("go", encoding="utf-8")
                process.join(3)
                if process.is_alive():
                    process.terminate()
                    process.join(2)

    def test_parent_module_blocks_descendant(self):
        root = Path(self.tmp.name) / "repos"
        parent_entered = threading.Event()
        release_parent = self._release_on_teardown(threading.Event())
        child_entered = threading.Event()

        def parent():
            with resource_locks.module_lock(root, "shared", data_dir=self.lock_dir):
                parent_entered.set()
                release_parent.wait(2)

        def child():
            with resource_locks.module_lock(root, "shared/api", data_dir=self.lock_dir):
                child_entered.set()

        parent_worker = self._start_thread(parent)
        self.assertTrue(parent_entered.wait(1))
        child_worker = self._start_thread(child)
        self.assertFalse(child_entered.wait(0.1))
        release_parent.set()
        self._assert_finished(parent_worker, child_worker)
        self.assertTrue(child_entered.is_set())

    def test_sibling_modules_keep_ancestor_guard_shared(self):
        root = Path(self.tmp.name) / "repos"
        barrier = threading.Barrier(2, timeout=2)

        def worker(path):
            with resource_locks.module_lock(root, path, data_dir=self.lock_dir):
                barrier.wait()

        workers = [self._start_thread(worker, path) for path in ("a", "b")]
        self._assert_finished(*workers)

    def test_lock_is_released_after_exception(self):
        with self.assertRaises(RuntimeError):
            with resource_locks.resource_lock("module:boom", data_dir=self.lock_dir):
                raise RuntimeError("boom")
        acquired = threading.Event()

        def worker():
            with resource_locks.resource_lock("module:boom", data_dir=self.lock_dir):
                acquired.set()

        worker_thread = self._start_thread(worker)
        self._assert_finished(worker_thread)
        self.assertTrue(acquired.is_set())

    def test_same_key_blocks_across_processes(self):
        ctx = multiprocessing.get_context("spawn")
        first_attempting = self.lock_dir / "first.attempting"
        second_attempting = self.lock_dir / "second.attempting"
        first_started = self.lock_dir / "first.started"
        second_started = self.lock_dir / "second.started"
        first_release = self.lock_dir / "first.release"
        second_release = self.lock_dir / "second.release"
        second_release.write_text("go", encoding="utf-8")
        first = ctx.Process(target=_process_lock_worker,
                            args=(str(self.lock_dir), "compose:host:/path",
                                  str(first_attempting), str(first_started), str(first_release)))
        second = ctx.Process(target=_process_lock_worker,
                             args=(str(self.lock_dir), "compose:host:/path",
                                   str(second_attempting), str(second_started), str(second_release)))
        first.start()
        try:
            self.assertTrue(self._wait_for_path(first_started))
            second.start()
            self.assertTrue(self._wait_for_path(second_attempting))
            self.assertFalse(self._wait_for_path(second_started, 0.15))
            first_release.write_text("go", encoding="utf-8")
            second.join(3)
            self.assertFalse(second.is_alive())
            self.assertEqual(second.exitcode, 0)
            self.assertTrue(second_started.exists())
        finally:
            first_release.write_text("go", encoding="utf-8")
            for process in (first, second):
                if process.pid is not None:
                    process.join(3)
                    if process.is_alive():
                        process.terminate()
                        process.join(2)


class ResourceKeyTest(unittest.TestCase):
    def test_repo_key_matches_remote_clone_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote = resource_locks.repo_lock_key(
                tmp, "https://git.example.com/team/saas-backend.git")
            local = resource_locks.repo_lock_key(tmp, "saas-backend")
            self.assertEqual(remote, local)

    def test_repo_key_removes_only_exact_git_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            for remote_name, local_name in (("api.git", "api"),
                                            ("widget", "widget"),
                                            ("git", "git"),
                                            ("logging.git", "logging")):
                remote = resource_locks.repo_lock_key(
                    tmp, f"https://git.example.com/team/{remote_name}")
                local = resource_locks.repo_lock_key(tmp, local_name)
                self.assertEqual(remote, local, remote_name)

    def test_repo_key_uses_host_path_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            upper = resource_locks.repo_lock_key(tmp, "Repo/Sub")
            backslash = resource_locks.repo_lock_key(tmp, "repo\\sub")
            if os.name == "nt":
                self.assertEqual(upper, backslash)
            else:
                self.assertNotEqual(upper, backslash)

    def test_repo_key_ignores_equivalent_relative_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = resource_locks.repo_lock_key(tmp, "repo-sub")
            for alias in ("repo-sub/", "repo-sub/.", "repo-sub//", "./repo-sub"):
                self.assertEqual(resource_locks.repo_lock_key(tmp, alias), base)

    def test_relative_project_root_is_stable_across_cwd(self):
        here = os.getcwd()
        try:
            key_here = resource_locks.repo_lock_key("repos", "repo-sub")
            os.chdir(tempfile.gettempdir())
            key_there = resource_locks.repo_lock_key("repos", "repo-sub")
        finally:
            os.chdir(here)
        self.assertEqual(key_here, key_there)
        self.assertTrue(os.path.isabs(key_here.split(":", 1)[1]))

    def test_module_key_uses_absolute_module_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = resource_locks.module_lock_key(tmp, "shared-api")
            self.assertTrue(key.startswith("module:"))
            self.assertIn("shared-api", key)
            self.assertNotIn("..", key)

    def test_artifact_key_uses_final_output_identity(self):
        self.assertEqual(
            resource_locks.artifact_lock_key("svc-a", {"image_name": "shared"}),
            resource_locks.artifact_lock_key("svc-b", {"image_name": "shared"}))
        self.assertEqual(
            resource_locks.artifact_lock_key("site-a", {"deploy_mode": "static_archive",
                                                         "zip_prefix": "shared"}),
            resource_locks.artifact_lock_key("site-b", {"deploy_mode": "static_archive",
                                                         "zip_prefix": "shared"}))
        self.assertNotEqual(
            resource_locks.artifact_lock_key("svc-a", {"image_name": "a"}),
            resource_locks.artifact_lock_key("svc-b", {"image_name": "b"}))

    def test_compose_key_includes_host_and_path(self):
        self.assertEqual(
            resource_locks.compose_lock_key("10.0.0.1", "/opt/app/docker-compose.yml"),
            "compose:10.0.0.1:/opt/app/docker-compose.yml")

    def test_compose_key_normalizes_host_case_and_trailing_dot(self):
        self.assertEqual(
            resource_locks.compose_lock_key(" Deploy.Example.COM. ", "/opt/app/compose.yml"),
            resource_locks.compose_lock_key("deploy.example.com", "/opt/app/compose.yml"))

    def test_compose_key_normalizes_remote_posix_path(self):
        self.assertEqual(
            resource_locks.compose_lock_key("10.0.0.1", "/opt/app/../app//compose.yml"),
            resource_locks.compose_lock_key("10.0.0.1", "/opt/app/compose.yml"))

    def test_compose_key_treats_double_slash_root_as_root(self):
        self.assertEqual(
            resource_locks.compose_lock_key("10.0.0.1", "//opt/app//compose.yml"),
            resource_locks.compose_lock_key("10.0.0.1", "/opt/app/compose.yml"))


if __name__ == "__main__":
    unittest.main()
