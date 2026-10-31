"""SSH direct-tcpip HTTP 探活：不要求目标机安装 curl，不跟随重定向。

经既有 SSH 连接的 transport 打开 direct-tcpip 通道直连目标机 127.0.0.1:{port}，
平台无需直连业务端口；单次结果恒为
{"ok": bool, "ms": int, "status": int|None, "error": str}，异常折算进 error 不外抛。
"""
import time

MAX_BODY = 1024 * 1024


def _friendly_error(e: Exception) -> str:
    """异常文本折算为可读错误；转发被禁/不可用必须与普通连接失败可区分。"""
    text = str(e)
    lowered = text.lower()
    if "prohibited" in lowered or "forward" in lowered:
        return "SSH 端口转发(forward)被禁止或不可用"
    return text


def _read_all(fileobj, limit: int) -> bytes:
    data = b""
    while len(data) <= limit:
        chunk = fileobj.read(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data += chunk
    return data


def probe_http(server, port: int, path: str, expected_status: int = 200,
               body_contains: str = "", timeout: float = 5.0) -> dict:
    """单次有界探活：server 只需提供 client() -> SSHClient 形状的接缝（RemoteServer 同形）。

    302 重定向、状态码或正文不匹配、响应超 1 MiB 均算失败，绝不跟随重定向。
    """
    start = time.time()
    channel = None
    try:
        transport = server.client().get_transport()
        channel = transport.open_channel(
            "direct-tcpip", ("127.0.0.1", int(port)), ("127.0.0.1", 0),
            timeout=min(float(timeout), 5.0))
        channel.settimeout(min(float(timeout), 5.0))
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Connection: close\r\n\r\n").encode("ascii")
        channel.sendall(request)
        f = channel.makefile("rb")
        raw = _read_all(f, MAX_BODY)
        if len(raw) > MAX_BODY:
            return {"ok": False, "ms": int((time.time() - start) * 1000),
                    "status": None, "error": "响应超过 1 MiB 上限"}
        head, _, body = raw.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first.split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            return {"ok": False, "ms": int((time.time() - start) * 1000),
                    "status": None, "error": "无效 HTTP 响应"}
        status = int(parts[1])
        ok = status == expected_status and (not body_contains
                                            or body_contains in body.decode("utf-8", "ignore"))
        return {"ok": ok, "ms": int((time.time() - start) * 1000),
                "status": status, "error": "" if ok else "状态或响应正文不匹配"}
    except Exception as e:  # 探活不抛异常：失败折算为结果字典，交由轮询/预检消费
        return {"ok": False, "ms": int((time.time() - start) * 1000),
                "status": None, "error": _friendly_error(e)}
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass


def poll_http(server, port: int, path: str, timeout: int, interval: int,
              expected_status: int = 200, body_contains: str = "") -> dict:
    """有界轮询 probe_http：deadline 到点即停，最后一次结果附加 attempts。

    仅测试辅助；生产探活走 tomcat_lifecycle._probe——其单次探活超时随剩余
    截止时间收缩，poll_http 的固定单次 timeout 不具备该约束。
    """
    deadline = time.monotonic() + max(0, timeout)
    attempts = 0
    last = {"ok": False, "ms": 0, "status": None, "error": "未执行"}
    while True:
        attempts += 1
        last = probe_http(server, port, path, expected_status=expected_status,
                          body_contains=body_contains)
        if last["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(max(0.1, min(interval, deadline - time.monotonic())))
    last["attempts"] = attempts
    return last
