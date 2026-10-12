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


class TestProbeHealthViaSsh(unittest.TestCase):
    """SSH 本地探测：微服务端口不对公网放行，改为在服务器 localhost 探测。
    验证命令生成（针对 127.0.0.1、分层、并行）与输出解析。"""

    def _run(self, out):
        captured = {}

        def fake_ssh_exec(host, port, ssh_cfg, cmd, timeout=10):
            captured["cmd"] = cmd
            return out

        with mock.patch.object(monitor, "_ssh_exec", side_effect=fake_ssh_exec):
            res = monitor._probe_health_via_ssh(
                "10.0.0.1", 22, {}, [("svcA", 9210, ""), ("svcB", 9207, "/x/health")])
        return dict(res), captured["cmd"]

    def test_parses_all_layers(self):
        out = ("svcA|tcp|2|1\n"
               "svcB|http_strict|8|200\n")
        res, cmd = self._run(out)
        self.assertTrue(res["svcA"]["ok"])
        self.assertEqual(res["svcA"]["method"], "tcp")
        self.assertEqual(res["svcA"]["ms"], 2)
        self.assertTrue(res["svcB"]["ok"])
        self.assertEqual(res["svcB"]["method"], "http_strict")

    def test_strict_non200_is_not_ok(self):
        out = "svcB|http_strict|5|404\n"
        res, _ = self._run(out)
        self.assertFalse(res["svcB"]["ok"])
        self.assertEqual(res["svcB"]["ms"], -1)

    def test_loose_any_code_alive(self):
        # 无 health_url 服务 curl 拿到 401 也算存活
        out = "svcA|http_loose|3|401\n"
        res, _ = self._run(out)
        self.assertTrue(res["svcA"]["ok"])
        self.assertEqual(res["svcA"]["method"], "http_loose")

    def test_down_not_ok(self):
        out = "svcA|down|-1|0\n"
        res, _ = self._run(out)
        self.assertFalse(res["svcA"]["ok"])

    def test_zero_ms_bumped_to_one_for_display(self):
        out = "svcA|tcp|0|1\n"
        res, _ = self._run(out)
        self.assertEqual(res["svcA"]["ms"], 1)

    def test_command_targets_localhost_and_parallel(self):
        _, cmd = self._run("")
        self.assertIn("127.0.0.1:9210", cmd)
        self.assertIn("127.0.0.1:9207/x/health", cmd)
        self.assertIn("/dev/tcp/127.0.0.1/9210", cmd)
        # 两个检查各自后台并行，末尾统一 wait
        self.assertEqual(cmd.count(" ) &"), 2)
        self.assertTrue(cmd.rstrip().endswith("wait"))

    def test_ssh_failure_returns_empty(self):
        with mock.patch.object(monitor, "_ssh_exec",
                               side_effect=OSError("ssh down")):
            res = monitor._probe_health_via_ssh(
                "10.0.0.1", 22, {}, [("svcA", 9210, "")])
        self.assertEqual(res, [])


if __name__ == "__main__":
    unittest.main()
