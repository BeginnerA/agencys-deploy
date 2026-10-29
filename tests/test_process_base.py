"""archive 传输路径：上传→复核→解压→钩子→原子就位→清理，以及幂等/残骸重建"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies.process_config import sha256_file
from deploy_strategies.process_service import ProcessServiceStrategy

APP_HOME = "/opt/apps/nsvc"


class FakeRemote:
    def __init__(self, rules=None, try_exec_ok=None):
        self.host = "10.0.0.1"
        self.exec_calls = []
        self.uploaded = []
        self.rules = rules or {}
        self.try_exec_ok = try_exec_ok or {}

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.exec_calls.append(cmd)
        for key, val in self.rules.items():
            if key in cmd:
                if isinstance(val, Exception):
                    raise val
                return val
        return ""

    def try_exec(self, cmd, timeout=60):
        self.exec_calls.append(cmd)
        for key, val in self.try_exec_ok.items():
            if key in cmd:
                return val
        return True

    def sftp_put_with_progress(self, local_path, remote_path, label="", streams=1):
        self.uploaded.append((local_path, remote_path, label))


class _ArchiveSvc(ProcessServiceStrategy):
    """测试用 archive 形态（不 @register，避免污染 process_modes）"""
    name = "_test_archive"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None


def make_deployer(tmp_dir: str, version="1.0.0") -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "nsvc"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0}}
    d.ssh_cfg = {}
    # 解析按 node_process 形态（archive 家族），避免未注册模式解析为空
    d.proj_cfg = {"deploy_mode": "node_process", "image_name": "nsvc",
                  "runtime_config": {"app_home": APP_HOME,
                                     "start_file": "server.js"}}
    d.dist_dir = Path(tmp_dir)
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_pkg(deployer: Deployer, version=None, payload=b"tar-bytes"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"nsvc-{version}.tar.gz"
    p.write_bytes(payload)
    p.with_name(p.name + ".sha256").write_text(sha256_file(p) + "\n",
                                               encoding="utf-8")
    return p


class ArchiveTransferTest(unittest.TestCase):
    def _svc(self, tmp):
        d = make_deployer(tmp)
        return _ArchiveSvc(d), d

    def test_upload_extract_finalize(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        extracted = []
        s.post_extract = lambda server, tmp_dir, ver: extracted.append(tmp_dir)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "",  # .sha256 标记缺失 → 不跳过
                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded[0][1], f"{APP_HOME}/releases/1.0.0.tar.gz")
        joined = "\n".join(remote.exec_calls)
        self.assertIn(f"tar -xzf {APP_HOME}/releases/1.0.0.tar.gz", joined)
        self.assertIn(f"mv -Tf {APP_HOME}/releases/1.0.0.tmp {APP_HOME}/releases/1.0.0", joined)
        self.assertIn("echo ", joined)  # .sha256 写入
        self.assertIn(f"rm -f {APP_HOME}/releases/1.0.0.tar.gz", joined)
        self.assertEqual(extracted, [f"{APP_HOME}/releases/1.0.0.tmp"])

    def test_skips_when_marker_matches(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        remote = FakeRemote(rules={".sha256": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded, [])

    def test_rebuilds_on_marker_mismatch(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "deadbeef",  # 旧标记与本次不符
                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        joined = "\n".join(remote.exec_calls)
        self.assertIn(f"rm -rf {APP_HOME}/releases/1.0.0.tmp", joined)
        self.assertIn(f"rm -rf {APP_HOME}/releases/1.0.0 &&", joined)

    def test_upload_sha_mismatch_fails(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        make_pkg(d)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "", "sha256sum": "0" * 64})
        with self.assertRaises(RuntimeError):
            s.transfer(remote)
        self.assertFalse(any("mv -Tf" in c for c in remote.exec_calls))


if __name__ == "__main__":
    unittest.main()
