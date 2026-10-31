import unittest
from deploy_strategies import tomcat_health as th

class FakeChannel:
    def __init__(self, payload, fail=None):
        self.payload = payload
        self.fail = fail
        self.sent = b""
    def settimeout(self, value):
        self.timeout = value
    def sendall(self, data):
        self.sent += data
    def makefile(self, mode):
        import io
        if self.fail:
            raise OSError(self.fail)
        return io.BytesIO(self.payload)
    def close(self):
        self.closed = True

class FakeTransport:
    def __init__(self, channel):
        self.channel = channel
        self.args = None
    def open_channel(self, kind, dest, src, timeout=None):
        self.args = (kind, dest, src, timeout)
        return self.channel

class FakeClient:
    def __init__(self, channel):
        self.transport = FakeTransport(channel)
    def get_transport(self):
        return self.transport

class FakeServer:
    # 生产接缝同形：RemoteServer.client() / _PooledClient.client() 都是方法而非属性
    def __init__(self, channel):
        self._client = FakeClient(channel)
    def client(self):
        return self._client

class HealthTest(unittest.TestCase):
    def test_200_and_body_ok(self):
        channel = FakeChannel(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nUP")
        server = FakeServer(channel)
        out = th.probe_http(server, 18080, "/orders/health", body_contains="UP")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], 200)
        self.assertEqual(channel.sent,
                         b"GET /orders/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Connection: close\r\n\r\n")
        self.assertEqual(server.client().transport.args[0], "direct-tcpip")

    def test_redirect_and_wrong_body_fail_without_following(self):
        for payload, body in ((b"HTTP/1.1 302 Found\r\nLocation: /x\r\n\r\n", ""),
                              (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nNO", "UP")):
            out = th.probe_http(FakeServer(FakeChannel(payload)), 1, "/h",
                                body_contains=body)
            self.assertFalse(out["ok"])

    def test_forwarding_denied(self):
        server = FakeServer(FakeChannel(b"", fail="Administratively prohibited"))
        out = th.probe_http(server, 1, "/h")
        self.assertFalse(out["ok"])
        self.assertIn("forward", out["error"])

    def test_body_limit(self):
        payload = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (th.MAX_BODY + 1)
        out = th.probe_http(FakeServer(FakeChannel(payload)), 1, "/h")
        self.assertFalse(out["ok"])
        self.assertIn("响应", out["error"])

    def test_poll_stops_at_deadline(self):
        calls = []
        original = th.probe_http
        th.probe_http = lambda *a, **k: calls.append(1) or {"ok": False, "ms": 1, "status": None, "error": "x"}
        try:
            out = th.poll_http(FakeServer(FakeChannel(b"")), 1, "/h", timeout=0, interval=1)
        finally:
            th.probe_http = original
        self.assertFalse(out["ok"])
        self.assertEqual(len(calls), 1)
