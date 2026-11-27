import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build  # noqa: E402
import resource_locks  # noqa: E402


class BuildResourceLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.threads = []
        self.root = Path(self.tmp.name) / "repos"
        self.root.mkdir()
        self.data_dir = Path(self.tmp.name) / "data"
        patcher = mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": str(self.data_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for thread in self.threads:
            thread.join(3)

    def _start(self, fn, errors):
        started = threading.Event()

        def run():
            started.set()
            try:
                fn()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(started.wait(1))
        return thread

    def _join(self, thread, errors):
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_git_prepare_waits_for_exclusive_repo_lock(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        proj = {"git_repo": "repo", "git_branch": "main", "git_pull_before_build": 1}
        errors = []
        with mock.patch.object(build.subprocess, "run") as run, \
                mock.patch.object(build, "log"):
            run.return_value = mock.Mock(returncode=0, stdout="a" * 40 + "\n", stderr="")
            with resource_locks.resource_lock(resource_locks.repo_lock_key(self.root, "repo")):
                thread = self._start(lambda: build.git_prepare(proj, self.root), errors)
                time.sleep(0.1)
                self.assertFalse(run.called, "仓库被独占时不得执行 checkout/pull")
            self._join(thread, errors)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[0][-2:], ["rev-parse", "HEAD"])

    def test_build_waits_for_repo_writer_and_allows_shared_reader(self):
        proj = {"build_type": "none", "image_name": "svc",
                "dockerfile": "Dockerfile", "docker_context": "."}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        key = resource_locks.repo_lock_key(self.root, ".")
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: entered.set()), \
                mock.patch.object(build, "run"), mock.patch.object(build, "log"):
            with resource_locks.resource_lock(key):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertFalse(entered.wait(0.1), "Git 写锁未释放时构建不得开始")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())

            entered.clear()
            errors.clear()
            with resource_locks.resource_lock(key, shared=True):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertTrue(entered.wait(1), "两个构建读取方应能共享仓库锁")
                self._join(thread, errors)

    def test_build_retries_when_git_head_changes_before_read_lock(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        proj_a = {"git_repo": "repo", "git_branch": "feature",
                  "build_type": "none", "deploy_mode": "go_binary", "image_name": "svc-a"}
        proj_b = {"git_repo": "repo", "git_branch": "main",
                  "build_type": "none", "deploy_mode": "go_binary", "image_name": "svc-b"}
        config = {"deploy": {"project_root": str(self.root)}}
        a_prepared = threading.Event()
        b_prepared = threading.Event()
        local = threading.local()
        state = {"head": "", "a_calls": 0}
        built_heads = []
        errors = []

        def prepare(proj, project_root):
            if proj["image_name"] == "svc-a":
                state["a_calls"] += 1
                if state["a_calls"] == 1:
                    local.head = "A"
                    state["head"] = "A"
                    a_prepared.set()
                    b_prepared.wait(2)
                    return repo, "A"
            local.head = "B"
            state["head"] = "B"
            b_prepared.set()
            return repo, "B"

        def git_run(cmd, **kwargs):
            return mock.Mock(returncode=0, stdout=state["head"] + "\n", stderr="")

        def steps(proj, *args, **kwargs):
            built_heads.append((proj["image_name"], local.head))

        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config",
                                  side_effect=lambda name: proj_a if name == "svc-a" else proj_b), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare", side_effect=prepare), \
                mock.patch.object(build.subprocess, "run", side_effect=git_run), \
                mock.patch.object(build, "_run_build_steps", side_effect=steps), \
                mock.patch.object(build, "log"):
            thread_a = self._start(lambda: build.build_project("svc-a", "1.0.0"), errors)
            self.assertTrue(a_prepared.wait(1))
            thread_b = self._start(lambda: build.build_project("svc-b", "1.0.0"), errors)
            self._join(thread_a, errors)
            self._join(thread_b, errors)

        self.assertNotIn(("svc-a", "A"), built_heads)
        self.assertIn(("svc-a", "B"), built_heads)
        self.assertIn(("svc-b", "B"), built_heads)
        self.assertEqual(state["a_calls"], 2)

    def test_static_default_prefix_matches_deployer_project(self):
        src = self.root / "dist"
        src.mkdir()
        (src / "index.html").write_text("ok", encoding="utf-8")
        proj = {"build_type": "none", "deploy_mode": "static_archive",
                "image_name": "wrong-fallback"}
        config = {"deploy": {"project_root": str(self.root),
                             "dist_dir": str(self.root / "out")}}
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare", return_value=None), \
                mock.patch.object(build, "_run_build_steps"), \
                mock.patch.object(build, "log"):
            build.build_project("site-a", "1.0.0")
        self.assertTrue((self.root / "out" / "site-a-1.0.0.zip").exists())
        self.assertFalse((self.root / "out" / "wrong-fallback-1.0.0.zip").exists())

    def test_failed_static_zip_preserves_previous_artifact(self):
        src = self.root / "dist"
        src.mkdir()
        (src / "index.html").write_text("new", encoding="utf-8")
        out_dir = self.root / "out"
        out_dir.mkdir()
        out = out_dir / "site-1.0.0.zip"
        out.write_bytes(b"previous")
        proj = {"zip_prefix": "site"}
        config = {"deploy": {"dist_dir": str(out_dir)}}
        with mock.patch.object(build.zipfile.ZipFile, "write", side_effect=RuntimeError("write failed")), \
                mock.patch.object(build, "log"):
            with self.assertRaises(RuntimeError):
                build._make_static_zip(proj, self.root, "1.0.0", config)
        self.assertEqual(out.read_bytes(), b"previous")
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), [out.name])

    def test_build_returns_verified_git_snapshot(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        proj = {"git_repo": "repo", "build_type": "none",
                "deploy_mode": "go_binary", "image_name": "svc"}
        config = {"deploy": {"project_root": str(self.root)}}
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare", return_value=(repo, "A" * 40)), \
                mock.patch.object(build.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="A" * 40 + "\n")), \
                mock.patch.object(build, "_run_build_steps"), \
                mock.patch.object(build, "log"):
            snapshot = build.build_project("svc", "1.0.0")
        self.assertEqual(snapshot, (repo, "A" * 40))

    def test_build_waits_for_final_artifact_identity(self):
        proj = {"build_type": "none", "deploy_mode": "go_binary", "image_name": "shared"}
        config = {"deploy": {"project_root": str(self.root)}}
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare", return_value=None), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: entered.set()), \
                mock.patch.object(build, "log"):
            with resource_locks.resource_lock(resource_locks.artifact_lock_key("svc", proj)):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertFalse(entered.wait(0.1), "同名最终产物不得并发生成")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())

    def test_build_locks_actual_workspace_not_only_git_repo(self):
        proj = {"git_repo": "repo", "build_type": "none", "image_name": "svc",
                "dockerfile": "Dockerfile", "docker_context": "."}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: entered.set()), \
                mock.patch.object(build, "run"), mock.patch.object(build, "log"):
            with resource_locks.resource_lock(resource_locks.repo_lock_key(self.root, ".")):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertFalse(entered.wait(0.1), "构建工作区被独占时不得开始读取")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())

    def test_artifact_packaging_waits_for_module_lock(self):
        module = self.root / "shared"
        (module / "target").mkdir(parents=True)
        (module / "target" / "app.jar").write_bytes(b"jar")
        proj = {"build_type": "maven", "deploy_mode": "jar_service",
                "build_order": [{"path": "shared", "goal": "package"}],
                "runtime_config": {"artifact_pattern": "target/*.jar"},
                "image_name": "svc"}
        config = {"deploy": {"project_root": str(self.root),
                             "dist_dir": str(self.root / "out")}}
        errors = []
        built = threading.Event()
        packaged = threading.Event()
        real_package = build._make_jar_artifact

        def package(*args, **kwargs):
            packaged.set()
            return real_package(*args, **kwargs)

        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: built.set()), \
                mock.patch.object(build, "_make_jar_artifact", side_effect=package), \
                mock.patch.object(build, "log"):
            with resource_locks.module_lock(self.root, "shared"):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertFalse(built.wait(0.1), "共享模块被占用时不得先构建再等待打包")
                self.assertFalse(packaged.wait(0.1), "模块被下一次构建清理时不得读取产物")
            self._join(thread, errors)
            self.assertTrue(built.is_set())
            self.assertTrue(packaged.is_set())
            self.assertEqual((self.root / "out" / "svc-1.0.0.jar").read_bytes(), b"jar")

    def test_docker_promotes_temporary_tag_before_push(self):
        proj = {"build_type": "none", "image_name": "svc",
                "dockerfile": "Dockerfile", "docker_context": "."}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="push"), \
                mock.patch.object(build, "docker_login"), \
                mock.patch.object(build, "git_prepare", return_value=None), \
                mock.patch.object(build, "_run_build_steps"), \
                mock.patch.object(build, "run") as run, \
                mock.patch.object(build, "log"):
            build.build_project("svc", "1.0.0")
        commands = [call.args[0] for call in run.call_args_list]
        formal = "registry.example.com/team/svc:1.0.0"
        temporary = commands[0][commands[0].index("-t") + 1]
        self.assertNotEqual(temporary, formal)
        self.assertEqual(commands[1], ["docker", "tag", temporary, formal])
        self.assertEqual(commands[2], ["docker", "push", formal])

    def test_docker_build_waits_for_module_lock(self):
        proj = {"build_type": "maven", "build_order": [{"path": "shared", "goal": "package"}],
                "image_name": "svc", "dockerfile": "Dockerfile", "docker_context": "."}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        errors = []
        built = threading.Event()
        docker_started = threading.Event()
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: built.set()), \
                mock.patch.object(build, "run",
                                  side_effect=lambda *a, **k: docker_started.set()), \
                mock.patch.object(build, "log"):
            with resource_locks.module_lock(self.root, "shared"):
                thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                self.assertFalse(built.wait(0.1), "根上下文读取子模块时不得与其构建交错")
                self.assertFalse(docker_started.wait(0.1), "Docker 读取模块产物期间不得被 clean")
            self._join(thread, errors)
            self.assertTrue(built.is_set())
            self.assertTrue(docker_started.is_set())

    def test_docker_context_releases_disjoint_build_module_before_docker(self):
        proj = {"build_type": "maven",
                "build_order": [{"path": "common", "goal": "install"},
                                {"path": "web", "goal": "package"}],
                "image_name": "svc", "dockerfile": "web/Dockerfile",
                "docker_context": "web"}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        errors = []
        built = threading.Event()
        docker_started = threading.Event()
        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "_run_build_steps",
                                  side_effect=lambda *a, **k: built.set()), \
                mock.patch.object(build, "run",
                                  side_effect=lambda *a, **k: docker_started.set()), \
                mock.patch.object(build, "log"):
            thread = None
            try:
                with resource_locks.module_lock(self.root, "common"):
                    thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                    self.assertFalse(built.wait(0.1), "公共模块构建期间不得与其他构建交错")
                self._join(thread, errors)
                self.assertTrue(docker_started.is_set())
            finally:
                if thread is not None:
                    thread.join(3)

    def test_docker_subcontext_locks_build_module_only_during_steps(self):
        proj = {"build_type": "maven",
                "build_order": [{"path": "common", "goal": "install"},
                                {"path": "web", "goal": "package"}],
                "image_name": "svc", "dockerfile": "web/Dockerfile",
                "docker_context": "web"}
        config = {"deploy": {"project_root": str(self.root)}}
        registry = {"host": "registry.example.com", "namespace": "team"}
        errors = []
        built = threading.Event()
        release_steps = threading.Event()
        docker_waiting = threading.Event()
        release_docker = threading.Event()
        module_acquired = threading.Event()

        def steps(*args, **kwargs):
            built.set()
            release_steps.wait(5)

        def docker_run(*args, **kwargs):
            docker_waiting.set()
            release_docker.wait(5)

        def acquire_module():
            with resource_locks.module_lock(self.root, "common"):
                module_acquired.set()

        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config", return_value=proj), \
                mock.patch.object(build, "get_registry_auth", return_value=registry), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "_run_build_steps", side_effect=steps), \
                mock.patch.object(build, "run", side_effect=docker_run), \
                mock.patch.object(build, "log"):
            thread = None
            contender = None
            try:
                with resource_locks.module_lock(self.root, "common"):
                    thread = self._start(lambda: build.build_project("svc", "1.0.0"), errors)
                    self.assertFalse(built.wait(0.1), "子上下文之外的构建模块也必须互斥")
                self.assertTrue(built.wait(1))
                release_steps.set()
                self.assertTrue(docker_waiting.wait(1))
                contender = self._start(acquire_module, errors)
                self.assertTrue(module_acquired.wait(2),
                                "构建步骤结束后应释放上下文之外的模块锁")
                release_docker.set()
                self._join(thread, errors)
                self._join(contender, errors)
            finally:
                release_steps.set()
                release_docker.set()
                if thread is not None:
                    thread.join(3)
                if contender is not None:
                    contender.join(3)

    def test_shared_module_artifact_stays_with_its_build(self):
        module = self.root / "shared"
        (module / "target").mkdir(parents=True)
        (module / "pom.xml").write_text("<project/>", encoding="utf-8")
        base = {"build_type": "maven", "deploy_mode": "jar_service",
                "build_order": [{"path": "shared", "goal": "package"}],
                "runtime_config": {"artifact_pattern": "target/*.jar"}}
        proj_a = dict(base, image_name="svc-a")
        proj_b = dict(base, image_name="svc-b")
        config = {"deploy": {"project_root": str(self.root),
                             "dist_dir": str(self.root / "out")}}
        inner_done = threading.Event()
        release_a = threading.Event()
        b_built = threading.Event()
        overlapped = []
        errors = []
        active = {"name": "A"}
        real_steps = build._run_build_steps

        def steps(proj, *args, **kwargs):
            active["name"] = "A" if proj["image_name"] == "svc-a" else "B"
            real_steps(proj, *args, **kwargs)
            if proj["image_name"] == "svc-a":
                inner_done.set()
                release_a.wait(3)
            else:
                b_built.set()

        def run(cmd, cwd, **kwargs):
            (module / "target" / "app.jar").write_text(active["name"], encoding="utf-8")

        with mock.patch.object(build, "load_config", return_value=config), \
                mock.patch.object(build, "get_project_config",
                                  side_effect=lambda name: proj_a if name == "svc-a" else proj_b), \
                mock.patch.object(build, "get_registry_auth", return_value={}), \
                mock.patch.object(build, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build, "git_prepare"), \
                mock.patch.object(build, "find_mvn", return_value="mvn"), \
                mock.patch.object(build, "find_java_home", return_value=""), \
                mock.patch.object(build, "_run_build_steps", side_effect=steps), \
                mock.patch.object(build, "run", side_effect=run), \
                mock.patch.object(build, "log"):
            thread_a = self._start(lambda: build.build_project("svc-a", "1.0.0"), errors)
            self.assertTrue(inner_done.wait(1))
            thread_b = self._start(lambda: build.build_project("svc-b", "1.0.0"), errors)
            overlapped.append(b_built.wait(0.2))
            release_a.set()
            self._join(thread_a, errors)
            self._join(thread_b, errors)

        self.assertFalse(overlapped[0], "A 打包期间 B 不得清理并重写同一模块")
        self.assertEqual((self.root / "out" / "svc-a-1.0.0.jar").read_text(), "A")
        self.assertEqual((self.root / "out" / "svc-b-1.0.0.jar").read_text(), "B")

    def test_same_maven_module_waits_for_module_lock(self):
        module = self.root / "shared"
        module.mkdir()
        (module / "pom.xml").write_text("<project/>", encoding="utf-8")
        proj = {"build_order": [{"path": "shared", "goal": "package"}]}
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "find_mvn", return_value="mvn"), \
                mock.patch.object(build, "find_java_home", return_value=""), \
                mock.patch.object(build, "run", side_effect=lambda *a, **k: entered.set()), \
                mock.patch.object(build, "log"):
            with resource_locks.module_lock(self.root, "shared"):
                thread = self._start(lambda: build._run_maven_steps(
                    proj, self.root, "1.0.0", "prod", {}), errors)
                self.assertFalse(entered.wait(0.1), "同模块构建不得并发清理 target")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())

    def test_same_frontend_module_waits_for_module_lock(self):
        proj = {"build_order": [{"name": "web", "path": "shared",
                                  "commands": ["npm run build"]}]}
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "find_node", return_value="node"), \
                mock.patch.object(build.shutil, "which", return_value=None), \
                mock.patch.object(build, "run_shell", side_effect=lambda *a, **k: entered.set()), \
                mock.patch.object(build, "log"):
            with resource_locks.module_lock(self.root, "shared"):
                thread = self._start(lambda: build._run_frontend_steps(
                    proj, self.root, "1.0.0", {}), errors)
                self.assertFalse(entered.wait(0.1), "同前端目录构建不得并发")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())

    def test_whole_root_build_waits_for_root_module_lock(self):
        errors = []
        entered = threading.Event()
        with mock.patch.object(build, "_run_node_steps", side_effect=lambda *a: entered.set()), \
                mock.patch.object(build, "log"):
            with resource_locks.module_lock(self.root, "."):
                thread = self._start(lambda: build._run_build_steps(
                    {"build_type": "node"}, self.root, "1.0.0", "", {}), errors)
                self.assertFalse(entered.wait(0.1), "整根构建必须与同根构建互斥")
            self._join(thread, errors)
            self.assertTrue(entered.is_set())


if __name__ == "__main__":
    unittest.main()
