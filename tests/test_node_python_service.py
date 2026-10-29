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
from deploy_strategies.process_config import validate_runtime_config  # noqa: E402


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
        self.assertIn("rm -f", joined)  # 安装前清旧标记、成功后写新标记


class FlakyProbeRemote(FakeRemote):
    """按顺序返回指定探测结果，模拟 SSH 抖动下的「首次 False、复探 True」"""

    def __init__(self, probe_key, results, **kwargs):
        super().__init__(**kwargs)
        self._probe_key = probe_key
        self._results = list(results)
        self.probe_calls = 0

    def try_exec(self, cmd, timeout=60):
        if self._probe_key in cmd:
            self.probe_calls += 1
            self.exec_calls.append(cmd)
            return self._results.pop(0) if self._results else True
        return super().try_exec(cmd, timeout)


class PythonVenvHardeningTest(unittest.TestCase):
    """venv 生命周期加固：标记顺序、依赖清单缺失、空哈希、损坏 venv 重建"""

    def _svc(self):
        tmp = tempfile.mkdtemp(prefix="py_hard_")
        return create_strategy("python_process", make_py_deployer(tmp))

    def test_marker_cleared_before_install(self):
        s = self._svc()
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True},
                       rules={"sha256sum": "b" * 64, "cat ": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        rm_idx = next(i for i, c in enumerate(r.exec_calls)
                      if c.startswith("rm -f ") and ".req_hash" in c)
        pip_idx = next(i for i, c in enumerate(r.exec_calls) if "pip install" in c)
        # 先清标记再安装：安装中断时不能留下上一版本的「已装」指纹
        self.assertLess(rm_idx, pip_idx)

    def test_missing_requirements_raises_without_install(self):
        s = self._svc()
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True, "requirements.txt": False},
                       rules={"sha256sum": ""})
        with self.assertRaises(RuntimeError) as ctx:
            s.ensure_runtime(r, "1.0.0")
        self.assertIn(f"{APP_HOME}/releases/1.0.0/requirements.txt",
                      str(ctx.exception))
        self.assertIn("发布包必须包含", str(ctx.exception))
        self.assertFalse(any("pip install" in c for c in r.exec_calls))

    def test_empty_hash_raises_instead_of_silent_skip(self):
        s = self._svc()
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True}, rules={"sha256sum": ""})
        with self.assertRaises(RuntimeError) as ctx:
            s.ensure_runtime(r, "1.0.0")
        self.assertIn("哈希", str(ctx.exception))
        self.assertFalse(any("pip install" in c for c in r.exec_calls))

    def test_broken_venv_recreated_and_installed(self):
        s = self._svc()
        # pyvenv.cfg 仍在但解释器不可用（系统 Python 升级后的典型形态）
        r = FakeRemote(
            try_exec_ok={"bin/python -V": False, "pyvenv.cfg": True,
                         "test -d": True},
            rules={"sha256sum": "c" * 64, "cat ": ""})
        s.ensure_runtime(r, "1.0.0")
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"rm -rf {APP_HOME}/venv", joined)
        self.assertIn(f"python3 -m venv {APP_HOME}/venv", joined)
        self.assertIn("pip install", joined)
        rm_idx = next(i for i, c in enumerate(r.exec_calls)
                      if f"rm -rf {APP_HOME}/venv" in c)
        venv_idx = next(i for i, c in enumerate(r.exec_calls)
                        if "python3 -m venv" in c)
        self.assertLess(rm_idx, venv_idx)

    def test_unhealthy_on_both_probes_rebuilds(self):
        s = self._svc()
        r = FlakyProbeRemote("pyvenv.cfg", [False, False],
                             try_exec_ok={"test -d": True},
                             rules={"sha256sum": "c" * 64, "cat ": ""})
        with mock.patch("deploy_strategies.python_process.log"):
            s.ensure_runtime(r, "1.0.0")
        self.assertEqual(r.probe_calls, 2, "两次探测都不可用才允许重建")
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"rm -rf {APP_HOME}/venv", joined)
        self.assertIn(f"python3 -m venv {APP_HOME}/venv", joined)
        rm_idx = next(i for i, c in enumerate(r.exec_calls)
                      if f"rm -rf {APP_HOME}/venv" in c)
        venv_idx = next(i for i, c in enumerate(r.exec_calls)
                        if "python3 -m venv" in c)
        self.assertLess(rm_idx, venv_idx)

    def test_transient_probe_failure_keeps_healthy_venv(self):
        s = self._svc()
        # 首次探测 False（SSH 抖动），复探 True → 不得 rm -rf、不得重建，
        # 依赖哈希一致时连 pip install 都跳过
        r = FlakyProbeRemote("pyvenv.cfg", [False, True],
                             rules={"sha256sum": "a" * 64, "cat ": "a" * 64})
        with mock.patch("deploy_strategies.python_process.log") as log_mock:
            s.ensure_runtime(r, "1.0.0")
        self.assertEqual(r.probe_calls, 2)
        joined = "\n".join(r.exec_calls)
        self.assertNotIn(f"rm -rf {APP_HOME}/venv", joined)
        self.assertNotIn("python3 -m venv", joined)
        self.assertNotIn("pip install", joined)
        self.assertTrue(any("复探" in str(c) for c in log_mock.call_args_list))


class PythonRollbackTest(unittest.TestCase):
    def _svc(self):
        d = make_py_deployer(tempfile.mkdtemp(prefix="py_rb_"))
        d.previous_version = "0.9.0"
        return create_strategy("python_process", d), d

    def test_rollback_installs_before_restart_and_never_docker(self):
        s, _ = self._svc()
        r = FakeRemote(try_exec_ok={"test -d": True},
                       rules={"sha256sum": "b" * 64, "cat ": "a" * 64})
        self.assertTrue(s.rollback(r), "回滚确实执行时必须返回 True")
        joined = "\n".join(r.exec_calls)
        pip_idx = next(i for i, c in enumerate(r.exec_calls) if "pip install" in c)
        restart_idx = next(i for i, c in enumerate(r.exec_calls)
                           if "systemctl restart" in c)
        self.assertLess(pip_idx, restart_idx)
        self.assertIn(f"ln -sfn {APP_HOME}/releases/0.9.0", joined)
        self.assertIn(f"echo 0.9.0 > {APP_HOME}/.deploy_version", joined)
        self.assertNotIn("docker", joined)

    def test_rollback_missing_wheels_stops_nothing(self):
        s, d = self._svc()
        r = FakeRemote(try_exec_ok={"wheels": False})
        with mock.patch("deploy_strategies.python_process.log") as log_mock:
            self.assertFalse(s.rollback(r),
                             "前置检查阻止回滚时必须返回 False，否则汇总会误报已回滚")
        joined = "\n".join(r.exec_calls)
        self.assertNotIn("systemctl", joined)
        self.assertNotIn("ln -sfn", joined)
        self.assertNotIn("mv -Tf", joined)
        self.assertFalse(getattr(d, "target_modified", False))
        self.assertTrue(any("wheels" in str(c) for c in log_mock.call_args_list))

    def test_rollback_without_previous_version_returns_true(self):
        d = make_py_deployer(tempfile.mkdtemp(prefix="py_rb_skip_"))
        s = create_strategy("python_process", d)
        r = FakeRemote()
        with mock.patch("deploy_strategies.python_process.log"):
            self.assertTrue(s.rollback(r), "无回滚版本属于既有「跳过」语义，按成功上报")
        self.assertEqual(r.exec_calls, [])


class DeployServerRollbackStatusTest(unittest.TestCase):
    """发布失败后的汇总状态必须如实反映回滚是否真的执行（deploy.deploy_server）"""

    class _Server:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

    def _run(self, rollback_result):
        d = make_py_deployer(tempfile.mkdtemp(prefix="py_status_"))
        d.previous_version = "0.9.0"

        def _boom(server):
            d.target_modified = True  # 与真实 switch 一致：换链接即视为已变更
            raise RuntimeError("start failed")

        strategy = mock.Mock(supports_nacos=False)
        strategy.switch.side_effect = _boom
        strategy.rollback.return_value = rollback_result
        d._strategy = strategy
        with mock.patch("deploy.RemoteServer", self._Server), \
                mock.patch("deploy.log") as log_mock:
            ok = d.deploy_server("10.0.0.1")
        messages = "\n".join(str(c.args[0]) for c in log_mock.call_args_list if c.args)
        return ok, strategy, messages

    def test_preflight_blocked_is_not_reported_as_rolled_back(self):
        ok, strategy, messages = self._run(False)
        self.assertFalse(ok)
        strategy.rollback.assert_called_once()
        self.assertIn("回滚被前置检查阻止", messages)
        self.assertNotIn("已回滚", messages)

    def test_rolled_back_and_legacy_none_still_report_rolled_back(self):
        for result in (True, None):  # None：container/static 策略的历史返回值
            with self.subTest(result=result):
                ok, _, messages = self._run(result)
                self.assertFalse(ok)
                self.assertIn("发布失败（已回滚）", messages)
                self.assertNotIn("被前置检查阻止", messages)


class SwitchOrderingTest(unittest.TestCase):
    @staticmethod
    def _assert_ordered(calls, needles):
        idx = []
        for needle in needles:
            i = next((k for k, c in enumerate(calls) if needle in c), -1)
            if i < 0:
                raise AssertionError(f"missing command: {needle}")
            idx.append(i)
        if idx != sorted(idx):
            raise AssertionError(f"wrong order: {needles} -> {idx}")

    def test_node_switch_order_without_docker(self):
        d = make_deployer(tempfile.mkdtemp(prefix="node_sw_"))
        s = create_strategy("node_process", d)
        r = FakeRemote()
        s.switch(r)
        self._assert_ordered(r.exec_calls, [
            "systemctl stop", f"ln -sfn {APP_HOME}/releases/1.0.0 ",
            "mv -Tf", "systemctl start", ".deploy_version"])
        self.assertFalse(any("docker" in c for c in r.exec_calls))

    def test_python_switch_order_includes_ensure_runtime(self):
        d = make_py_deployer(tempfile.mkdtemp(prefix="py_sw_"))
        s = create_strategy("python_process", d)
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True},
                       rules={"sha256sum": "b" * 64, "cat ": ""})
        s.switch(r)
        self._assert_ordered(r.exec_calls, [
            "systemctl stop", f"ln -sfn {APP_HOME}/releases/1.0.0 ",
            "mv -Tf", "pip install", "systemctl start", ".deploy_version"])
        self.assertFalse(any("docker" in c for c in r.exec_calls))


class RuntimeConfigValidationTest(unittest.TestCase):
    def test_requirements_rejects_dotdot_and_whitespace(self):
        base = {"app_home": APP_HOME, "start_file": "main.py"}
        for bad in ("../x.txt", "a b.txt", "conf/a\tb.txt"):
            errs = validate_runtime_config(dict(base, requirements=bad),
                                           "python_process")
            self.assertTrue(any("runtime_config.requirements" in e for e in errs),
                            bad)
        self.assertEqual(validate_runtime_config(
            dict(base, requirements="conf/requirements.txt"), "python_process"), [])

    def test_node_bin_rejects_control_chars(self):
        base = {"app_home": APP_HOME, "start_file": "dist/server.js"}
        for bad in ("/usr/bin/no\nde", "/usr/bin/no de", "/usr/bin/no\tde"):
            errs = validate_runtime_config(dict(base, node_bin=bad),
                                           "node_process")
            self.assertTrue(any("runtime_config.node_bin" in e for e in errs),
                            repr(bad))
        self.assertEqual(validate_runtime_config(
            dict(base, node_bin="/usr/local/bin/node"), "node_process"), [])


if __name__ == "__main__":
    unittest.main()
