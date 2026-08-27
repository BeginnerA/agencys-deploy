# tests/test_image_sync.py
"""直传模式镜像同步测试：同版本重复发布时以镜像 digest 决定是否传输"""
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


def ok_run(*args, **kwargs):
    """模拟成功执行的 subprocess.run 返回值"""
    r = mock.Mock()
    r.returncode = 0
    r.stdout = ""
    r.stderr = ""
    return r


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
                mock.patch("deploy.subprocess.run", side_effect=ok_run) as run, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in run.call_args_list if c.args[0][0] == "docker"]
        self.assertTrue(save_calls, "同 tag 不同 digest 必须重新 docker save")
        self.assertEqual(len(server.uploaded), 1)
        self.assertTrue(any("docker load" in c for c in server.exec_calls))

    def test_remote_missing_transfers(self):
        """远端无镜像（首次发布）：正常传输并在 load 后校验 digest"""
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.run", side_effect=ok_run), \
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
        tar = Path(self.tmp) / "app_1.0.30.tar"
        tar.write_bytes(b"fake")
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.run", side_effect=ok_run) as run, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in run.call_args_list if c.args[0][0] == "docker"]
        self.assertFalse(save_calls, "tar 与本地一致时应复用，不应 docker save")

    def test_tar_stale_reexports(self):
        """tar 是旧代码导出（digest 不一致）：重新 docker save 覆盖"""
        tar = Path(self.tmp) / "app_1.0.30.tar"
        tar.write_bytes(b"fake")
        server = FakeServer(image_ids=[None, "sha256:new"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.tar_image_id", return_value="sha256:old"), \
                mock.patch("deploy.subprocess.run", side_effect=ok_run) as run, \
                mock.patch("deploy.log"):
            self.deployer.ensure_image_ready(server)
        save_calls = [c for c in run.call_args_list if c.args[0][0] == "docker"]
        self.assertTrue(save_calls, "tar 与本地不一致时必须重新 docker save")

    def test_load_mismatch_raises(self):
        """load 后远端 digest 与本地仍不一致：报错"""
        server = FakeServer(image_ids=[None, "sha256:other"])
        with mock.patch("deploy.local_image_id", return_value="sha256:new"), \
                mock.patch("deploy.subprocess.run", side_effect=ok_run), \
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
