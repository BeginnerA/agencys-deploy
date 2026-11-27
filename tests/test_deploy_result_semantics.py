"""发布结果语义：灰度停止/回滚后健康检查失败不得上报成功"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from resource_locks import artifact_lock_key, resource_lock, snailjob_lock_key


class FakeServer:
    def __init__(self):
        self.host = ""

    def close(self):
        pass


class TestCanaryStopSemantics(unittest.TestCase):

    def setUp(self):
        self.deployer = Deployer.__new__(Deployer)
        self.deployer.project = "app"
        self.deployer.version = "2.0.0"
        self.deployer.previous_version = "1.0.0"
        self.deployer.env = "prod"
        self.deployer.config = {}
        self.deployer.proj_cfg = {"deploy_mode": "container",
                                  "git_tag_enabled": False}
        self.deployer.canary = True
        self.deployer.registry_mode = "direct"
        self.deployer.image_prefix = ""
        self.deployer.image_name = "app"
        self.deployer.full_image = "app:2.0.0"
        self.deployer.prev_image = "app:1.0.0"
        self.deployer._no_git_tag = True
        self.deployer._skip_git_push = True

    def test_run_waits_for_same_final_artifact_identity(self):
        entered = threading.Event()
        errors = []
        self.deployer._strategy = mock.Mock(supports_prefetch=False)

        def run():
            try:
                self.deployer.run([], skip_build=True, skip_snailjob=True)
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify", side_effect=lambda *a, **k: entered.set()):
            with resource_lock(artifact_lock_key(self.deployer.project, self.deployer.proj_cfg)):
                thread = threading.Thread(target=run)
                thread.start()
                self.assertFalse(entered.wait(0.1), "同名产物被占用时发布不得开始")
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(entered.is_set())

    def test_build_and_push_share_final_artifact_lock(self):
        built = threading.Event()
        pushed = threading.Event()
        errors = []

        def run():
            try:
                self.deployer.build_and_push([])
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.build_project",
                           side_effect=lambda *a, **k: built.set()) as build, \
                mock.patch.object(self.deployer, "_push_image_only",
                                  side_effect=lambda *a, **k: pushed.set() or True):
            thread = None
            try:
                with resource_lock(artifact_lock_key(self.deployer.project,
                                                     self.deployer.proj_cfg)):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertFalse(built.wait(0.1), "构建到推送窗口不得被其他产物任务插入")
            finally:
                if thread is not None:
                    thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(built.is_set())
        self.assertTrue(pushed.is_set())
        self.assertFalse(build.call_args.kwargs["lock_artifact"])

    def test_snailjob_pause_waits_for_same_group(self):
        self.deployer.proj_cfg["snailjob_group"] = "shared"
        self.deployer._strategy = mock.Mock(supports_prefetch=False)
        entered = threading.Event()
        errors = []

        def run():
            try:
                self.deployer.run([], skip_build=True)
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.backup_jobs", return_value={},
                           side_effect=lambda *a, **k: entered.set()) as backup, \
                mock.patch("deploy.set_job_status"), \
                mock.patch("deploy.restore_jobs") as restore:
            thread = None
            try:
                with resource_lock(snailjob_lock_key("shared")):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertFalse(entered.wait(0.1), "同组 SnailJob 暂停生命周期必须互斥")
            finally:
                if thread is not None:
                    thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        backup.assert_called_once()
        restore.assert_called_once()

    def test_snailjob_restores_when_prefetch_raises(self):
        self.deployer.proj_cfg["snailjob_group"] = "shared"
        self.deployer._strategy = mock.Mock(supports_prefetch=True)
        self.deployer._strategy.prefetch.side_effect = RuntimeError("prefetch failed")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.backup_jobs", return_value={"jobs": []}), \
                mock.patch("deploy.set_job_status") as pause, \
                mock.patch("deploy.restore_jobs") as restore, \
                self.assertRaisesRegex(RuntimeError, "prefetch failed"):
            self.deployer.run([], skip_build=True)
        pause.assert_called_once()
        restore.assert_called_once()

    def test_snailjob_restores_when_pause_raises(self):
        self.deployer.proj_cfg["snailjob_group"] = "shared"
        self.deployer._strategy = mock.Mock(supports_prefetch=False)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.backup_jobs", return_value={"jobs": []}), \
                mock.patch("deploy.set_job_status", side_effect=RuntimeError("pause failed")), \
                mock.patch("deploy.restore_jobs") as restore, \
                self.assertRaisesRegex(RuntimeError, "pause failed"):
            self.deployer.run([], skip_build=True)
        restore.assert_called_once()

    def test_snailjob_rollback_restores_when_pause_raises(self):
        self.deployer.proj_cfg["snailjob_group"] = "shared"
        self.deployer._strategy = mock.Mock(strict_host_lifecycle=False)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.backup_jobs", return_value={"jobs": []}), \
                mock.patch("deploy.set_job_status", side_effect=RuntimeError("pause failed")), \
                mock.patch("deploy.restore_jobs") as restore, \
                self.assertRaisesRegex(RuntimeError, "pause failed"):
            self.deployer.rollback_all([])
        restore.assert_called_once()

    def test_snailjob_rollback_waits_for_same_group(self):
        self.deployer.proj_cfg["snailjob_group"] = "shared"
        self.deployer._strategy = mock.Mock(strict_host_lifecycle=False)
        entered = threading.Event()
        errors = []

        def run():
            try:
                self.deployer.rollback_all([])
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.backup_jobs", return_value={},
                           side_effect=lambda *a, **k: entered.set()) as backup, \
                mock.patch("deploy.set_job_status"), \
                mock.patch("deploy.restore_jobs") as restore:
            thread = None
            try:
                with resource_lock(snailjob_lock_key("shared")):
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertFalse(entered.wait(0.1), "同组回滚暂停生命周期必须互斥")
            finally:
                if thread is not None:
                    thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        backup.assert_called_once()
        restore.assert_called_once()

    def test_successful_tag_uses_verified_build_snapshot(self):
        self.deployer.config = {"deploy": {"project_root": "/repos"}}
        self.deployer.proj_cfg["git_tag_enabled"] = True
        self.deployer._no_git_tag = False
        self.deployer._strategy = mock.Mock(supports_prefetch=False)
        snapshot = (Path("/repos/app"), "a" * 40)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch("deploy.build_project", return_value=snapshot), \
                mock.patch.object(self.deployer, "deploy_server", return_value=True), \
                mock.patch("git_tag.tag_project") as tag:
            ok = self.deployer.run([{"ip": "10.0.0.1"}], skip_snailjob=True)
        self.assertTrue(ok)
        self.assertEqual(tag.call_args.kwargs.get("expected_head"), snapshot[1])

    def test_run_reuses_held_artifact_lock(self):
        self.deployer._strategy = mock.Mock(supports_prefetch=False)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PLATFORM_DATA_DIR": tmp}), \
                mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                resource_lock(artifact_lock_key(self.deployer.project, self.deployer.proj_cfg)):
            ok = self.deployer.run([], skip_build=True, skip_snailjob=True,
                                   lock_artifact=False)
        self.assertTrue(ok)

    def test_stop_after_first_server_is_not_success(self):
        servers = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]
        with mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify") as notify, \
                mock.patch("sqlite3.connect", side_effect=RuntimeError("no db")), \
                mock.patch.object(self.deployer, "prefetch_images",
                                  return_value=[]), \
                mock.patch.object(self.deployer, "deploy_server",
                                  return_value=True) as deploy_srv, \
                mock.patch.object(self.deployer, "_canary_prompt",
                                  return_value="stop"):
            ok = self.deployer.run(servers, skip_build=True,
                                   skip_snailjob=True)
        self.assertFalse(ok, "灰度 stop 属于未完成发布，不能返回成功")
        self.assertEqual(deploy_srv.call_count, 1)
        self.assertNotIn(
            "success", [c.args[1] for c in notify.call_args_list if len(c.args) > 1],
            "未完成发布不得发送 success 通知")


class TestRollbackHealthSemantics(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.deployer = Deployer.__new__(Deployer)
        self.deployer.registry_mode = "direct"
        self.deployer.ssh_cfg = {}
        self.deployer.previous_version = "1.0.0"
        self.deployer.version = "2.0.0"
        self.deployer.project = "app"
        self.deployer.env = "prod"
        self.deployer.proj_cfg = {}
        self.deployer.service_name = "app"
        self.deployer.nacos = mock.Mock()
        self.deployer.nacos._snapshotted_keys = set()
        self.deployer.config = {}
        self.deployer.dist_dir = Path(self.tmp)
        self.deployer.remote_tmp_dir = "/tmp/platform-deploy"
        self.deployer._save_lock = threading.Lock()

    def test_health_check_failure_reports_rollback_failed(self):
        servers = [{"ip": "10.0.0.1"}]
        with mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify") as notify, \
                mock.patch("deploy.wait_for_healthy",
                           side_effect=RuntimeError("健康检查超时")), \
                mock.patch.object(self.deployer, "nacos_offline"), \
                mock.patch.object(self.deployer, "nacos_online",
                                  return_value=True), \
                mock.patch.object(self.deployer, "rollback_container"), \
                mock.patch("deploy.RemoteServer", return_value=FakeServer()):
            ok = self.deployer.rollback_all(servers)
        self.assertFalse(ok, "容器已回滚但健康检查失败，不能上报回滚成功")
        self.assertNotIn(
            "success", [c.args[1] for c in notify.call_args_list if len(c.args) > 1])


class TargetModifiedAliasTest(unittest.TestCase):
    def test_alias_is_bidirectional(self):
        from deploy import Deployer
        d = Deployer.__new__(Deployer)
        d.target_modified = True
        self.assertTrue(d.container_modified)
        d.container_modified = False
        self.assertFalse(d.target_modified)

    def test_default_false_for_bare_instance(self):
        from deploy import Deployer
        d = Deployer.__new__(Deployer)
        self.assertFalse(d.container_modified)


class RollbackSourceMissingTest(unittest.TestCase):
    """显式回滚：目标产物缺失必须在 Nacos 下线前判失败（且仍关闭连接）"""

    def setUp(self):
        self.deployer = Deployer.__new__(Deployer)
        self.deployer.project = "app"
        self.deployer.version = "2.0.0"
        self.deployer.previous_version = "1.0.0"
        self.deployer.env = "prod"
        self.deployer.config = {}
        self.deployer.proj_cfg = {}
        self.deployer.ssh_cfg = {}
        self.deployer.service_name = "app"

    def test_missing_source_fails_before_nacos_offline(self):
        strategy = mock.Mock()
        strategy.supports_nacos = True
        strategy.rollback_source_missing.return_value = True
        self.deployer._strategy = strategy
        server = mock.Mock()
        with mock.patch("deploy.log"), \
                mock.patch("deploy.send_notify"), \
                mock.patch.object(self.deployer, "nacos_offline") as offline, \
                mock.patch("deploy.RemoteServer", return_value=server):
            ok = self.deployer.rollback_all([{"ip": "10.0.0.1"}])
        self.assertFalse(ok, "回滚源缺失属于回滚失败，不能上报成功")
        offline.assert_not_called()
        strategy.rollback.assert_not_called()
        server.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
