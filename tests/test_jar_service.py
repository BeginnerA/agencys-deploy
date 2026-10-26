"""jar_service 策略探针：Fake SSH 断言命令序列，不触真实服务器"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy as deploy_mod
from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY
from deploy_strategies.jar_config import sha256_file
from deploy_strategies.jar_service import JarServiceStrategy

APP_HOME = "/opt/apps/hqb-auth"


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


def make_jar_deployer(tmp_dir: str, version="1.0.31", previous_version=None,
                      runtime_config=None) -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "auth"
    d.version = version
    d.previous_version = previous_version
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0, "health_timeout": "1",
                           "health_interval": "0"}}
    d.ssh_cfg = {}
    d.proj_cfg = {
        "deploy_mode": "jar_service",
        "image_name": "hqb-auth",
        "runtime_config": runtime_config or {
            "app_home": APP_HOME, "java_bin": "/usr/bin/java", "port": 8080,
            "readiness": {"type": "process"},
        },
    }
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_jar(deployer: Deployer, version=None, payload=b"jar-bytes"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"hqb-auth-{version}.jar"
    p.write_bytes(payload)
    (p.with_name(p.name + ".sha256")).write_text(sha256_file(p) + "\n", encoding="utf-8")
    return p


class TestRegistry(unittest.TestCase):
    def test_registered_and_routed(self):
        self.assertIn("jar_service", _REGISTRY)
        d = make_jar_deployer(tempfile.mkdtemp())
        self.assertIsInstance(d.strategy, JarServiceStrategy)

    def test_capability_flags(self):
        s = create_strategy("jar_service", make_jar_deployer(tempfile.mkdtemp()))
        self.assertTrue(s.supports_nacos)
        self.assertTrue(s.supports_canary)
        self.assertTrue(s.supports_prefetch)
        self.assertTrue(s.supports_restart)
        self.assertEqual(s.version_scheme, "semver")

    def test_app_home_required(self):
        d = make_jar_deployer(tempfile.mkdtemp(), runtime_config={})
        d.proj_cfg["runtime_config"] = {}
        with self.assertRaises(ValueError) as ctx:
            create_strategy("jar_service", d)
        self.assertIn("app_home", str(ctx.exception))

    def test_defaults(self):
        s = create_strategy("jar_service", make_jar_deployer(tempfile.mkdtemp()))
        self.assertEqual(s.app_home, APP_HOME)
        self.assertEqual(s.unit, "hqb-auth")
        self.assertEqual(s.jar_name("1.0.31"), "hqb-auth-1.0.31.jar")


class TestCheckLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_jar_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("本地 jar 不存在", str(ctx.exception))

    def test_missing_sidecar_raises(self):
        p = Path(self.tmp) / "hqb-auth-1.0.31.jar"
        p.write_bytes(b"x")
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("校验文件", str(ctx.exception))

    def test_checksum_mismatch_raises(self):
        p = make_jar(self.d)
        p.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("校验和不一致", str(ctx.exception))

    def test_ok(self):
        make_jar(self.d)
        self.s.check_local()


class TestVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_next_version_bumps_patch(self):
        self.assertEqual(self.d.strategy.next_version("1.0.29"), "1.0.30")

    def test_next_version_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.d.strategy.next_version("20261012")

    def _query(self, rules):
        fake = FakeRemote(rules=rules)
        with mock.patch("deploy_strategies.jar_service.default_servers_file",
                        return_value="servers.test.yaml"), \
                mock.patch("deploy_strategies.jar_service.load_yaml",
                           return_value={"servers": [{"ip": "10.0.0.1"}]}), \
                mock.patch("deploy_strategies.jar_service.get_ssh_config",
                           return_value={}), \
                mock.patch("deploy_strategies.jar_service.merge_server_ssh",
                           return_value={}), \
                mock.patch.object(deploy_mod, "RemoteServer", return_value=fake):
            return self.d.strategy.query_current_version(), fake

    def test_query_reads_marker(self):
        ver, _ = self._query({"cat": "1.0.30\n"})
        self.assertEqual(ver, "1.0.30")

    def test_query_none_when_no_marker(self):
        ver, _ = self._query({"cat": ""})
        self.assertIsNone(ver)


if __name__ == "__main__":
    unittest.main()
