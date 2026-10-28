"""go_binary/rust_binary 策略：注册、五阶段命令序列、chmod 与接管门槛（Fake SSH）"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY, process_modes
from deploy_strategies.go_binary import GoBinaryStrategy
from deploy_strategies.process_config import sha256_file
from deploy_strategies.rust_binary import RustBinaryStrategy

APP_HOME = "/opt/apps/report"


class FakeRemote:
    """模拟 RemoteServer：exec_calls 记录命令；rules 按子串匹配返回值/失败"""

    def __init__(self, host="10.0.0.1", rules=None, try_exec_ok=None):
        self.host = host
        self.exec_calls = []
        self.uploaded = []
        self.closed = False
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

    def close(self):
        self.closed = True


def make_deployer(tmp_dir: str, mode="go_binary", version="1.0.0") -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "report"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0, "health_timeout": "1",
                           "health_interval": "0"}}
    d.ssh_cfg = {}
    d.proj_cfg = {"deploy_mode": mode, "image_name": "report",
                  "runtime_config": {"app_home": APP_HOME}}
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_binary(deployer: Deployer, version=None, payload=b"\x7fELFbin"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"report-{version}"
    p.write_bytes(payload)
    p.with_name(p.name + ".sha256").write_text(sha256_file(p) + "\n",
                                               encoding="utf-8")
    return p


class RegistryTest(unittest.TestCase):
    def test_registered_and_routed(self):
        self.assertIn("go_binary", _REGISTRY)
        self.assertIn("rust_binary", _REGISTRY)
        self.assertIsInstance(
            create_strategy("go_binary", make_deployer(tempfile.mkdtemp())),
            GoBinaryStrategy)
        self.assertIsInstance(
            create_strategy("rust_binary",
                            make_deployer(tempfile.mkdtemp(), mode="rust_binary")),
            RustBinaryStrategy)

    def test_process_modes_contains_both(self):
        modes = process_modes()
        self.assertIn("go_binary", modes)
        self.assertIn("rust_binary", modes)

    def test_capability_flags(self):
        s = create_strategy("go_binary", make_deployer(tempfile.mkdtemp()))
        self.assertTrue(s.supports_nacos)
        self.assertTrue(s.supports_canary)
        self.assertTrue(s.supports_prefetch)
        self.assertTrue(s.supports_restart)
        self.assertEqual(s.version_scheme, "semver")

    def test_app_home_required(self):
        d = make_deployer(tempfile.mkdtemp())
        d.proj_cfg["runtime_config"] = {}
        with self.assertRaises(ValueError) as ctx:
            create_strategy("go_binary", d)
        self.assertIn("app_home", str(ctx.exception))


class TransferTest(unittest.TestCase):
    def test_upload_then_chmod(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        bin_path = make_binary(d)
        s = create_strategy("go_binary", d)
        remote = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": sha256_file(bin_path)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded[0][1], f"{APP_HOME}/releases/1.0.0/app")
        self.assertTrue(any(c.startswith("chmod 0755") for c in remote.exec_calls))

    def test_skips_when_remote_sha_matches(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        bin_path = make_binary(d)
        s = create_strategy("go_binary", d)
        remote = FakeRemote(rules={"sha256sum": sha256_file(bin_path)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded, [])


class CheckPrepareTest(unittest.TestCase):
    def test_check_has_no_runtime_probe_and_no_java(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        make_binary(d)
        s = create_strategy("go_binary", d)
        r = FakeRemote(rules={"df -Pk": "1048576"})
        s.check(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn("command -v systemctl", joined)
        self.assertNotIn("java", joined)

    def test_prepare_rejects_foreign_exec_start(self):
        d = make_deployer(tempfile.mkdtemp())
        s = create_strategy("go_binary", d)
        r = FakeRemote(rules={"systemctl show -p ExecStart":
                              "ExecStart={ path=/bin/other }"})
        with self.assertRaises(RuntimeError) as ctx:
            s.prepare(r)
        self.assertIn("拒绝接管", str(ctx.exception))
        self.assertIn(f"{APP_HOME}/CURRENT/app", str(ctx.exception))

    def test_prepare_missing_unit(self):
        d = make_deployer(tempfile.mkdtemp())
        s = create_strategy("go_binary", d)
        r = FakeRemote(try_exec_ok={"systemctl cat": False})
        with self.assertRaises(RuntimeError) as ctx:
            s.prepare(r)
        self.assertIn("未接入", str(ctx.exception))


class SwitchRollbackTest(unittest.TestCase):
    def test_switch_symlink_marker_and_version(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp, version="1.0.1")
        s = create_strategy("go_binary", d)
        r = FakeRemote()
        s.switch(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"ln -sfn {APP_HOME}/releases/1.0.1", joined)
        self.assertIn("mv -Tf", joined)
        self.assertIn("systemctl start", joined)
        self.assertIn(".deploy_version", joined)
        self.assertTrue(d.target_modified)

    def test_rollback_without_source_skips(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        d.previous_version = "0.9.0"
        s = create_strategy("go_binary", d)
        r = FakeRemote(try_exec_ok={"test -d": False})
        s.rollback(r)
        self.assertFalse(d.target_modified)
        self.assertFalse(any("systemctl restart" in c for c in r.exec_calls))

    def test_rollback_source_missing_flag(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        d.previous_version = "0.9.0"
        s = create_strategy("rust_binary", d)
        self.assertTrue(s.rollback_source_missing(
            FakeRemote(try_exec_ok={"test -d": False})))
        self.assertFalse(s.rollback_source_missing(FakeRemote()))


if __name__ == "__main__":
    unittest.main()
