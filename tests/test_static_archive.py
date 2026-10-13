# tests/test_static_archive.py
"""静态压缩包发布策略探针：用 Fake SSH 断言命令序列（不触真实服务器）

覆盖：注册路由与能力标志、日期版本推导、预检/上传幂等/原子切换命令链、
验证退化路径、回滚快速与慢速路径、deploy_server 端到端（Nacos 零调用）。
"""
import contextlib
import io
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy as deploy_mod  # noqa: E402
from deploy import Deployer  # noqa: E402
from deploy_strategies import create_strategy  # noqa: E402
from deploy_strategies.base import _REGISTRY  # noqa: E402
from deploy_strategies.static_archive import StaticArchiveStrategy  # noqa: E402

SITE_ROOT = "/data/1panel/www/sites/example.com/index"
SITE_PARENT = "/data/1panel/www/sites/example.com"


class FakeRemote:
    """模拟 RemoteServer：exec_calls 记录命令；rules 按子串匹配定制返回值/失败"""

    def __init__(self, host="10.0.0.1", rules=None, try_exec_ok=None):
        self.host = host
        self.exec_calls = []
        self.uploaded = []
        self.closed = False
        self.rules = rules or {}            # {子串: 返回字符串 或 Exception}
        self.try_exec_ok = try_exec_ok or {}  # {子串: bool}

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


def make_static_deployer(tmp_dir: str, version="20261012",
                         previous_version=None) -> Deployer:
    """绕过 __init__ 构造静态模式最小 Deployer（与 make_deployer 同风格）"""
    d = Deployer.__new__(Deployer)
    d.project = "creator-web"
    d.version = version
    d.previous_version = previous_version
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0, "health_timeout": "1", "health_interval": "0"}}
    d.ssh_cfg = {}
    d.proj_cfg = {
        "deploy_mode": "static_archive",
        "site_root": SITE_ROOT,
        "zip_prefix": "达人端",
        "build_type": "frontend",
    }
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d.upload_streams = 2
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_zip(deployer: Deployer, version=None):
    version = version or deployer.version
    zip_path = Path(deployer.dist_dir) / f"达人端-{version}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("index.html", "<html></html>")
    return zip_path


class TestRegistry(unittest.TestCase):
    def test_registered_and_routed(self):
        self.assertIn("static_archive", _REGISTRY)
        d = make_static_deployer(tempfile.mkdtemp())
        self.assertIsInstance(d.strategy, StaticArchiveStrategy)

    def test_capability_flags(self):
        s = create_strategy("static_archive", make_static_deployer(tempfile.mkdtemp()))
        self.assertFalse(s.supports_nacos)
        self.assertFalse(s.supports_restart)
        self.assertTrue(s.supports_prefetch)
        self.assertEqual(s.version_scheme, "date")

    def test_site_root_required(self):
        d = make_static_deployer(tempfile.mkdtemp())
        d.proj_cfg = {"deploy_mode": "static_archive"}
        with self.assertRaises(ValueError) as ctx:
            create_strategy("static_archive", d)
        self.assertIn("site_root", str(ctx.exception))

    def test_defaults(self):
        s = create_strategy("static_archive", make_static_deployer(tempfile.mkdtemp()))
        self.assertEqual(s.backup_dir, f"{SITE_PARENT}/backup")
        self.assertEqual(s.entry_file, "index.html")
        self.assertEqual(s.file_mode, "0644")
        self.assertEqual(s.zip_name("20261012"), "达人端-20261012.zip")


class TestDateVersion(unittest.TestCase):
    def setUp(self):
        self.s = create_strategy("static_archive", make_static_deployer(tempfile.mkdtemp()))

    def test_next_version_first_deploy(self):
        self.assertEqual(self.s.next_version(None), time.strftime("%Y%m%d"))

    def test_next_version_from_past(self):
        self.assertEqual(self.s.next_version("20200101"), time.strftime("%Y%m%d"))

    def test_next_version_same_day_increments_suffix(self):
        today = time.strftime("%Y%m%d")
        self.assertEqual(self.s.next_version(today), f"{today}-2")
        self.assertEqual(self.s.next_version(f"{today}-3"), f"{today}-4")

    def test_next_version_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.s.next_version("1.0.29")


class TestPhases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_static_deployer(self.tmp)
        self.s = self.d.strategy

    def test_check_commands(self):
        server = FakeRemote()
        self.s.check(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn("command -v unzip", joined)
        self.assertIn(f"test -d {SITE_ROOT}", joined)
        self.assertIn(f"mkdir -p {SITE_PARENT}/backup /tmp/platform-deploy", joined)

    def test_check_site_root_missing_raises(self):
        server = FakeRemote(rules={"test -d": RuntimeError("rc=1")})
        with self.assertRaises(RuntimeError):
            self.s.check(server)

    def test_transfer_uploads_zip(self):
        make_zip(self.d)
        server = FakeRemote(try_exec_ok={"-size": False})  # 远端无同尺寸文件
        self.assertTrue(self.s.transfer(server))
        self.assertEqual(len(server.uploaded), 1)
        local, remote, label = server.uploaded[0]
        self.assertTrue(local.endswith("达人端-20261012.zip"))
        self.assertEqual(remote, "/tmp/platform-deploy/达人端-20261012.zip")
        self.assertEqual(label, "达人端-20261012.zip")

    def test_transfer_idempotent_skips_same_size(self):
        make_zip(self.d)
        server = FakeRemote(try_exec_ok={"-size": True})  # 远端已有同尺寸包
        self.s.transfer(server)
        self.assertEqual(server.uploaded, [])

    def test_transfer_missing_local_zip_raises(self):
        server = FakeRemote()
        with self.assertRaises(RuntimeError) as ctx:
            self.s.transfer(server)
        self.assertIn("本地静态包不存在", str(ctx.exception))

    def test_switch_command_sequence(self):
        entry_dir = f"{SITE_PARENT}/.deploy_staging_creator-web/dist"
        server = FakeRemote(rules={"find": entry_dir + "\n"})
        self.s.switch(server)
        # 语义约定：换入开始前必须置位 container_modified（失败时编排层据此回滚）
        self.assertTrue(self.d.container_modified)
        joined = "\n".join(server.exec_calls)
        self.assertIn("unzip -q -o /tmp/platform-deploy/达人端-20261012.zip", joined)
        # 原子换入链：备份归档 → 快照旧目录 → 换入新目录 → 权限归一 → 版本标记
        self.assertIn(f"mv -f /tmp/platform-deploy/达人端-20261012.zip "
                      f"{SITE_PARENT}/backup/达人端-20261012.zip", joined)
        self.assertIn(f"mv {SITE_ROOT} {SITE_PARENT}/.site_prev_creator-web", joined)
        self.assertIn(f"mv {entry_dir} {SITE_ROOT}", joined)
        self.assertIn("chmod 0644", joined)
        self.assertIn("echo 20261012 > " + SITE_ROOT + "/.deploy_version", joined)

    def test_switch_missing_entry_aborts_before_site_change(self):
        # find 无结果（MISSING）→ 必须抛错且尚未执行任何 mv（站点未被改动）
        server = FakeRemote(rules={"dirname": "MISSING"})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.switch(server)
        self.assertIn("入口文件", str(ctx.exception))
        swap_calls = [c for c in server.exec_calls if f"mv {SITE_ROOT}" in c]
        self.assertEqual(swap_calls, [], "定位入口失败后不得改动站点目录")

    def test_switch_prefers_root_index_over_nested(self):
        # 真实缺陷回归：dist 同时含根 index.html 与嵌套子应用 im_web/index.html 时，
        # 旧实现 find -maxdepth 2 -print -quit 在 readdir 顺序下会误选深层目录，
        # 把子应用当成站点根换入。修复后必须先测暂存区根，命中即用。
        st = f"{SITE_PARENT}/.deploy_staging_creator-web"
        server = FakeRemote(rules={"if [ -f": st + "\n"})
        self.s.switch(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"mv {st} {SITE_ROOT}", joined)
        self.assertNotIn(f"mv {st}/im_web {SITE_ROOT}", joined)
        # find 仅兜底恰好一层包裹目录（-mindepth 2），且根判定必须排在 find 之前
        locate = next(c for c in server.exec_calls if "-name index.html" in c)
        self.assertIn("-mindepth 2 -maxdepth 2", locate)
        self.assertLess(locate.index("if [ -f"), locate.index("find "))

    def test_verify_falls_back_to_marker(self):
        server = FakeRemote()
        self.s.verify(server)  # 未配 health_url：远端标记校验，命令必须成功返回
        self.assertIn("grep -qx '20261012'", server.exec_calls[0])

    def test_verify_http_success(self):
        self.d.proj_cfg["health_url"] = "https://example.com/"
        with mock.patch("health_check.check_http", return_value=True) as ch:
            self.s.verify(FakeRemote())
        # 静态发布必须走 body 无关模式，否则 index.html（200 无 UP 体）会被误报失败
        self.assertIn("require_health_body", ch.call_args.kwargs)
        self.assertFalse(ch.call_args.kwargs["require_health_body"])

    def test_verify_http_static_index_html_passes(self):
        """端到端回归：真实 200 静态首页（body 无 UP/ok）不得误报为超时失败"""
        self.d.proj_cfg["health_url"] = "https://dist.test.hongqiu8.com"
        html = "<!doctype html><html><head><title>安米聊后端管理系统</title></head><body></body></html>"
        resp = mock.Mock(status_code=200, text=html)
        with mock.patch("health_check.requests.get", return_value=resp):
            self.s.verify(FakeRemote())  # 不抛 RuntimeError 即通过

    def test_verify_http_timeout_raises(self):
        self.d.proj_cfg["health_url"] = "https://example.com/"
        with mock.patch("health_check.check_http", return_value=False), \
                mock.patch("deploy_strategies.static_archive.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self.s.verify(FakeRemote())
        self.assertIn("超时", str(ctx.exception))


class TestRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_static_deployer(self.tmp, version="20261012",
                                      previous_version="20261011")
        self.s = self.d.strategy

    def test_rollback_fast_path_restores_prev_dir(self):
        server = FakeRemote(rules={"if [ -d": "yes\n"})
        self.s.rollback(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"mv {SITE_ROOT} ", joined)
        self.assertIn(f"mv {SITE_PARENT}/.site_prev_creator-web {SITE_ROOT}", joined)
        # 快速路径不应重新解压
        self.assertNotIn("unzip", joined)

    def test_rollback_slow_path_from_backup_zip(self):
        entry_dir = f"{SITE_PARENT}/.deploy_staging_creator-web/dist"
        server = FakeRemote(rules={
            "if [ -d": "no\n",
            "find": entry_dir + "\n",
        })
        self.s.rollback(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"unzip -q -o {SITE_PARENT}/backup/达人端-20261011.zip", joined)
        self.assertIn(f"mv {entry_dir} {SITE_ROOT}", joined)
        self.assertIn("echo 20261011", joined)

    def test_rollback_no_source_skips(self):
        server = FakeRemote(rules={"if [ -d": "no\n"}, try_exec_ok={"test -f": False})
        with mock.patch("deploy_strategies.static_archive.log"):
            self.s.rollback(server)  # 无快照且无备份包：告警跳过，不抛新错误
        self.assertNotIn("unzip", "\n".join(server.exec_calls))

    def test_rollback_without_previous_version_skips(self):
        self.d.previous_version = None
        server = FakeRemote()
        self.s.rollback(server)
        self.assertEqual(server.exec_calls, [])


class TestQueryCurrentVersion(unittest.TestCase):
    def _query(self, rules, try_exec_ok=None):
        fake = FakeRemote(rules=rules, try_exec_ok=try_exec_ok)
        with mock.patch("deploy_strategies.static_archive.default_servers_file",
                        return_value="servers.test.yaml"), \
                mock.patch("deploy_strategies.static_archive.load_yaml",
                           return_value={"servers": [{"ip": "10.0.0.1"}]}), \
                mock.patch("deploy_strategies.static_archive.get_ssh_config",
                           return_value={}), \
                mock.patch("deploy_strategies.static_archive.merge_server_ssh",
                           return_value={}), \
                mock.patch.object(deploy_mod, "RemoteServer", return_value=fake):
            d = make_static_deployer(tempfile.mkdtemp())
            return d.strategy.query_current_version(), fake

    def test_reads_marker_file(self):
        ver, _ = self._query({"cat": "20261001\n"})
        self.assertEqual(ver, "20261001")

    def test_falls_back_to_backup_ls(self):
        # cat 无标记（返回空）→ 回退解析 backup 最新包名
        ver, _ = self._query({"ls -1t": "达人端-20260930.zip\n"})
        self.assertEqual(ver, "20260930")

    def test_none_when_nothing_found(self):
        ver, _ = self._query({}, try_exec_ok={})
        self.assertIsNone(ver)


class TestDeployServerEndToEnd(unittest.TestCase):
    """静态模式走 Deployer.deploy_server 全流程：成功路径 + 验证失败自动回滚"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_static_deployer(self.tmp, previous_version="20261011")
        make_zip(self.d)

    def _run(self, server, verify_error=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("deploy.RemoteServer", return_value=server))
            stack.enter_context(mock.patch("deploy.log"))
            if verify_error:
                stack.enter_context(mock.patch.object(
                    self.d.strategy, "verify", side_effect=verify_error))
            return self.d.deploy_server("10.0.0.1", None)

    def test_success_and_nacos_never_called(self):
        entry_dir = f"{SITE_PARENT}/.deploy_staging_creator-web/dist"
        server = FakeRemote(rules={"find": entry_dir + "\n"})
        ok = self._run(server)
        self.assertTrue(ok)
        self.d.nacos.offline_ip.assert_not_called()
        self.d.nacos.online_ip.assert_not_called()
        joined = "\n".join(server.exec_calls)
        self.assertIn("unzip", joined)
        self.assertIn(f"mv {entry_dir} {SITE_ROOT}", joined)
        self.assertTrue(server.closed)

    def test_verify_failure_triggers_rollback(self):
        entry_dir = f"{SITE_PARENT}/.deploy_staging_creator-web/dist"
        server = FakeRemote(rules={
            "find": entry_dir + "\n",
            "if [ -d": "yes\n",   # 回滚快速路径：快照存在
        })
        ok = self._run(server, verify_error=RuntimeError("健康检查超时"))
        self.assertFalse(ok)
        joined = "\n".join(server.exec_calls)
        # 换入失败后必须执行整目录还原
        self.assertIn(f"mv {SITE_PARENT}/.site_prev_creator-web {SITE_ROOT}", joined)
        # Nacos 恢复上线同样跳过
        self.d.nacos.online_ip.assert_not_called()

    def test_transfer_failure_leaves_site_untouched(self):
        # 本地包缺失：transfer 抛错 → container_modified 未置位 → 不做任何回滚动作
        self.d.dist_dir = Path(self.tmp) / "nowhere"
        server = FakeRemote()
        ok = self._run(server)
        self.assertFalse(ok)
        self.assertFalse(self.d.container_modified)
        self.assertEqual([c for c in server.exec_calls if "mv " + SITE_ROOT in c], [])


if __name__ == "__main__":
    unittest.main()
