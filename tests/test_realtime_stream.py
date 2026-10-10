"""实时通道测试：事件总线语义 / SSE 路由帧协议 / 生产者埋点漏斗

覆盖三类风险（都是实时旁路最容易出的问题）：
1. 总线自身：主题过滤、seq 单调、慢消费者溢出补 resync、退订与连接上限；
2. 路由：鉴权、hello/event 帧格式、未知主题回落、客户端一帧未读也不泄漏订阅者；
3. 生产者：任务状态与告警状态漏斗只在 status 变化时推事件（不误推普通字段更新）。
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg

_TMP = tempfile.mkdtemp(prefix="realtime_test_")
webapp_pkg.DATA_DIR = Path(_TMP)

from webapp import create_app, events
from webapp import db as dbm
from webapp.routes.stream_routes import KNOWN_TOPICS


def _decode(chunk) -> str:
    return chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk


def _frames(chunk) -> list:
    """SSE 文本 → payload 列表（注释心跳帧被跳过，与浏览器端解析同口径）"""
    out = []
    for block in _decode(chunk).split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                out.append(json.loads(line[5:].strip()))
    return out


class TestEventsBus(unittest.TestCase):
    """总线语义：过滤 / 顺序 / 溢出降级 / 退订 / 上限"""

    def setUp(self):
        events.reset()

    tearDown = setUp

    def test_topic_filter_and_seq_monotonic(self):
        sub = events.subscribe(["alerts"])
        self.assertTrue(events.publish("tasks", {"a": 1}))
        self.assertIsNone(events.next_event(sub, timeout=0.05))  # 不相关主题不入队
        events.publish("alerts", {"b": 2})
        ev = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev["topic"], "alerts")
        self.assertEqual(ev["data"], {"b": 2})
        events.publish("alerts", {"c": 3})
        self.assertGreater(events.next_event(sub, timeout=0.5)["seq"], ev["seq"])
        events.unsubscribe(sub)

    def test_subscribe_none_receives_all_topics(self):
        sub = events.subscribe(None)
        events.publish("monitor", {})
        self.assertEqual(events.next_event(sub, timeout=0.5)["topic"], "monitor")
        events.publish("tasks", {})
        self.assertEqual(events.next_event(sub, timeout=0.5)["topic"], "tasks")
        events.unsubscribe(sub)

    def test_unsubscribe_stops_delivery(self):
        sub = events.subscribe(None)
        events.unsubscribe(sub)
        events.unsubscribe(sub)   # 幂等
        events.publish("monitor", {})
        self.assertIsNone(events.next_event(sub, timeout=0.05))
        self.assertEqual(events.stats()["subscribers"], 0)

    def test_overflow_drops_then_emits_resync(self):
        sub = events.subscribe(["tasks"])
        total = events.QUEUE_MAXSIZE + 20
        for i in range(total):
            events.publish("tasks", {"i": i})
        self.assertEqual(events.stats()["dropped"], 20)
        # 积压的真实事件优先投递，队列排空后才补一帧 resync 提示前端全量重取
        received = 0
        while events.next_event(sub, timeout=0.01) is not None:
            received += 1
        self.assertEqual(received, events.QUEUE_MAXSIZE + 1)
        self.assertEqual(events.stats()["dropped"], 20)

    def test_resync_only_once_after_gap_cleared(self):
        sub = events.subscribe(["tasks"])
        for i in range(events.QUEUE_MAXSIZE + 5):
            events.publish("tasks", {"i": i})
        while events.next_event(sub, timeout=0.01) is not None:
            pass
        # 上面循环已消费掉 resync（计入 received），此后再取应为空且不再重复补发
        self.assertIsNone(events.next_event(sub, timeout=0.05))

    def test_subscriber_cap_rejects_extra(self):
        subs = [events.subscribe(["tasks"]) for _ in range(events.MAX_SUBSCRIBERS)]
        self.assertTrue(all(s is not None for s in subs))
        self.assertIsNone(events.subscribe(["tasks"]))
        for s in subs:
            events.unsubscribe(s)
        self.assertEqual(events.stats()["subscribers"], 0)

    def test_publish_never_raises_out_of_bad_payload(self):
        # 总线内部故障不得外溢到业务线程：订阅表被破坏时只返回 False
        sub = events.subscribe(["tasks"])
        with mock.patch.dict(events._subs, {999999: {"q": None, "topics": None,
                                                     "gap": False}}):
            self.assertFalse(events.publish("tasks", {"a": 1}))
        # 异常不能把总线状态搞坏：重新订阅后后续发布仍正常投递
        events.unsubscribe(sub)
        sub2 = events.subscribe(["tasks"])
        self.assertTrue(events.publish("tasks", {"a": 2}))
        self.assertEqual(events.next_event(sub2, timeout=0.5)["data"], {"a": 2})
        events.unsubscribe(sub2)


class TestStreamRoute(unittest.TestCase):
    """SSE 路由：鉴权 / 帧协议 / 回落 / 不泄漏"""

    @classmethod
    def setUpClass(cls):
        webapp_pkg.DATA_DIR = Path(_TMP)
        cls.app = create_app()
        cls.c = cls.app.test_client()
        r = cls.c.post("/api/auth/login",
                       json={"username": "admin", "password": "admin"})
        assert r.status_code == 200, r.get_data(as_text=True)
        token = r.get_json()["token"]
        r = cls.c.post("/api/auth/password",
                       json={"old_password": "admin", "new_password": "live1234"},
                       headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.get_data(as_text=True)
        r = cls.c.post("/api/auth/login",
                       json={"username": "admin", "password": "live1234"})
        cls.token = r.get_json()["token"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    def setUp(self):
        events.reset()

    tearDown = setUp

    def _open(self, topics="alerts"):
        return self.c.get(f"/api/stream?topics={topics}", headers=self._h())

    def test_stream_requires_auth(self):
        self.assertEqual(self.c.get("/api/stream").status_code, 401)

    def test_hello_frame_and_topics_filter(self):
        resp = self._open("alerts,tasks,bogus")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(resp.headers.get("X-Accel-Buffering"), "no")
        it = resp.response
        hello = _frames(next(it))[0]
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(sorted(hello["topics"]), ["alerts", "tasks"])  # 未知主题被剔除
        resp.close()
        self.assertEqual(events.stats()["subscribers"], 0)

    def test_unknown_topics_fall_back_to_defaults(self):
        resp = self._open("nope")
        hello = _frames(next(resp.response))[0]
        self.assertEqual(sorted(hello["topics"]), sorted(KNOWN_TOPICS))
        resp.close()

    def test_event_frame_delivered_after_publish(self):
        resp = self._open("alerts")
        it = resp.response
        next(it)   # hello
        events.publish("alerts", {"alert_id": 7, "status": "resolved"})
        frame = _frames(next(it))[0]
        self.assertEqual(frame["type"], "event")
        self.assertEqual(frame["topic"], "alerts")
        self.assertEqual(frame["data"]["alert_id"], 7)
        self.assertIsInstance(frame["seq"], int)
        resp.close()
        self.assertEqual(events.stats()["subscribers"], 0)

    def test_no_subscription_leak_when_client_never_reads(self):
        # 生成器未启动就关闭：订阅发生在生成器体内，因此不会残留订阅者
        resp = self._open("alerts")
        self.assertEqual(resp.status_code, 200)
        resp.close()
        self.assertEqual(events.stats()["subscribers"], 0)

    def test_subscriber_cap_emits_error_frame_and_closes(self):
        # 先占满上限，再建流：新订阅拿不到名额 → 错帧 + 立即收尾
        blockers = [events.subscribe(["alerts"])
                    for _ in range(events.MAX_SUBSCRIBERS)]
        try:
            resp = self._open("alerts")
            frames = _frames(next(resp.response))
            self.assertEqual(frames[0]["type"], "error")
            resp.close()
            self.assertEqual(events.stats()["subscribers"],
                             events.MAX_SUBSCRIBERS)
        finally:
            for b in blockers:
                events.unsubscribe(b)


class TestProducerHooks(unittest.TestCase):
    """生产者埋点：任务/告警状态漏斗 + 建议落库 + 引擎新建告警"""

    def setUp(self):
        events.reset()
        self.db = str(Path(tempfile.mkdtemp(prefix="rt_prod_")) / "t.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="",
            created_at=int(time.time())))

    def tearDown(self):
        events.reset()
        shutil.rmtree(str(Path(self.db).parent), ignore_errors=True)

    def _create_task(self):
        return dbm.create_task(self.db, self.proj_id, "deploy", 0, "svc",
                               "prod", "1.0.0", "", 1)

    def test_update_task_publishes_only_on_status_change(self):
        tid = self._create_task()
        sub = events.subscribe(["tasks"])
        dbm.update_task(self.db, tid, status="running")
        ev = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev["topic"], "tasks")
        self.assertEqual(ev["data"], {"task_id": tid, "status": "running"})
        dbm.update_task(self.db, tid, log_file="x.log")   # 非状态字段不推
        self.assertIsNone(events.next_event(sub, timeout=0.05))
        events.unsubscribe(sub)

    def test_alert_status_update_publishes(self):
        now = int(time.time())
        aid = dbm.create_record(self.db, "alerts", dict(
            rule_id=1, fingerprint="fp", status="firing", severity="P2",
            subject="svc", description="d", target_info={},
            first_seen_at=now, last_seen_at=now, notify_count=0,
            escalation_level=0, escalation_at=now))
        sub = events.subscribe(["alerts"])
        dbm.update_record(self.db, "alerts", aid, {"status": "acked"})
        self.assertEqual(events.next_event(sub, timeout=0.5)["data"]["status"],
                         "acked")
        dbm.update_record(self.db, "alerts", aid, {"notify_count": 3})  # 不推
        self.assertIsNone(events.next_event(sub, timeout=0.05))
        events.unsubscribe(sub)

    def test_engine_fire_publishs_new_alert_only(self):
        from webapp.alert_engine import AlertEngine
        eng = AlertEngine(self.db)
        sub = events.subscribe(["alerts"])
        rule = {"id": 1, "severity": "P1", "escalation_interval": 30}
        aid = eng._fire(rule, "svc:prod", "服务异常", "描述",
                        {"service": "svc"}, windows=[])
        ev = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev["data"]["action"], "fired")
        self.assertEqual(ev["data"]["alert_id"], aid)
        self.assertEqual(ev["data"]["severity"], "P1")
        # 同指纹重复命中：只刷 last_seen_at，不应再产生事件（防事件风暴）
        eng._fire(rule, "svc:prod", "服务异常", "描述", {"service": "svc"},
                  windows=[])
        self.assertIsNone(events.next_event(sub, timeout=0.05))
        # 引擎恢复走 update_record 漏斗，不双发
        alert = dbm.get_record(self.db, "alerts", aid)
        eng._resolve(alert, "auto")
        ev2 = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev2["data"]["status"], "resolved")
        events.unsubscribe(sub)

    def test_save_advice_publishes_agent_event(self):
        from agent import advisor
        sub = events.subscribe(["agent"])
        aid = advisor.save_advice(self.db, "service", 5, "标题", "正文")
        ev = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev["data"]["action"], "advice_new")
        self.assertEqual(ev["data"]["advice_id"], aid)
        events.unsubscribe(sub)

    def test_monitor_cycle_publishs_round(self):
        from webapp.monitor import MonitorScheduler
        mon = MonitorScheduler(self.db, interval=10)
        sub = events.subscribe(["monitor"])
        with mock.patch.object(mon, "_probe_envs", return_value=None):
            mon._cycle()
        ev = events.next_event(sub, timeout=0.5)
        self.assertEqual(ev["topic"], "monitor")
        self.assertEqual(ev["data"]["envs"], 0)
        self.assertEqual(ev["data"]["round"], 1)
        events.unsubscribe(sub)


if __name__ == "__main__":
    unittest.main()
