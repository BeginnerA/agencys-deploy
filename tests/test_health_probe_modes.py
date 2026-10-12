"""webapp.monitor 健康检查分层退化探针：strict/loose HTTP、TCP 兜底、自适应编排

验证现实项目「极少遗留健康检查接口」场景下的替代探活方式：
- 无专用 health_url 时，宽松 HTTP（任意响应码即存活）不得因 404/401 误判为不健康；
- HTTP 不可达时 TCP 端口连通兜底；
- 三者结果都带 method 字段供前端区分展示。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from webapp import monitor


class TestProbeHealthUrlModes(unittest.TestCase):
    def test_strict_requires_200(self):
        resp = mock.Mock(status_code=404)
        with mock.patch.object(monitor, "_http_session") as sess:
            sess.return_value.get.return_value = resp
            r = monitor._probe_health_url("10.0.0.1", 9210, "/", 5, strict=True)
        self.assertFalse(r["ok"])
        self.assertEqual(r["method"], "http_strict")

    def test_loose_accepts_any_response(self):
        """404/401/5xx 只要有 HTTP 响应即视为应用层存活（回归用户痛点）"""
        for code in (404, 401, 403, 500):
            resp = mock.Mock(status_code=code)
            with mock.patch.object(monitor, "_http_session") as sess:
                sess.return_value.get.return_value = resp
                r = monitor._probe_health_url("10.0.0.1", 9210, "/", 5, strict=False)
            self.assertTrue(r["ok"], f"code={code} 应判存活")
            self.assertEqual(r["method"], "http_loose")

    def test_strict_200_ok(self):
        resp = mock.Mock(status_code=200)
        with mock.patch.object(monitor, "_http_session") as sess:
            sess.return_value.get.return_value = resp
            r = monitor._probe_health_url("10.0.0.1", 9210, "/actuator/health", 5, True)
        self.assertTrue(r["ok"])


class TestProbeTcpPort(unittest.TestCase):
    def test_open(self):
        with mock.patch("socket.create_connection") as cc:
            cc.return_value = mock.Mock()
            r = monitor._probe_tcp_port("10.0.0.1", 9210)
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], "tcp")

    def test_refused(self):
        with mock.patch("socket.create_connection",
                        side_effect=ConnectionRefusedError("x")):
            r = monitor._probe_tcp_port("10.0.0.1", 9210)
        self.assertFalse(r["ok"])
        self.assertEqual(r["ms"], -1)


class TestProbeHealthAdaptive(unittest.TestCase):
    def test_http_loose_success_short_circuits(self):
        """宽松 HTTP 成功 → 直接返回，不再跑 TCP"""
        with mock.patch.object(monitor, "_probe_health_url",
                               return_value={"ok": True, "ms": 12, "method": "http_loose"}), \
             mock.patch.object(monitor, "_probe_tcp_port") as tcp:
            r = monitor._probe_health_adaptive("10.0.0.1", 9210)
        self.assertEqual(r["method"], "http_loose")
        tcp.assert_not_called()

    def test_falls_back_to_tcp(self):
        """HTTP 失败 → TCP 兜底成功"""
        with mock.patch.object(monitor, "_probe_health_url",
                               return_value={"ok": False, "ms": -1, "method": "http_loose"}), \
             mock.patch.object(monitor, "_probe_tcp_port",
                               return_value={"ok": True, "ms": 3, "method": "tcp"}):
            r = monitor._probe_health_adaptive("10.0.0.1", 9210)
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], "tcp")


if __name__ == "__main__":
    unittest.main()
