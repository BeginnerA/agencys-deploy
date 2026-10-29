"""node_process/python_process 策略（本文件 Task 9 先覆盖 node；Task 10 追加 python）"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY
from deploy_strategies.node_process import NodeProcessStrategy
from deploy_strategies.process_config import sha256_file

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


def make_deployer(tmp_dir: str, version="1.0.0", runtime_config=None) -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "nsvc"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0}}
    d.ssh_cfg = {}
    d.proj_cfg = {"deploy_mode": "node_process", "image_name": "nsvc",
                  "runtime_config": runtime_config or {
                      "app_home": APP_HOME, "start_file": "dist/server.js"}}
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


class NodeRegistryTest(unittest.TestCase):
    def test_registered_and_capabilities(self):
        self.assertIn("node_process", _REGISTRY)
        s = create_strategy("node_process", make_deployer(tempfile.mkdtemp()))
        self.assertIsInstance(s, NodeProcessStrategy)
        self.assertEqual(s.artifact_kind, "archive")
        self.assertTrue(s.supports_nacos and s.supports_canary)

    def test_node_probe(self):
        s = create_strategy("node_process", make_deployer(tempfile.mkdtemp()))
        r = FakeRemote()
        s.runtime_probe(r)
        self.assertTrue(any("command -v /usr/bin/node" in c for c in r.exec_calls))


class NodeTransferTest(unittest.TestCase):
    def test_archive_upload_and_chmod_hook(self):
        tmp = tempfile.mkdtemp(prefix="node_svc_")
        d = make_deployer(tmp)
        pkg = make_pkg(d)
        s = create_strategy("node_process", d)
        remote = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"cat ": "",
                                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        joined = "\n".join(remote.exec_calls)
        self.assertIn("tar -xzf", joined)
        # 解压后、就位前修 .bin 执行位（Windows 打包丢 +x 的保险）
        self.assertTrue(any("chmod -R u+x" in c and "node_modules/.bin" in c
                            for c in remote.exec_calls))
        # post_extract 钩子收到的是暂存目录（Task 8 契约：解压到 {ver}.tmp 后就位），
        # 因此 chmod 目标必然是 .tmp 形态；就位后权限随目录整体迁移
        self.assertIn(f"{APP_HOME}/releases/1.0.0.tmp/node_modules/.bin", joined)


from deploy_strategies.python_process import PythonProcessStrategy  # noqa: E402


def make_py_deployer(tmp_dir: str, version="1.0.0") -> Deployer:
    d = make_deployer(tmp_dir, version=version)
    d.proj_cfg = {"deploy_mode": "python_process", "image_name": "psvc",
                  "runtime_config": {"app_home": APP_HOME,
                                     "start_file": "main.py",
                                     "python_version": "3.11"}}
    d.project = "psvc"
    return d


class PythonRegistryTest(unittest.TestCase):
    def test_registered_and_family_complete(self):
        self.assertIn("python_process", _REGISTRY)
        from deploy_strategies.base import process_modes
        self.assertEqual(process_modes(),
                         ["go_binary", "jar_service", "node_process",
                          "python_process", "rust_binary"])

    def test_python_probe_checks_ensurepip(self):
        s = create_strategy("python_process", make_py_deployer(tempfile.mkdtemp()))
        r = FakeRemote()
        s.runtime_probe(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn("command -v python3", joined)
        self.assertIn("python3 -m ensurepip", joined)


class PythonVenvTest(unittest.TestCase):
    def test_first_deploy_creates_venv_and_installs(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": False},
                       rules={"sha256sum": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"python3 -m venv {APP_HOME}/venv", joined)
        self.assertIn("--no-index", joined)
        self.assertIn(f"--find-links {APP_HOME}/releases/1.0.0/wheels", joined)
        self.assertIn(f"-r {APP_HOME}/releases/1.0.0/requirements.txt", joined)
        self.assertIn(".req_hash", joined)

    def test_hash_match_skips_install(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(rules={"sha256sum": "a" * 64, "cat ": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        self.assertFalse(any("pip install" in c for c in r.exec_calls))
        self.assertFalse(any("python3 -m venv" in c for c in r.exec_calls))

    def test_requirements_change_reinstalls_without_recreate(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True},
                       rules={"sha256sum": "b" * 64, "cat ": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        joined = "\n".join(r.exec_calls)
        self.assertNotIn("python3 -m venv", joined)
        self.assertIn("pip install", joined)
        self.assertIn("rm -f", joined)  # 先清旧标记再写新标记


if __name__ == "__main__":
    unittest.main()
