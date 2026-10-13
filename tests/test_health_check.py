"""scripts/health_check 容器状态检查测试：docker --format 模板转义与运行状态判定"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import health_check as hc


class TestContainerRunningCheck(unittest.TestCase):
    def _run_check(self, stdout_bytes: bytes):
        """执行 check_container_running，返回 (发送给 docker 的命令, 判定结果)"""
        stdout = mock.Mock()
        stdout.read.return_value = stdout_bytes
        client = mock.Mock()
        client.exec_command.return_value = (mock.Mock(), stdout, mock.Mock())
        with mock.patch.object(hc, "get_ssh_client", return_value=client):
            result = hc.check_container_running("10.0.0.1", "hqb-expert", {})
        return client.exec_command.call_args[0][0], result

    def test_docker_format_template_keeps_go_braces(self):
        """str.format 后必须保留 Go 模板双大括号 {{.Status}}，不得退化为 {.Status}"""
        cmd, _ = self._run_check(b"")
        self.assertIn("{{.Status}}", cmd,
                      "docker --format 必须是 Go 模板 {{.Status}}，"
                      "{{.Status}} 经 str.format 会退化为字面量 {.Status} 导致永远判为未运行")

    def test_status_up_returns_true(self):
        _, result = self._run_check(b"Up 2 hours\n")
        self.assertTrue(result)

    def test_status_literal_placeholder_returns_false(self):
        """回归保护：docker 原样输出模板占位符（模板无效时的典型表现）不得判为运行"""
        _, result = self._run_check(b"{.Status}\n")
        self.assertFalse(result)

    def test_empty_output_returns_false(self):
        _, result = self._run_check(b"")
        self.assertFalse(result)


class TestTcpPortCheck(unittest.TestCase):
    """check_tcp_port：未配健康接口时的端口连通退化检查"""

    def test_open_port_returns_true(self):
        with mock.patch("socket.create_connection") as cc:
            cc.return_value = mock.Mock()
            self.assertTrue(hc.check_tcp_port("10.0.0.1", 9210))
            cc.return_value.close.assert_called_once()

    def test_refused_returns_false(self):
        import socket as _s
        with mock.patch("socket.create_connection",
                        side_effect=ConnectionRefusedError("refused")):
            self.assertFalse(hc.check_tcp_port("10.0.0.1", 9210))

    def test_timeout_returns_false(self):
        import socket as _s
        with mock.patch("socket.create_connection",
                        side_effect=_s.timeout("t")):
            self.assertFalse(hc.check_tcp_port("10.0.0.1", 9210))


class TestTcpPortViaSshCheck(unittest.TestCase):
    """check_tcp_port_via_ssh：微服务端口不对公网放行时，在服务器内部探回环端口。

    回归护栏：平台机直连公网业务端口被云安全组丢包（timed out）导致的假超时误回滚。
    """

    def _run(self, stdout_bytes: bytes, ssh_raises=None):
        stdout = mock.Mock()
        stdout.read.return_value = stdout_bytes
        stderr = mock.Mock()
        stderr.read.return_value = b""
        client = mock.Mock()
        client.exec_command.return_value = (mock.Mock(), stdout, stderr)
        with mock.patch.object(hc, "get_ssh_client", return_value=client) as gsc:
            if ssh_raises is not None:
                gsc.side_effect = ssh_raises
            result = hc.check_tcp_port_via_ssh("47.95.172.17", 8888, {})
        cmd = client.exec_command.call_args[0][0] if client.exec_command.call_count else ""
        return cmd, result

    def test_up_on_loopback_returns_true(self):
        cmd, result = self._run(b"__UP__\n")
        self.assertTrue(result)
        # 必须探测 127.0.0.1 回环，而非公网 IP（公网直连正是假超时根因）
        self.assertIn("/dev/tcp/127.0.0.1/8888", cmd)
        self.assertNotIn("47.95.172.17", cmd)

    def test_down_returns_false(self):
        _, result = self._run(b"__DOWN__\n")
        self.assertFalse(result)

    def test_no_marker_returns_none_for_fallback(self):
        """无 bash/无 /dev/tcp：命令未产出判定标记 → None（机制不可用，交调用方回退）"""
        _, result = self._run(b"bash: /dev/tcp: No such file\n")
        self.assertIsNone(result)

    def test_ssh_unavailable_returns_none(self):
        """SSH 通道建立失败 → None，交由容器运行状态检查兜底"""
        _, result = self._run(b"", ssh_raises=RuntimeError("ssh down"))
        self.assertIsNone(result)


class TestWaitForHealthyRoutesViaSsh(unittest.TestCase):
    """wait_for_healthy 的 health_port 分支必须走 SSH 内部探测，不得再从平台机直连公网端口"""

    def test_health_port_uses_ssh_probe_not_socket(self):
        cfg = {"deploy": {"health_timeout": 30, "health_interval": 5}}
        proj = {"service_name": "im-platform", "health_url": "", "health_port": 8888,
                "log_error_keywords": []}
        with mock.patch.object(hc, "load_config", return_value=cfg), \
                mock.patch.object(hc, "get_project_config", return_value=proj), \
                mock.patch.object(hc, "get_ssh_config", return_value={}), \
                mock.patch.object(hc, "check_tcp_port_via_ssh", return_value=True) as via_ssh, \
                mock.patch.object(hc, "check_tcp_port") as sock_probe, \
                mock.patch.object(hc, "check_container_logs", return_value=True), \
                mock.patch.object(hc, "log"):
            ok = hc.wait_for_healthy("47.95.172.17", "im-platform")
        self.assertTrue(ok)
        via_ssh.assert_called_once_with("47.95.172.17", 8888, {})
        sock_probe.assert_not_called()  # 关键：绝不再走公网直连 socket 探测

    def test_ssh_probe_mechanism_unavailable_falls_back_to_container_running(self):
        cfg = {"deploy": {"health_timeout": 30, "health_interval": 5}}
        proj = {"service_name": "im-platform", "health_url": "", "health_port": 8888,
                "log_error_keywords": []}
        with mock.patch.object(hc, "load_config", return_value=cfg), \
                mock.patch.object(hc, "get_project_config", return_value=proj), \
                mock.patch.object(hc, "get_ssh_config", return_value={}), \
                mock.patch.object(hc, "check_tcp_port_via_ssh", return_value=None), \
                mock.patch.object(hc, "check_container_running", return_value=True) as running, \
                mock.patch.object(hc, "check_container_logs", return_value=True), \
                mock.patch.object(hc, "log"):
            ok = hc.wait_for_healthy("47.95.172.17", "im-platform")
        self.assertTrue(ok)
        running.assert_called_once_with("47.95.172.17", "im-platform", {})

    def test_loopback_down_but_container_up_is_healthy(self):
        """回环探 DOWN（端口只在 docker 内部网络未发布到宿主机）但容器 Up → 健康，不得误回滚"""
        cfg = {"deploy": {"health_timeout": 30, "health_interval": 5}}
        proj = {"service_name": "im-platform", "health_url": "", "health_port": 8888,
                "log_error_keywords": []}
        with mock.patch.object(hc, "load_config", return_value=cfg), \
                mock.patch.object(hc, "get_project_config", return_value=proj), \
                mock.patch.object(hc, "get_ssh_config", return_value={}), \
                mock.patch.object(hc, "check_tcp_port_via_ssh", return_value=False), \
                mock.patch.object(hc, "check_container_running", return_value=True), \
                mock.patch.object(hc, "check_container_logs", return_value=True), \
                mock.patch.object(hc, "log"):
            ok = hc.wait_for_healthy("47.95.172.17", "im-platform")
        self.assertTrue(ok)

    def test_loopback_down_and_container_not_up_times_out(self):
        """回环 DOWN 且容器未 Up（真异常）→ 保持不健康直至超时抛错，不得盲判成功"""
        cfg = {"deploy": {"health_timeout": 10, "health_interval": 5}}
        proj = {"service_name": "im-platform", "health_url": "", "health_port": 8888,
                "log_error_keywords": []}
        with mock.patch.object(hc, "load_config", return_value=cfg), \
                mock.patch.object(hc, "get_project_config", return_value=proj), \
                mock.patch.object(hc, "get_ssh_config", return_value={}), \
                mock.patch.object(hc, "check_tcp_port_via_ssh", return_value=False), \
                mock.patch.object(hc, "check_container_running", return_value=False), \
                mock.patch.object(hc, "log"), \
                mock.patch.object(hc.time, "sleep"), \
                mock.patch.object(hc.time, "time", side_effect=[0, 0, 11]):
            with self.assertRaises(RuntimeError):
                hc.wait_for_healthy("47.95.172.17", "im-platform")
