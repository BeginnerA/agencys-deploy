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
