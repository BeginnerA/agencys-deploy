"""告警日志上下文接口测试：三段查询 / 同秒游标 / 边界兜底 / 不可用场景"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg

_TMP = tempfile.mkdtemp(prefix="alert_log_ctx_test_")

from webapp import create_app
from webapp import db as dbm


class TestAlertLogContext(unittest.TestCase):
    """基于真实 create_app + 独立临时 DB 的日志上下文 API 测试"""

    @classmethod
    def setUpClass(cls):
        # 执行期绑定临时目录：避免与其它测试文件的模块级 DATA_DIR 重绑定互相覆盖
        webapp_pkg.DATA_DIR = Path(_TMP)
        cls.app = create_app()
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers=cls._h(token))
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.token = cls._login(cls.c, "admin", "test1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    @staticmethod
    def _h(token):
        return {"Authorization": f"Bearer {token}"}

    def _db_path(self):
        return str(Path(_TMP) / "platform.db")

    def setUp(self):
        """每个用例独立数据：清空 log_entries，避免同服务同秒数据相互污染"""
        conn = dbm.get_conn(self._db_path())
        try:
            conn.execute("DELETE FROM log_entries")
            conn.commit()
        finally:
            conn.close()

    def _make_rule(self, metric_type="log_keyword", metric="ERROR"):
        r = self.c.post("/api/alerts/rules", json=dict(
            name="ctx test", metric_type=metric_type, metric=metric,
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P3",
            channels=["wecom"]), headers=self._h(self.token))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()["id"]

    def _seed_entries(self, n=20, base_ts=1787000000):
        """插入 n 条 log_entries（前 10 条 ts=base_ts，其余 ts=base_ts+1），
        返回 rowid 列表（按插入顺序）"""
        conn = dbm.get_conn(self._db_path())
        rowids = []
        try:
            for i in range(n):
                ts = base_ts if i < 10 else base_ts + 1
                cur = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, "
                    "line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, 1, ?, '[]')",
                    (ts, "prod", "svc-a",
                     f"/logs/prod/svc-a/2026-08-18.log", i,
                     f"line {i} content"))
                rowids.append(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        return rowids

    def _make_alert(self, rule_id, target_info, seq=0):
        now = int(time.time())
        return dbm.create_record(self._db_path(), "alerts", dict(
            rule_id=rule_id, fingerprint=f"ctx-{seq}-{now}",
            status="firing", severity="P3", subject="ctx subject",
            description="desc", target_info=target_info,
            first_seen_at=now, last_seen_at=now, notify_count=0,
            escalation_level=0, escalation_at=now))

    def _get_logs(self, alert_id, lines=None):
        url = f"/api/alerts/{alert_id}/logs"
        if lines is not None:
            url += f"?lines={lines}"
        return self.c.get(url, headers=self._h(self.token))

    def test_context_three_segments(self):
        """正常三段：before 正序 / hit 精确 / after 正序"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[9]  # 第 10 条，ts=base_ts 段最后一条
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=1)
        r = self._get_logs(aid, lines=3)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data["available"])
        self.assertEqual([x["id"] for x in data["before"]], rowids[6:9])
        self.assertEqual(data["hit"]["id"], hit_id)
        self.assertNotIn("fallback", data["hit"])
        self.assertEqual([x["id"] for x in data["after"]], rowids[10:13])
        self.assertEqual(data["keyword"], "ERROR")

    def test_same_second_composite_cursor(self):
        """同秒多条日志：仅按 id 分界，不串行"""
        rid = self._make_rule()
        rowids = self._seed_entries(n=5, base_ts=1787000000)
        hit_id = rowids[2]
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=2)
        data = self._get_logs(aid, lines=10).get_json()
        self.assertEqual([x["id"] for x in data["before"]], rowids[0:2])
        self.assertEqual(data["hit"]["id"], hit_id)
        self.assertEqual([x["id"] for x in data["after"]], rowids[3:5])

    def test_hit_at_start(self):
        """命中行在开头：before 为空"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": rowids[0],
                                     "hit_ts": 1787000000}, seq=3)
        data = self._get_logs(aid, lines=3).get_json()
        self.assertEqual(data["before"], [])
        self.assertEqual(data["hit"]["id"], rowids[0])
        self.assertEqual(len(data["after"]), 3)

    def test_hit_at_end(self):
        """命中行在结尾：after 为空"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[-1]  # 最后一条，ts=base_ts+1
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000001}, seq=4)
        data = self._get_logs(aid, lines=3).get_json()
        self.assertEqual(data["after"], [])
        self.assertEqual(data["hit"]["id"], hit_id)
        self.assertEqual(len(data["before"]), 3)

    def test_hit_missing_falls_back(self):
        """命中行被清理：用 before 最后一行兜底并标记 fallback（兜底行移出 before）"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[9]
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=5)
        conn = dbm.get_conn(self._db_path())
        try:
            conn.execute("DELETE FROM log_entries WHERE id=?", (hit_id,))
            conn.commit()
        finally:
            conn.close()
        data = self._get_logs(aid, lines=3).get_json()
        self.assertTrue(data["available"])
        self.assertEqual(data["hit"]["id"], rowids[8])
        self.assertTrue(data["hit"].get("fallback"))
        # 兜底行已从 before 移除，不重复出现，且未泄漏 fallback 标记
        self.assertEqual([x["id"] for x in data["before"]], rowids[6:8])
        self.assertNotIn("fallback", data["before"][-1])

    def test_hit_missing_after_fallback(self):
        """命中行被清理且 before 为空：用 after 第一行兜底标记 fallback"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[0]  # 第一行，before 天然为空
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=10)
        conn = dbm.get_conn(self._db_path())
        try:
            conn.execute("DELETE FROM log_entries WHERE id=?", (hit_id,))
            conn.commit()
        finally:
            conn.close()
        data = self._get_logs(aid, lines=3).get_json()
        self.assertTrue(data["available"])
        self.assertEqual(data["hit"]["id"], rowids[1])
        self.assertTrue(data["hit"].get("fallback"))
        # after LIMIT 3 取 [rowids[1..3]]，兜底弹出 rowids[1]，剩余 2 行
        self.assertEqual([x["id"] for x in data["after"]], rowids[2:4])

    def test_env_service_filter(self):
        """其他 env/service 的日志不混入上下文"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[5]
        # 插入干扰行：不同环境/服务的同 ts 日志
        conn = dbm.get_conn(self._db_path())
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, "
                    "line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, 1, ?, '[]')",
                    (1787000000, "test", "svc-b", "/logs/test/svc-b/2026-08-18.log",
                     i, f"intruder {i}"))
            conn.commit()
        finally:
            conn.close()
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=11)
        data = self._get_logs(aid, lines=100).get_json()
        ids = {x["id"] for x in data["before"] + [data["hit"]] + data["after"]}
        self.assertEqual(data["hit"]["id"], hit_id)
        # 所有上下文行都属于 prod/svc-a：干扰行 id 全部大于本用例插入的最大 rowid，
        # 若过滤失效它们会进入 after（同 ts 且 id 更大，排在 ts+1 行之前）
        self.assertLessEqual(max(ids), rowids[-1])

    def test_expired_when_all_gone(self):
        """before/after/hit 均无数据：reason=expired"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        hit_id = rowids[9]
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": hit_id,
                                     "hit_ts": 1787000000}, seq=6)
        conn = dbm.get_conn(self._db_path())
        try:
            conn.execute("DELETE FROM log_entries")
            conn.commit()
        finally:
            conn.close()
        data = self._get_logs(aid).get_json()
        self.assertFalse(data["available"])
        self.assertEqual(data["reason"], "expired")

    def test_old_alert_no_position(self):
        """旧告警无 entry_id/hit_ts：reason=no_position"""
        rid = self._make_rule()
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR"}, seq=7)
        data = self._get_logs(aid).get_json()
        self.assertFalse(data["available"])
        self.assertEqual(data["reason"], "no_position")

    def test_not_log_alert(self):
        """非日志类规则告警：reason=not_log_alert"""
        rid = self._make_rule(metric_type="metric", metric="cpu")
        aid = self._make_alert(rid, {"env": "prod", "host": "h1"}, seq=8)
        data = self._get_logs(aid).get_json()
        self.assertFalse(data["available"])
        self.assertEqual(data["reason"], "not_log_alert")

    def test_lines_clamped(self):
        """lines 越界钳制到 1~200"""
        rid = self._make_rule()
        rowids = self._seed_entries()
        aid = self._make_alert(rid, {"env": "prod", "service": "svc-a",
                                     "keyword": "ERROR", "entry_id": rowids[9],
                                     "hit_ts": 1787000000}, seq=9)
        self.assertEqual(self._get_logs(aid, lines=999).get_json()["lines"], 200)
        self.assertEqual(self._get_logs(aid, lines=0).get_json()["lines"], 1)

    def test_alert_not_found(self):
        r = self._get_logs(999999)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
