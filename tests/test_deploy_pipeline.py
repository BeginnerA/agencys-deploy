"""流水线 stage 执行：每个服务必须使用各自的 servers_file/版本，不能串用最后一个服务配置"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy_pipeline
from resource_locks import artifact_lock_key, resource_lock


class TestRunStageRouting(unittest.TestCase):

    def _run(self, parallel):
        stage = {
            "name": "stage1", "parallel": parallel,
            "services": [
                {"name": "svc-a", "version": "2.0.0",
                 "servers_file": "servers-a.yaml"},
                {"name": "svc-b", "version": "2.1.0"},
            ],
        }
        calls = {}

        def fake_deploy(name, version, previous, servers_file, *args, **kwargs):
            calls[name] = (version, previous, servers_file)
            return True

        with mock.patch.object(deploy_pipeline, "deploy_one_service",
                               side_effect=fake_deploy):
            ok = deploy_pipeline.run_stage(
                stage, "1.0.0", "0.9.0", "servers-default.yaml",
                {}, True, False, env="prod", canary=False)
        self.assertTrue(ok)
        return calls

    def _assert_routing(self, calls):
        self.assertEqual(calls["svc-a"],
                         ("2.0.0", "0.9.0", "servers-a.yaml"))
        self.assertEqual(calls["svc-b"],
                         ("2.1.0", "0.9.0", "servers-default.yaml"))

    def test_serial_uses_per_service_servers_file(self):
        self._assert_routing(self._run(parallel=False))

    def test_parallel_uses_per_service_servers_file(self):
        self._assert_routing(self._run(parallel=True))

    def test_prebuilt_snapshot_reaches_deployer(self):
        head = "a" * 40
        deployer = mock.Mock()
        deployer.run.return_value = True
        with tempfile.TemporaryDirectory() as tmp:
            servers_file = Path(tmp) / "servers.yaml"
            servers_file.write_text("servers:\n  - ip: 10.0.0.1\n", encoding="utf-8")
            with mock.patch.object(deploy_pipeline, "Deployer", return_value=deployer), \
                    mock.patch.object(deploy_pipeline, "log"):
                ok = deploy_pipeline.deploy_one_service(
                    "svc", "2.0.0", "1.0.0", str(servers_file), {}, False,
                    env="prod", built_git_head=head)
        self.assertTrue(ok)
        self.assertEqual(deployer._built_git_head, head)
        self.assertTrue(deployer.run.call_args.kwargs["skip_build"])

    def test_deploy_one_service_reuses_held_artifact_lock(self):
        deployer = mock.Mock()
        deployer.run.return_value = True
        with tempfile.TemporaryDirectory() as tmp:
            servers_file = Path(tmp) / "servers.yaml"
            servers_file.write_text("servers:\n  - ip: 10.0.0.1\n", encoding="utf-8")
            with mock.patch.object(deploy_pipeline, "Deployer", return_value=deployer), \
                    mock.patch.object(deploy_pipeline, "log"):
                ok = deploy_pipeline.deploy_one_service(
                    "svc", "2.0.0", "1.0.0", str(servers_file), {}, False,
                    env="prod", lock_artifact=False)
        self.assertTrue(ok)
        self.assertFalse(deployer.run.call_args.kwargs["lock_artifact"])

    def test_stage_build_snapshots_reach_each_service(self):
        stage = {"name": "stage1", "parallel": True, "services": [
            {"name": "svc-a"}, {"name": "svc-b"}]}
        snapshots = {"svc-a": "a" * 40, "svc-b": "b" * 40}
        calls = {}

        def fake_deploy(name, *args, **kwargs):
            calls[name] = kwargs.get("built_git_head")
            return True

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch.object(deploy_pipeline, "build_services", return_value=snapshots), \
                mock.patch.object(deploy_pipeline, "get_project_config", return_value={}), \
                mock.patch.object(deploy_pipeline, "deploy_one_service", side_effect=fake_deploy), \
                mock.patch.object(deploy_pipeline, "log"):
            ok = deploy_pipeline.run_stage(
                stage, "2.0.0", "1.0.0", "servers.yaml", {}, False, False)
        self.assertTrue(ok)
        self.assertEqual(calls, snapshots)

    def test_stage_artifact_lock_covers_build_to_deploy(self):
        stage = {"name": "stage1", "services": [{"name": "svc"}]}
        proj = {"image_name": "shared"}
        competitor_attempted = threading.Event()
        competitor_entered = threading.Event()
        deploy_entered = threading.Event()
        allow_deploy = threading.Event()
        results = []
        errors = []

        def compete():
            competitor_attempted.set()
            with resource_lock(artifact_lock_key("svc", proj)):
                competitor_entered.set()

        def fake_deploy(*args, **kwargs):
            self.assertFalse(kwargs["lock_artifact"], "stage 已持产物锁，部署不得重复加锁")
            deploy_entered.set()
            self.assertTrue(allow_deploy.wait(2))
            return True

        def run():
            try:
                results.append(deploy_pipeline.run_stage(
                    stage, "2.0.0", "1.0.0", "servers.yaml", {}, False, False))
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch.object(deploy_pipeline, "get_project_config", return_value=proj), \
                mock.patch.object(deploy_pipeline, "build_services", return_value={}), \
                mock.patch.object(deploy_pipeline, "deploy_one_service", side_effect=fake_deploy), \
                mock.patch.object(deploy_pipeline, "log"):
            thread = threading.Thread(target=run)
            competitor = None
            try:
                thread.start()
                self.assertTrue(deploy_entered.wait(1))
                competitor = threading.Thread(target=compete)
                competitor.start()
                self.assertTrue(competitor_attempted.wait(1))
                self.assertFalse(competitor_entered.wait(0.1),
                                 "构建到部署窗口内同名产物不得被其他任务覆盖")
            finally:
                allow_deploy.set()
                thread.join(3)
                if competitor is not None:
                    competitor.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertTrue(competitor_entered.is_set())

    def test_build_first_artifact_lock_covers_build_through_stages(self):
        pipeline = {"build_first": True, "stages": [
            {"name": "stage1", "services": [
                {"name": "svc", "version": "2.0.0", "previous_version": "1.0.0"}]}]}
        proj = {"image_name": "shared"}
        competitor_attempted = threading.Event()
        competitor_entered = threading.Event()
        built = threading.Event()
        allow_build = threading.Event()
        competitors = []
        errors = []

        def compete():
            competitor_attempted.set()
            with resource_lock(artifact_lock_key("svc", proj)):
                competitor_entered.set()

        def fake_build(*args, **kwargs):
            competitor = threading.Thread(target=compete)
            competitors.append(competitor)
            competitor.start()
            self.assertTrue(competitor_attempted.wait(1))
            built.set()
            self.assertTrue(allow_build.wait(2))
            return {"svc": "a" * 40}

        def fake_run_stage(*args, **kwargs):
            self.assertFalse(kwargs["lock_artifact"], "build_first 外层已持产物锁")
            self.assertFalse(competitor_entered.is_set())
            return True

        def run():
            try:
                deploy_pipeline.main()
            except BaseException as exc:
                errors.append(exc)

        argv = ["deploy_pipeline.py", "--pipeline", "pipeline", "--config-source", "yaml"]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(deploy_pipeline, "_resolve_source_db", return_value=False), \
                mock.patch.object(deploy_pipeline, "load_config", return_value={"nacos": {}}), \
                mock.patch.object(deploy_pipeline, "get_pipeline_config", return_value=pipeline), \
                mock.patch.object(deploy_pipeline, "get_project_config", return_value=proj), \
                mock.patch.object(deploy_pipeline, "default_servers_file", return_value="servers.yaml"), \
                mock.patch.object(deploy_pipeline, "build_services", side_effect=fake_build) as build, \
                mock.patch.object(deploy_pipeline, "run_stage", side_effect=fake_run_stage) as run_stage, \
                mock.patch("notify.send_notify"), \
                mock.patch.object(deploy_pipeline, "log"):
            thread = threading.Thread(target=run)
            try:
                thread.start()
                self.assertTrue(built.wait(1))
                self.assertFalse(competitor_entered.wait(0.1),
                                 "build_first 构建到部署窗口必须持有同名产物锁")
            finally:
                allow_build.set()
                thread.join(3)
                for competitor in competitors:
                    competitor.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(build.call_args.kwargs["lock_artifact"])
        self.assertFalse(run_stage.call_args.kwargs["lock_artifact"])
        self.assertTrue(competitor_entered.is_set())

    def test_build_first_skips_stage_rebuild_and_passes_snapshots(self):
        pipeline = {"build_first": True, "stages": [
            {"name": "stage1", "services": [
                {"name": "svc", "version": "2.0.0", "previous_version": "1.0.0"}]}]}
        snapshots = {"svc": "a" * 40}
        argv = ["deploy_pipeline.py", "--pipeline", "pipeline", "--config-source", "yaml"]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(deploy_pipeline, "_resolve_source_db", return_value=False), \
                mock.patch.object(deploy_pipeline, "load_config", return_value={"nacos": {}}), \
                mock.patch.object(deploy_pipeline, "get_pipeline_config", return_value=pipeline), \
                mock.patch.object(deploy_pipeline, "get_project_config", return_value={}), \
                mock.patch.object(deploy_pipeline, "default_servers_file", return_value="servers.yaml"), \
                mock.patch.object(deploy_pipeline, "build_services", return_value=snapshots) as build, \
                mock.patch.object(deploy_pipeline, "run_stage", return_value=True) as run, \
                mock.patch("notify.send_notify"), \
                mock.patch.object(deploy_pipeline, "log"):
            deploy_pipeline.main()
        build.assert_called_once()
        self.assertTrue(run.call_args.args[5], "build_first 后不得在 stage 内重复构建")
        self.assertEqual(run.call_args.kwargs.get("build_snapshots"), snapshots)


class TestBuildServicesSnapshots(unittest.TestCase):
    def test_returns_first_snapshot_for_each_unique_service(self):
        snapshot = (Path("/repos/app"), "a" * 40)
        with mock.patch.object(deploy_pipeline, "build_project", side_effect=[snapshot, None]) as build, \
                mock.patch.object(deploy_pipeline, "log"):
            snapshots = deploy_pipeline.build_services(
                [("svc-a", "2.0.0"), ("svc-a", "2.0.1"), ("svc-b", "3.0.0")])
        self.assertEqual(snapshots, {"svc-a": snapshot[1], "svc-b": ""})
        self.assertEqual(build.call_count, 2)


class TestPushOnlyArtifactLock(unittest.TestCase):
    def test_pipeline_locks_cover_build_to_push(self):
        entered = threading.Event()
        errors = []
        proj = {"image_name": "shared"}

        def run():
            try:
                with deploy_pipeline._pipeline_artifact_locks([("svc", "1.0.0")]):
                    entered.set()
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch.object(deploy_pipeline, "get_project_config", return_value=proj):
            thread = None
            try:
                with resource_lock(artifact_lock_key("svc", proj)):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertFalse(entered.wait(0.1), "流水线构建到推送窗口必须持有产物锁")
            finally:
                if thread is not None:
                    thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())

    def test_build_and_push_reuse_pipeline_artifact_lock(self):
        with mock.patch.object(deploy_pipeline, "build_project") as build:
            deploy_pipeline.build_services([("svc", "1.0.0")], lock_artifact=False)
        self.assertFalse(build.call_args.kwargs["lock_artifact"])

        stage = {"name": "stage1", "services": [{"name": "svc"}]}
        with mock.patch.object(deploy_pipeline, "push_one_service", return_value=True) as push:
            deploy_pipeline.push_stage(stage, "1.0.0", "0.9.0", "servers.yaml", {},
                                       lock_artifact=False)
        self.assertFalse(push.call_args.kwargs["lock_artifact"])


class AutoFillVersionsStrategyTest(unittest.TestCase):
    def test_mixed_pipeline_uses_each_strategy_semantics(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [
            {"services": [{"name": "web-api"}, {"name": "creator-web"}]},
        ]}
        probes = {
            "web-api": mock.Mock(current="1.0.29", next="1.0.30"),
            "creator-web": mock.Mock(current="20261012", next="20261013"),
        }

        def fake_deployer(name, *a, **kw):
            d = mock.Mock()
            d.strategy.query_current_version.return_value = probes[name].current
            d.strategy.next_version.side_effect = lambda cur, n=name: probes[n].next
            return d

        with mock.patch.object(dp, "Deployer", side_effect=fake_deployer), \
                mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")

        svcs = {s["name"]: s for s in pipeline["stages"][0]["services"]}
        self.assertEqual(svcs["web-api"]["version"], "1.0.30")
        self.assertEqual(svcs["web-api"]["previous_version"], "1.0.29")
        self.assertEqual(svcs["creator-web"]["version"], "20261013")
        self.assertEqual(svcs["creator-web"]["previous_version"], "20261012")

    def test_explicit_service_version_wins(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [{"services": [
            {"name": "svc", "version": "9.9.9", "previous_version": "9.9.8"}]}]}
        with mock.patch.object(dp, "Deployer") as dep, mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")
        dep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
