"""实时推送通道：GET /api/stream（SSE / text/event-stream）

选型：浏览器侧只需要「服务端单向通知 → 前端重取既有 REST 数据」，SSE 即够，
不引入 WebSocket 依赖（flask-sock / socketio 需异步 worker，会改变整体运行模型）；
前端沿用平台既有 SSE 消费范式（static/js/agent-stream.js 的 fetch + ReadableStream
逐行解析），而非 EventSource——后者无法携带 Authorization 头，与本平台 JWT 认证不兼容。

帧协议（与 static/js/live.js 对齐）：
  data: {"type":"hello","topics":[...],"user":..., "server_ts":...}   订阅成功
  data: {"type":"event","seq":N,"topic":"alerts","data":{...},"ts":...} 业务事件
  data: {"type":"error","error":...}                                  订阅已满（紧接收尾）
  data: {"type":"bye","reason":"max_lifetime"}                        服务端主动收尾
  : ping                                                               空闲保活注释帧

订阅登记放在生成器体内（而非视图返回前）：视图返回后客户端若一帧未读就断开，
生成器从未启动、finally 不会执行，在视图里订阅会造成订阅者泄漏。
"""
import json
import time

from flask import Blueprint, Response, request, g

from .. import events
from ..auth import require_auth

stream_bp = Blueprint("stream", __name__)

# 已知主题（与生产者埋点一一对应）：未知主题直接忽略，避免前端拼错参数收不到东西
# logs = 采集器本轮有新日志行（服务日志抽屉取尾部）；任务日志文件由发布子进程直接
# 写入（跨进程不可埋点），仍靠页面轮询追尾，不在本主题覆盖范围内
KNOWN_TOPICS = ("monitor", "alerts", "tasks", "agent", "logs")
HEARTBEAT_SECONDS = 15.0      # 空闲发注释帧保活，防代理判定连接死掉
MAX_LIFETIME_SECONDS = 1800   # 单流最长 30 分钟，到期收尾由前端自动重连
# 已断开的客户端要等到下一次写帧（事件或心跳）才因 BrokenPipe 退出生成器并退订，
# 因此空闲死连接的回收窗口上界 = HEARTBEAT_SECONDS（真服务器实测一致）；
# 叠加 MAX_SUBSCRIBERS 上限与 MAX_LIFETIME 兜底，不会无限累积


def _frame(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False,
                                 separators=(",", ":")) + "\n\n"


@stream_bp.route("/stream")
@require_auth
def stream():
    topics = [t for t in (request.args.get("topics", "") or "").split(",")
              if t in KNOWN_TOPICS] or list(KNOWN_TOPICS)
    # 生成器在请求上下文之外迭代，身份与参数必须此刻取妥
    username = g.user["username"]

    def gen():
        sub = events.subscribe(topics)
        if sub is None:
            # 超出订阅上限：发错帧后立即收尾，前端转降级轮询（变慢但不丢更新）
            yield _frame({"type": "error", "error": "订阅通道已满"})
            return
        deadline = time.time() + MAX_LIFETIME_SECONDS
        try:
            yield _frame({"type": "hello", "topics": topics,
                          "user": username,
                          "server_ts": round(time.time(), 3)})
            while time.time() < deadline:
                ev = events.next_event(sub, timeout=HEARTBEAT_SECONDS)
                if ev is None:
                    yield ": ping\n\n"        # 注释帧，前端解析器忽略
                    continue
                ev["type"] = "event"
                yield _frame(ev)
            yield _frame({"type": "bye", "reason": "max_lifetime"})
        finally:
            # 客户端断开（GeneratorExit）与到期收尾共用同一条退出路径
            events.unsubscribe(sub)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # 反代关闭缓冲，否则事件被攒住不发
    return resp
