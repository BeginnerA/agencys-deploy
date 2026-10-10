"""进程内实时事件总线：生产者（监控/告警/任务/建议）→ SSE 订阅者

设计约束（实时推送是旁路能力，任何情况下都不得影响主业务）：
- publish 吞掉自身全部异常并返回 False，业务线程永不被推送阻塞或拖崩；
- 每个订阅者一条有界队列（QUEUE_MAXSIZE），队列满时丢弃事件并置 gap 标记，
  下一次取帧补发 resync 事件，由订阅端主动全量重取（丢弃不会造成最终不一致）；
- 全局单调 seq 便于订阅端自检是否漏帧；
- topic 过滤在订阅时确定（topics=None 表示收全部），推送侧 O(订阅数) 分发。

作用域限制：本总线是**进程内**的，生产者与订阅者须同进程。当前部署形态为
单进程多线程（webapp/app.py 直接 app.run(threaded=True)，Dockerfile 同），
监控/告警/任务线程与请求线程同进程，故事件可见。若未来改为多 worker
（gunicorn -w N），跨进程事件收不到——前端所有接入点都保留了兜底轮询，
降级只是变慢而非丢更新。
"""
import itertools
import queue
import threading
import time

# 连接上限：每条 SSE 流占用一个请求线程，超限后新订阅返回 None
# （前端转降级轮询），避免刷屏式开页耗尽线程
MAX_SUBSCRIBERS = 64
# 单订阅者待投递队列上限：慢客户端只丢事件（补 resync），不无限堆内存
QUEUE_MAXSIZE = 100

_lock = threading.Lock()
_subs = {}                                  # sub_id -> subscription dict
_sub_ids = itertools.count(1)
_seq = itertools.count(1)
_counters = {"published": 0, "dropped": 0}


def subscribe(topics=None):
    """登记订阅者；topics 为可迭代主题名（空/None=全部）。超限返回 None"""
    topic_set = None
    if topics:
        topic_set = {t for t in topics if t}
        if not topic_set:
            topic_set = None
    with _lock:
        if len(_subs) >= MAX_SUBSCRIBERS:
            return None
        sub = {"id": next(_sub_ids),
               "q": queue.Queue(maxsize=QUEUE_MAXSIZE),
               "topics": topic_set,
               "gap": False}
        _subs[sub["id"]] = sub
        return sub


def unsubscribe(sub):
    """注销订阅者（幂等）"""
    if not sub:
        return
    with _lock:
        _subs.pop(sub.get("id"), None)


def publish(topic, data=None):
    """业务侧唯一发布入口：内部异常全吞，返回是否成功分发

    data 必须是可 json 序列化的轻量字典（事件只作「有新东西」的信号，
    真实数据由订阅者回调既有 REST 接口取，避免总线承载业务数据契约）。
    """
    try:
        event = {"seq": next(_seq), "topic": topic,
                 "data": data if data is not None else {},
                 "ts": round(time.time(), 3)}
        with _lock:
            _counters["published"] += 1
            targets = [s for s in _subs.values()
                       if s["topics"] is None or topic in s["topics"]]
        for s in targets:
            try:
                s["q"].put_nowait(event)
            except queue.Full:
                # 慢消费者：丢事件并标记缺口，稍后补发 resync 让前端全量重取
                s["gap"] = True
                _counters["dropped"] += 1
        return True
    except Exception:
        return False


def next_event(sub, timeout=15.0):
    """取下一帧：最多阻塞 timeout 秒；空闲返回 None（调用方发力保活心跳）

    队列曾发生溢出且当前无待发事件时，补一帧 resync 信号（不占用真实 topic），
    提示订阅端做一次全量刷新。
    """
    try:
        ev = sub["q"].get(timeout=timeout)
    except queue.Empty:
        ev = None
    if ev is None and sub["gap"]:
        sub["gap"] = False
        ev = {"seq": next(_seq), "topic": "resync",
              "data": {"reason": "overflow"},
              "ts": round(time.time(), 3)}
    return ev


def stats():
    """可观测：当前订阅者数与累计发布/丢弃计数"""
    with _lock:
        return {"subscribers": len(_subs),
                "published": _counters["published"],
                "dropped": _counters["dropped"]}


def reset():
    """测试隔离用：清空订阅者与计数器（seq 不回卷，保持单调语义）"""
    with _lock:
        _subs.clear()
        _counters["published"] = 0
        _counters["dropped"] = 0
