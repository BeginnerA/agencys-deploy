# tests/test_image_sync.py
"""直传模式镜像同步测试：同版本重复发布时以镜像 digest 决定是否传输"""
import contextlib
import io
import json as _json
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer, tar_image_id  # noqa: E402


class FakeServer:
    """模拟 RemoteServer：image_id 按预置队列依次返回（首个为传输前查询）"""

    def __init__(self, image_ids=None):
        self.host = "10.0.0.1"
        self.image_ids = list(image_ids or [])
        self.exec_calls = []
        self.uploaded = []

    def image_id(self, image):
        return self.image_ids.pop(0) if self.image_ids else None

    def exec(self, cmd, timeout=60):
        self.exec_calls.append(cmd)
        return ""

    def sftp_put_with_progress(self, local_path, remote_path, label=""):
        self.uploaded.append((local_path, remote_path))

    def close(self):
        pass


def make_deployer(tmp_dir: str) -> Deployer:
    """绕过 __init__ 构造最小 Deployer（直传模式字段）"""
    d = Deployer.__new__(Deployer)
    d.image_prefix = ""
    d.image_name = "app"
    d.version = "1.0.30"
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d._save_lock = threading.Lock()
    return d


class FakeProc:
    """模拟成功的 subprocess.Popen：stdout 提供分块数据，wait 返回 0"""
    def __init__(self, data=b"fake-tar-data"):
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(b"")
        self.returncode = 0

    def wait(self):
        return self.returncode


def ok_proc(*args, **kwargs):
    """模拟执行成功的 subprocess.Popen 工厂"""
    return FakeProc()


class TestEnsureImageReady(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.deployer = make_deployer(self.tmp)

    def test_remote_same_digest_skips_transfer(self):
        """远端镜像与本地 digest 一致：跳过传输"""
        server = FakeServer(image_ids=["sha256:aaa"])
        with mock.patch("deploy.local_image_id", return_value="sha256:aaa"), \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        self.assertEqual(server.uploaded, [])
        self.assertEqual(server.exec_calls, [])

    def test_same_tag_different_digest_retransfers(self):
        """核心回归：同 tag 但远端是旧代码（digest 不同）时，必须重新传输"""
        server = FakeServer(image_ids=["sha256:old", "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc) as proc, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in proc.call_args_list if c.args[0][0] == "docker"]
        self.assertTrue(save_calls, "同 tag 不同 digest 必须重新 docker save")
        self.assertEqual(len(server.uploaded), 1)
        self.assertTrue(any("docker load" in c for c in server.exec_calls))

    def test_remote_missing_transfers(self):
        """远端无镜像（首次发布）：正常传输并在 load 后校验 digest"""
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc), \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        self.assertEqual(len(server.uploaded), 1)
        self.assertTrue(any("docker load" in c for c in server.exec_calls))

    def test_local_missing_remote_exists_skips(self):
        """本地无镜像（跳过构建场景）：远端已有则跳过传输，保持兼容"""
        server = FakeServer(image_ids=["sha256:aaa"])
        with mock.patch("deploy.local_image_id", return_value=None), \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        self.assertEqual(server.uploaded, [])
        self.assertEqual(server.exec_calls, [])

    def test_local_missing_no_remote_raises(self):
        """本地与远端均无镜像且无 tar：报错"""
        server = FakeServer(image_ids=[None])
        with mock.patch("deploy.local_image_id", return_value=None), \
                mock.patch("deploy.log"):
            with self.assertRaises(RuntimeError):
                self.deployer.ensure_image_ready(server)

    def test_tar_reused_only_when_matches_local(self):
        """tar 与本地镜像 digest 一致才复用，不触发 docker save"""
        tar = Path(self.tmp) / "app_1.0.30.tar.gz"
        tar.write_bytes(b"fake")
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc) as proc, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in proc.call_args_list if c.args[0][0] == "docker"]
        self.assertFalse(save_calls, "tar 与本地一致时应复用，不应 docker save")

    def test_tar_stale_reexports(self):
        """tar 是旧代码导出（digest 不一致）：重新 docker save 覆盖"""
        tar = Path(self.tmp) / "app_1.0.30.tar.gz"
        tar.write_bytes(b"fake")
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value="sha256:old"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc) as proc, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in proc.call_args_list if c.args[0][0] == "docker"]
        self.assertTrue(save_calls, "tar 与本地不一致时必须重新 docker save")

    def test_load_mismatch_raises(self):
        """load 后远端 digest 与本地仍不一致：报错"""
        server = FakeServer(image_ids=[None, "sha256:other"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc), \
                mock.patch("deploy.log"):
            with self.assertRaises(RuntimeError):
                self.deployer.ensure_image_ready(server)

    def test_rollback_version_uses_same_digest_logic(self):
        """回滚到 previous_version：以该版本镜像的 digest 为准（远端一致则跳过）"""
        server = FakeServer(image_ids=["sha256:old"])
        with mock.patch("deploy.local_image_id", return_value="sha256:old"), \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server, version="1.0.29")
        self.assertEqual(server.uploaded, [])
        self.assertEqual(server.exec_calls, [])


class TestPrefetchImages(unittest.TestCase):
    """并行预传输测试：所有服务器并行上传+load，失败收集不中断"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.deployer = make_deployer(self.tmp)
        self.deployer.registry_mode = "direct"
        self.deployer.ssh_cfg = {}

    def _patch_remote(self, fake_servers):
        """mock RemoteServer：按 IP 序号返回对应的 FakeServer 实例"""
        def fake_remote(host, ssh_cfg):
            idx = int(host.rsplit(".", 1)[1]) - 1
            fake_servers[idx].host = host
            return fake_servers[idx]
        return mock.patch("deploy.RemoteServer", side_effect=fake_remote)

    def test_parallel_transfers_all_servers(self):
        """3 台服务器全部需要传输：各自独立完成上传与 docker load"""
        servers = [{"ip": f"10.0.0.{i}"} for i in range(1, 4)]
        fakes = [FakeServer(image_ids=[None, "sha256:new"]) for _ in servers]
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc), \
                mock.patch("deploy.log"), \
                self._patch_remote(fakes):
            failures = self.deployer.prefetch_images(servers)
        self.assertEqual(failures, [])
        for fs in fakes:
            self.assertEqual(len(fs.uploaded), 1)
            self.assertTrue(any("docker load" in c for c in fs.exec_calls))

    def test_parallel_skips_consistent_servers(self):
        """远端已一致的服务器跳过传输，无需 docker save"""
        servers = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]
        fakes = [FakeServer(image_ids=["sha256:new"]),
                 FakeServer(image_ids=["sha256:new"])]
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc) as proc, \
                mock.patch("deploy.log"), \
                self._patch_remote(fakes):
            failures = self.deployer.prefetch_images(servers)
        self.assertEqual(failures, [])
        save_calls = [c for c in proc.call_args_list if c.args[0][0] == "docker"]
        self.assertFalse(save_calls, "远端一致时不应 docker save")
        for fs in fakes:
            self.assertEqual(fs.uploaded, [])
            self.assertEqual(fs.exec_calls, [])

    def test_failures_collected_not_raised(self):
        """单台传输失败：不抛异常，返回失败 host 列表"""
        class FailServer(FakeServer):
            def sftp_put_with_progress(self, local_path, remote_path, label=""):
                raise RuntimeError("sftp timeout")

        servers = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]
        fakes = [FailServer(image_ids=[None, "sha256:new"]),
                 FakeServer(image_ids=[None, "sha256:new"])]
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc), \
                mock.patch("deploy.log"), \
                self._patch_remote(fakes):
            failures = self.deployer.prefetch_images(servers)
        self.assertEqual(failures, ["10.0.0.1"])

    def test_push_only_parallel_summary(self):
        """push_image_only 复用并行预传输并汇总成败"""
        self.deployer.project = "app"
        servers = [{"ip": "10.0.0.1"}, {"ip": "10.0.0.2"}]
        fakes = [FakeServer(image_ids=[None, "sha256:new"]),
                 FakeServer(image_ids=[None, "sha256:new"])]
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.Popen", side_effect=ok_proc), \
                mock.patch("deploy.log"), \
                self._patch_remote(fakes):
            ok = self.deployer.push_image_only(servers)
        self.assertTrue(ok)
        for fs in fakes:
            self.assertEqual(len(fs.uploaded), 1)


class TestRollbackAllParallel(unittest.TestCase):
    """并行全量回滚测试：所有服务器并行回滚，失败不阻断其他服务器"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.deployer = make_deployer(self.tmp)
        self.deployer.registry_mode = "direct"
        self.deployer.ssh_cfg = {}
        self.deployer.previous_version = "1.0.29"
        self.deployer.project = "app"
        self.deployer.env = "test"
        self.deployer.proj_cfg = {}
        self.deployer.service_name = "app"
        self.deployer.nacos = mock.Mock()
        self.deployer.nacos._snapshotted_keys = set()
        self.deployer.config = {}

    def _patch_remote(self, fake_servers):
        """mock RemoteServer：按 IP 序号返回对应的 FakeServer 实例"""
        def fake_remote(host, ssh_cfg):
            idx = int(host.rsplit(".", 1)[1]) - 1
            fake_servers[idx].host = host
            return fake_servers[idx]
        return mock.patch("deploy.RemoteServer", side_effect=fake_remote)

    def _rollback_mocks(self, rollback_side_effect=None):
        """公共 mock 组合：Nacos/健康检查/通知/回滚容器"""
        return [
            mock.patch("deploy.log"),
            mock.patch("deploy.send_notify"),
            mock.patch("deploy.wait_for_healthy", return_value=True),
            mock.patch.object(self.deployer, "nacos_offline"),
            mock.patch.object(self.deployer, "nacos_online", return_value=True),
            mock.patch.object(self.deployer, "rollback_container",
                              side_effect=rollback_side_effect),
        ]

    def test_parallel_rollback_all_servers(self):
        """3 台服务器全部回滚成功：并发度达到服务器数量，结果汇总成功"""
        import time as _time
        lock = threading.Lock()
        state = {"cur": 0, "max": 0}

        def slow_rollback(server):
            with lock:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            _time.sleep(0.15)
            with lock:
                state["cur"] -= 1

        servers = [{"ip": f"10.0.0.{i}"} for i in range(1, 4)]
        fakes = [FakeServer() for _ in servers]
        with contextlib.ExitStack() as stack:
            for p in self._rollback_mocks(slow_rollback):
                stack.enter_context(p)
            stack.enter_context(self._patch_remote(fakes))
            ok = self.deployer.rollback_all(servers)
        self.assertTrue(ok)
        self.assertEqual(state["max"], 3, "3 台服务器应并行回滚（并发度=3）")

    def test_one_failure_does_not_block_others(self):
        """单台回滚失败：不阻断其他服务器，最终返回 False 并汇总"""
        calls = []

        def fail_rollback(server):
            calls.append(server.host)
            if server.host == "10.0.0.1":
                raise RuntimeError("docker compose up 失败")

        servers = [{"ip": f"10.0.0.{i}"} for i in range(1, 4)]
        fakes = [FakeServer() for _ in servers]
        with contextlib.ExitStack() as stack:
            for p in self._rollback_mocks(fail_rollback):
                stack.enter_context(p)
            stack.enter_context(self._patch_remote(fakes))
            ok = self.deployer.rollback_all(servers)
        self.assertFalse(ok)
        self.assertEqual(sorted(calls), ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                         "单台失败不应阻断其他服务器")

    def test_no_previous_version_returns_false(self):
        """未指定 previous_version：直接返回 False，不执行任何回滚"""
        self.deployer.previous_version = None
        servers = [{"ip": "10.0.0.1"}]
        with contextlib.ExitStack() as stack:
            for p in self._rollback_mocks():
                stack.enter_context(p)
            stack.enter_context(self._patch_remote([FakeServer()]))
            ok = self.deployer.rollback_all(servers)
        self.assertFalse(ok)


class TestTarImageId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_parse_manifest(self):
        """从 docker save 的 tar 中解析镜像 ID（config 文件名）"""
        digest = "abcdef" * 10  # 60 位十六进制
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = _json.dumps([{
                "Config": f"{digest}.json",
                "RepoTags": ["app:1.0.30"],
                "Layers": [],
            }]).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        path = Path(self.tmp) / "x.tar"
        path.write_bytes(buf.getvalue())
        self.assertEqual(tar_image_id(path), f"sha256:{digest}")

    def test_invalid_tar_returns_none(self):
        path = Path(self.tmp) / "bad.tar"
        path.write_bytes(b"not a tar")
        self.assertIsNone(tar_image_id(path))


if __name__ == "__main__":
    unittest.main()
