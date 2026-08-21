"""SSL 证书探测函数测试（mock ssl 模块，不依赖真实网络）"""
import datetime
import unittest
from unittest.mock import patch, MagicMock

from webapp.ssl_probe import probe_ssl


class TestProbeSsl(unittest.TestCase):
    """probe_ssl 单元测试：mock ssl.wrap_socket 避免真实网络连接"""

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_successful_probe(self, mock_sock_cls, mock_ctx_fn):
        """成功探测：返回证书信息和剩余天数"""
        # 模拟证书 notAfter: "Sep 15 12:00:00 2026 GMT"
        future = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(days=60)
        not_after = future.strftime("%b %d %H:%M:%S %Y GMT")
        # 构造 mock 证书
        mock_cert = {
            "subject": ((("commonName", "api.example.com"),),),
            "issuer": ((("commonName", "Let's Encrypt"),),),
            "notAfter": not_after,
        }
        mock_sock = MagicMock()
        mock_sock.getpeercert.return_value = mock_cert
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("api.example.com", 443, timeout=10)
        self.assertIsNone(result["error"])
        self.assertEqual(result["cert_subject"], "CN=api.example.com")
        self.assertEqual(result["cert_issuer"], "CN=Let's Encrypt")
        self.assertIsNotNone(result["cert_expiry"])
        self.assertAlmostEqual(result["days_left"], 60, delta=1)

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_connection_timeout(self, mock_sock_cls, mock_ctx_fn):
        """连接超时：返回 error 字段"""
        mock_sock = MagicMock()
        mock_sock.settimeout = MagicMock()
        mock_sock.connect.side_effect = TimeoutError("timed out")
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("bad.example.com", 443, timeout=5)
        self.assertIsNotNone(result["error"])
        self.assertIn("timed out", result["error"].lower() + result["error"])
        self.assertIsNone(result["cert_subject"])

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_dns_failure(self, mock_sock_cls, mock_ctx_fn):
        """DNS 解析失败：返回 error 字段"""
        mock_sock = MagicMock()
        mock_sock.settimeout = MagicMock()
        mock_sock.connect.side_effect = OSError("Name resolution failed")
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("nonexistent.invalid", 443, timeout=5)
        self.assertIsNotNone(result["error"])
        self.assertIsNone(result["cert_subject"])
