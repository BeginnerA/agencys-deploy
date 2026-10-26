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


class TestCheckPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy
        make_jar(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_commands(self):
        server = FakeRemote(rules={"df -Pk": "1048576\n"})
        self.s.check(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn("command -v /usr/bin/java", joined)
        self.assertIn("command -v systemctl", joined)
        self.assertIn(f"mkdir -p {APP_HOME}/releases", joined)

    def test_check_disk_insufficient_raises(self):
        # 默认测试 jar 仅 9 字节 → 需要 1KB；df 返回 0 必然不足
        server = FakeRemote(rules={"df -Pk": "0\n"})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check(server)
        self.assertIn("磁盘空间不足", str(ctx.exception))

    def test_prepare_requires_installed_unit(self):
        server = FakeRemote(try_exec_ok={"systemctl cat": False})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.prepare(server)
        self.assertIn("未接入", str(ctx.exception))

    def test_prepare_rejects_foreign_unit(self):
        server = FakeRemote(rules={
            "systemctl show": "ExecStart={ path=/usr/local/bin/java ; argv[]=/usr/local/bin/java -jar /srv/other.jar }",
        })
        with self.assertRaises(RuntimeError) as ctx:
            self.s.prepare(server)
        self.assertIn("拒绝接管", str(ctx.exception))

    def test_prepare_accepts_platform_unit(self):
        server = FakeRemote(rules={
            "systemctl show": f"ExecStart={{ path=/usr/bin/java ; argv[]=/usr/bin/java -jar {APP_HOME}/CURRENT/app.jar }}",
        })
        self.s.prepare(server)


class TestTransferPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy
        self.jar = make_jar(self.d)
        self.sha = sha256_file(self.jar)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uploads_to_version_dir_and_verifies_sha(self):
        server = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": self.sha + "\n"})
        self.assertTrue(self.s.transfer(server))
        local, remote, _ = server.uploaded[0]
        self.assertTrue(local.endswith("hqb-auth-1.0.31.jar"))
        self.assertEqual(remote, f"{APP_HOME}/releases/1.0.31/app.jar")
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"mkdir -p {APP_HOME}/releases/1.0.31", joined)

    def test_skips_when_remote_sha_matches(self):
        server = FakeRemote(try_exec_ok={"test -f": True},
                            rules={"sha256sum": self.sha + "\n"})
        self.s.transfer(server)
        self.assertEqual(server.uploaded, [])

    def test_remote_sha_mismatch_after_upload_raises(self):
        server = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": "deadbeef\n"})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.transfer(server)
        self.assertIn("校验和不一致", str(ctx.exception))

    def test_missing_local_jar_raises(self):
        self.jar.unlink()
        with self.assertRaises(RuntimeError):
            self.s.transfer(FakeRemote())


class TestPrefetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        make_jar(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefetch_returns_failed_hosts(self):
        ok = FakeRemote(host="10.0.0.1", try_exec_ok={"test -f": False},
                        rules={"sha256sum": sha256_file(
                            Path(self.tmp) / "hqb-auth-1.0.31.jar") + "\n"})
        bad = FakeRemote(host="10.0.0.2",
                         rules={"sftp": RuntimeError("disk full")})
        bad.sftp_put_with_progress = mock.Mock(side_effect=RuntimeError("disk full"))
        with mock.patch.object(deploy_mod, "RemoteServer",
                               side_effect=[ok, bad]), \
                mock.patch("deploy_strategies.jar_service.log"):
            failures = self.d.strategy.prefetch([{"ip": "10.0.0.1"},
                                                 {"ip": "10.0.0.2"}])
        self.assertEqual(failures, ["10.0.0.2"])


if __name__ == "__main__":
    unittest.main()
