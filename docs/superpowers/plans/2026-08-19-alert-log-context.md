# 告警详情日志上下文实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 日志关键词告警触发时记录命中行位置（entry_id/hit_ts），告警详情抽屉新增"日志上下文"区块，实时查询命中行前后各 100 行日志并高亮。

**Architecture:** 触发侧在 `log_collector.py` 索引日志时收集 rowid，关键词命中时把 `entry_id + hit_ts` 经 `trigger_log_alert` 写入 `alerts.target_info`（向后兼容，老字段 `context` 不动）；查询侧新增 `GET /api/alerts/<id>/logs`，按 `env + service` 过滤 `log_entries`，以 `(ts, id)` 复合游标取 before/hit/after 三段；前端详情抽屉新增区块，先 esc 再高亮关键词防 XSS。

**Tech Stack:** Python Flask + SQLite（FTS5）、原生 JS（无框架）、现有 CSS 变量体系、unittest。

---

### Task 1: 采集侧传递命中位置

**Files:**
- Modify: `webapp/log_collector.py`（`_index_log_entries` 返回 rowid 列表；`_collect_container` 传递 rowids；`_scan_keywords_in_entries` 增加 rowids 参数并传 entry_id/hit_ts）
- Modify: `webapp/alert_engine.py`（`trigger_log_alert` 增加 `entry_id`/`hit_ts` 参数写入 target_info）
- Test: `tests/test_log_collector.py`、`tests/test_alert_engine.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_log_collector.py` 的 `TestLogCollector` 类末尾（`test_index_log_lines_writes_fts` 之后）追加两个测试：

```python
    def test_index_log_entries_returns_rowids(self):
        """索引返回 rowid 列表，与 entries 一一对应"""
        path = os.path.join(self.log_dir, "test.log")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        entries = [
            (1787049045, "prod", "api", "line1", path, 1, 1),
            (1787049046, "prod", "api", "line2", path, 2, 1),
        ]
        rowids = self.collector._index_log_entries(entries)
        self.assertEqual(len(rowids), 2)
        self.assertGreater(rowids[1], rowids[0])
        conn = dbm.get_conn(self.db)
        try:
            r = conn.execute(
                "SELECT content FROM log_entries WHERE id=?", (rowids[1],)
            ).fetchone()
            self.assertEqual(r["content"], "line2")
        finally:
            conn.close()

    def test_scan_keywords_passes_entry_id_and_ts(self):
        """关键词命中：把命中行 entry_id + ts 传给 trigger_log_alert"""
        self._make_kw_rule("ERROR")
        path = os.path.join(self.log_dir, "test.log")
        entries = [
            (1787049045, "prod", "api", "INFO normal log", path, 0, 1),
            (1787049046, "prod", "api", "ERROR something broke", path, 10, 1),
        ]
        rowids = self.collector._index_log_entries(entries)
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1, rowids)
        call_kwargs = mock_engine.trigger_log_alert.call_args[1]
        self.assertEqual(call_kwargs["entry_id"], rowids[1])
        self.assertEqual(call_kwargs["hit_ts"], 1787049046)
```

在 `tests/test_alert_engine.py` 的 `test_trigger_log_alert_creates_alert` 之后追加：

```python
    def test_trigger_log_alert_records_position(self):
        """trigger_log_alert: 命中行 entry_id/hit_ts 写入 target_info"""
        rid = self._log_kw_rule(metric="ERROR")
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        e.trigger_log_alert(rule, "api-svc", "prod", "ERROR: db timeout",
                            ["ERROR: db timeout"], self.env_id,
                            matched_keyword="ERROR", entry_id=42, hit_ts=1787000000)
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(alerts[0]["target_info"]["entry_id"], 42)
        self.assertEqual(alerts[0]["target_info"]["hit_ts"], 1787000000)
        # 未传位置参数时不写入（老调用向后兼容）
        e2 = self._engine()
        e2.trigger_log_alert(rule, "api-svc", "prod", "ERROR: again",
                             ["ERROR: again"], self.env_id,
                             matched_keyword="ERROR")
        alerts2 = dbm.list_records(self.db, "alerts")
        self.assertNotIn("entry_id", alerts2[0]["target_info"])
        self.assertNotIn("hit_ts", alerts2[0]["target_info"])
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m pytest tests/test_log_collector.py::TestLogCollector::test_index_log_entries_returns_rowids tests/test_log_collector.py::TestLogCollector::test_scan_keywords_passes_entry_id_and_ts tests/test_alert_engine.py -v
```
Expected: `test_index_log_entries_returns_rowids` FAIL（`TypeError: 'NoneType' object is not subscriptable`）；`test_scan_keywords_passes_entry_id_and_ts` FAIL（`trigger_log_alert` mock 调用里没有 `entry_id`）；`test_trigger_log_alert_records_position` FAIL（`KeyError: 'entry_id'`）。

- [ ] **Step 3: 实现 log_collector.py 改动**

`_index_log_entries`（229-247 行）改为收集并返回 rowids：

```python
    def _index_log_entries(self, entries: list) -> list:
        """批量写入 log_entries + 同步 FTS5；返回每条日志的 rowid（与 entries 一一对应）"""
        conn = dbm.get_conn(self.db_path)
        rowids = []
        try:
            for ts, env, service, content, file_path, offset, count in entries:
                cursor = conn.execute(
                    "INSERT INTO log_entries (ts, env, service, container, file_path, line_offset, line_count, content, keywords) "
                    "VALUES (?, ?, ?, '', ?, ?, ?, ?, '[]')",
                    (ts, env, service, file_path, offset, count, content)
                )
                rowid = cursor.lastrowid
                rowids.append(rowid)
                conn.execute(
                    "INSERT INTO log_fts (rowid, ts, env, service, content) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rowid, str(ts), env, service, content)
                )
            conn.commit()
            return rowids
        finally:
            conn.close()
```

`_collect_container`（217-221 行）把 rowids 传给扫描：

```python
        if entries:
            rowids = self._index_log_entries(entries)
            self._scan_keywords_in_entries(entries, env_name, env_id, rowids)
            self._update_scan_state(env_name, host, container, service,
                                    last_line_ts or "", line_offset)
```

`_scan_keywords_in_entries`（264-293 行）增加 `rowids` 参数，命中时传 entry_id/hit_ts：

```python
    def _scan_keywords_in_entries(self, entries: list, env_name: str, env_id: int,
                                  rowids: list = None):
        """扫描日志条目中的关键词，命中时触发告警

        rowids: _index_log_entries 返回的 rowid 列表，与 entries 一一对应；
        传入后命中行携带 entry_id/hit_ts，供告警详情展示日志上下文。
        """
        rules = getattr(self, '_current_kw_rules', None)
        if not rules:
            return
        engine = get_engine()
        if engine is None:
            return
        service = entries[0][2] if entries else ""
        for rule in rules:
            metric_raw = (rule.get("metric") or "").strip()
            if not metric_raw:
                continue
            keywords = [kw.strip() for kw in metric_raw.split(",") if kw.strip()]
            if not keywords:
                continue
            # 扫描每条日志，找到第一个命中即触发（同规则同服务由指纹去重）
            for i, (ts, env, svc, content, file_path, offset, count) in enumerate(entries):
                if not content:
                    continue
                for kw in keywords:
                    if kw in content:
                        ctx = [content]
                        engine.trigger_log_alert(
                            rule, svc, env_name, content, ctx, env_id,
                            matched_keyword=kw,
                            entry_id=rowids[i] if rowids and i < len(rowids) else None,
                            hit_ts=ts or None)
                        break  # 本条日志只触发一次（取首个命中关键词）
                else:
                    continue
                break  # 本规则已触发，不再扫描后续日志行（指纹去重，同规则同服务只告警一次）
```

- [ ] **Step 4: 实现 alert_engine.py 改动**

`trigger_log_alert`（470-496 行）增加参数并把位置写入 target_info：

```python
    def trigger_log_alert(self, rule: dict, service: str, env_name: str,
                          line: str, context: list, env_id: int,
                          matched_keyword: str = "", entry_id: int = None,
                          hit_ts: int = None):
        """日志关键词命中：创建/更新日志告警（指纹含关键词，收敛由部分唯一索引保证）

        matched_keyword: 实际命中的那个关键词（多关键词场景下精确到单个词）
        entry_id/hit_ts: 命中日志行在 log_entries 的 rowid 与其时间戳，
        写入 target_info 供详情抽屉实时查询上下文（旧告警无此字段，向后兼容）
        """
        # 查询 env_id 对应的环境记录
        envs = dbm.list_records(self._db_path, "environments", "id=?", (env_id,))
        env = envs[0] if envs else {"id": env_id, "project_id": 0, "name": env_name}
        # 查询 service name → service_id（按项目精确匹配）
        service_id = None
        svcs = dbm.list_records(self._db_path, "services",
                                "name=? AND project_id=?",
                                (service, env["project_id"]))
        if svcs:
            service_id = svcs[0]["id"]
        keyword = matched_keyword or rule.get("metric") or "log"
        target = f"log:{env_id}:{service}:{keyword}"
        # scope 检查
        if not self._in_scope(rule, env, service=service, service_id=service_id):
            return
        target_info = {"env": env_name, "service": service,
                       "keyword": keyword, "context": context[:5]}
        if entry_id is not None:
            target_info["entry_id"] = entry_id
        if hit_ts:
            target_info["hit_ts"] = hit_ts
        self._fire(rule, target,
                   subject=f"日志告警 [{keyword}]: {service}",
                   description=f"{env_name}/{service} 日志命中关键词 {keyword}:\n{line}",
                   target_info=target_info)
```

- [ ] **Step 5: 运行测试确认通过**

Run:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m pytest tests/test_log_collector.py tests/test_alert_engine.py -v
```
Expected: 全部 PASS（含既有用例，验证向后兼容未破坏）。

- [ ] **Step 6: 提交**

```bash
git add webapp/log_collector.py webapp/alert_engine.py tests/test_log_collector.py tests/test_alert_engine.py
git commit -m "feat: 日志告警记录命中行位置 entry_id/hit_ts"
```

---

### Task 2: 后端查询接口 GET /api/alerts/<id>/logs

**Files:**
- Modify: `webapp/routes/alert_routes.py`（`alert_events` 函数之后新增接口）
- Create: `tests/test_alert_log_context.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_alert_log_context.py`（结构照抄 `tests/test_alert_routes.py` 的 create_app + 登录模式）：

```python
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
webapp_pkg.DATA_DIR = Path(_TMP)

from webapp import create_app
from webapp import db as dbm


class TestAlertLogContext(unittest.TestCase):
    """基于真实 create_app + 独立临时 DB 的日志上下文 API 测试"""

    @classmethod
    def setUpClass(cls):
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
        """命中行被清理：用 before 最后一行兜底并标记 fallback"""
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
        self.assertEqual([x["id"] for x in data["before"]], rowids[6:9])

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
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m pytest tests/test_alert_log_context.py -v
```
Expected: 除 `test_alert_not_found` 外全部 FAIL（`404 NOT FOUND` — 接口尚未实现）。

- [ ] **Step 3: 实现接口**

在 `webapp/routes/alert_routes.py` 的 `alert_events` 函数（123-136 行）之后、"告警批量操作"之前插入：

```python
@alert_bp.route("/alerts/<int:alert_id>/logs")
@require_auth
def alert_logs(alert_id):
    """告警日志上下文：按 (ts, id) 复合游标实时查 log_entries，取命中行前后 N 行

    复合游标必要性：ts 秒级精度，同秒多条日志靠 id 自增保持稳定次序；
    按 env+service 过滤避免其他容器日志混入。不可用场景返回 reason 供前端分发。
    """
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    rule = dbm.get_record(db, "alert_rules", a["rule_id"]) or {}
    if rule.get("metric_type") != "log_keyword":
        return jsonify({"available": False, "reason": "not_log_alert"})
    info = a.get("target_info") or {}
    entry_id = info.get("entry_id")
    hit_ts = info.get("hit_ts")
    if not entry_id or not hit_ts:
        return jsonify({"available": False, "reason": "no_position"})
    env = info.get("env") or ""
    service = info.get("service") or ""
    keyword = info.get("keyword") or ""
    lines = min(200, max(1, request.args.get("lines", 100, type=int)))

    conn = dbm.get_conn(db)
    try:
        # before：倒序取 lines 条再翻转回正序
        before_rows = conn.execute(
            "SELECT id, ts, content FROM log_entries "
            "WHERE env=? AND service=? AND (ts < ? OR (ts = ? AND id < ?)) "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (env, service, hit_ts, hit_ts, entry_id, lines)
        ).fetchall()
        before = [dict(r) for r in reversed(before_rows)]
        # hit：精确取命中行
        hit_row = conn.execute(
            "SELECT id, ts, content FROM log_entries WHERE id=?",
            (entry_id,)
        ).fetchone()
        hit = dict(hit_row) if hit_row else None
        # after：正序取 lines 条
        after_rows = conn.execute(
            "SELECT id, ts, content FROM log_entries "
            "WHERE env=? AND service=? AND (ts > ? OR (ts = ? AND id > ?)) "
            "ORDER BY ts ASC, id ASC LIMIT ?",
            (env, service, hit_ts, hit_ts, entry_id, lines)
        ).fetchall()
        after = [dict(r) for r in after_rows]
    finally:
        conn.close()

    # 命中行已被清理（如超保留期）：用相邻行兜底标记，仍展示上下文
    if not hit:
        hit = before[-1] if before else (after[0] if after else None)
        if hit:
            hit["fallback"] = True
    if not hit and not before and not after:
        return jsonify({"available": False, "reason": "expired",
                        "env": env, "service": service, "keyword": keyword,
                        "hit": None, "before": [], "after": [], "lines": lines})
    return jsonify({
        "available": True, "env": env, "service": service,
        "keyword": keyword, "hit": hit, "before": before, "after": after,
        "lines": lines,
    })
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m pytest tests/test_alert_log_context.py tests/test_alert_routes.py -v
```
Expected: 全部 PASS（新接口用例 + 既有告警路由用例回归）。

- [ ] **Step 5: 提交**

```bash
git add webapp/routes/alert_routes.py tests/test_alert_log_context.py
git commit -m "feat: 告警详情日志上下文查询接口 GET /api/alerts/<id>/logs"
```

---

### Task 3: 前端"日志上下文"区块

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`（`openAlertDetail` 新增区块 + `loadAlertLogCtx`/`renderLogCtx`/`fmtHHMMSS`/`highlightKw` 函数）
- Modify: `webapp/static/css/pages/alerts.css`（区块样式）

- [ ] **Step 1: 加 CSS 样式**

在 `webapp/static/css/pages/alerts.css` 的 `.evt-detail` 规则（142 行）之后追加：

```css
/* 告警详情-日志上下文 */
.alert-log-ctx-info{font-size:var(--fs-12);color:var(--text-2);margin-bottom:8px}
.alert-log-ctx-lines{max-height:340px;overflow-y:auto;border:1px solid var(--border);
  border-radius:var(--r-ctl);background:rgba(17,34,65,.3);font-family:monospace;font-size:var(--fs-12)}
.alert-log-ctx-line{display:flex;gap:10px;padding:3px 12px;border-left:3px solid transparent;
  white-space:pre-wrap;word-break:break-all}
.alert-log-ctx-line:hover{background:rgba(0,212,255,.05)}
.alert-log-ctx-line.hit{background:rgba(255,193,7,.12);border-left-color:#ffc107}
.ctx-ts{color:var(--text-2);font-family:var(--font-num);white-space:nowrap}
.ctx-content{color:var(--text-1);flex:1}
.alert-log-ctx-line mark{background:#ffc107;color:#1a1a1a;padding:0 2px;border-radius:2px}
```

- [ ] **Step 2: 实现 JS 逻辑**

`webapp/static/js/pages/alerts.js` 的 `openAlertDetail` 中，在投递历史区块（1268-1273 行）**之前**插入日志上下文区块：

```js
        <div class="alert-detail-section" id="alertLogCtxSection">
          <div class="alert-detail-section-title">📜 日志上下文</div>
          <div id="alertLogCtxList" class="alert-log-ctx">
            <div class="muted" style="padding:12px">加载中...</div>
          </div>
        </div>
```

在 `openAlertDetail` 函数体尾部（`}` 之前，即投递历史加载 try/catch 结束之后）追加调用：

```js
    // 加载日志上下文（与投递历史并行）
    loadAlertLogCtx(alertId);
```

在 `openAlertDetail` 函数之后新增三个函数（放在"工具函数（告警列表用）"注释块之前）：

```js
  // ---- 告警详情：日志上下文（命中行前后 N 行 + 高亮） ----

  async function loadAlertLogCtx(alertId) {
    const section = document.getElementById('alertLogCtxSection');
    const el = document.getElementById('alertLogCtxList');
    if (!section || !el) return;
    try {
      const data = await api(`/api/alerts/${alertId}/logs?lines=100`);
      if (!data.available) {
        // 非日志告警：不渲染该区块
        if (data.reason === 'not_log_alert') { section.remove(); return; }
        const msg = data.reason === 'no_position'
          ? '该告警未记录日志位置，无法展示上下文'
          : data.reason === 'expired'
            ? '日志已超出保留期，无法展示上下文'
            : '无法展示上下文';
        el.innerHTML = `<div class="muted" style="padding:12px;text-align:center">${msg}</div>`;
        return;
      }
      renderLogCtx(el, data);
    } catch(e) {
      console.warn('loadAlertLogCtx failed', e);
      el.innerHTML = `<div style="padding:12px;text-align:center">
        <div class="muted" style="color:var(--danger)">加载失败：${esc(e.message || '未知错误')}</div>
        <button class="btn sm ghost" id="logCtxRetryBtn" style="margin-top:8px">重试</button>
      </div>`;
      const retryBtn = document.getElementById('logCtxRetryBtn');
      if (retryBtn) retryBtn.onclick = () => loadAlertLogCtx(alertId);
    }
  }

  function renderLogCtx(el, data) {
    const rows = [...data.before, data.hit, ...data.after].filter(Boolean);
    const hitId = data.hit && !data.hit.fallback ? data.hit.id : null;  // 兜底行不标命中
    el.innerHTML = `
      <div class="alert-log-ctx-info">
        命中行前 ${data.before.length} 行${hitId ? ' · 命中行' : ''} · 后 ${data.after.length} 行
      </div>
      <div class="alert-log-ctx-lines">
        ${rows.map(r => {
          const isHit = hitId !== null && r.id === hitId;
          return `<div class="alert-log-ctx-line${isHit ? ' hit' : ''}">
            <span class="ctx-ts">${fmtHHMMSS(r.ts)}</span>
            <span class="ctx-content">${highlightKw(esc(r.content || ''), data.keyword)}</span>
          </div>`;
        }).join('')}
      </div>`;
  }

  // 秒级时间戳 → "HH:MM:SS"
  function fmtHHMMSS(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }

  // 内容已 esc 转义，关键词同样先转义再匹配高亮（安全 + 特殊字符关键词可用）
  function highlightKw(text, kw) {
    if (!kw) return text;
    const escKw = esc(kw).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return text.replace(new RegExp(`(${escKw})`, 'g'), '<mark>$1</mark>');
    } catch(e) {
      return text;
    }
  }
```

- [ ] **Step 3: 手工验证**

Run（后台启动）:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m webapp.app --port 5678
```
浏览器打开 `http://127.0.0.1:5678` 登录，验证清单：

1. **日志告警详情**：打开一个带 entry_id 的日志告警 → "日志上下文"区块出现在元信息与投递历史之间；命中行有黄色左边框 + 背景高亮；关键词在行内有 `<mark>` 高亮；信息条显示"命中行前 N 行 · 命中行 · 后 N 行"；列表超高时滚动
2. **旧告警**：打开 entry_id 缺失的旧日志告警 → 提示"该告警未记录日志位置，无法展示上下文"
3. **非日志告警**：打开指标/状态/任务告警 → 不出现日志上下文区块
4. **超保留期**：`DELETE FROM log_entries` 清空后打开 → 提示"日志已超出保留期，无法展示上下文"
5. **加载失败**：停掉后端后打开详情 → 错误提示 + "重试"按钮可点击
6. **XSS**：日志内容含 `<script>` 时原样显示为文本，不执行

- [ ] **Step 4: 提交**

```bash
git add webapp/static/js/pages/alerts.js webapp/static/css/pages/alerts.css
git commit -m "feat: 告警详情抽屉新增日志上下文区块（命中行/关键词高亮）"
```

---

### Task 4: 全量回归

- [ ] **Step 1: 运行全部测试**

Run:
```bash
cd d:\work\IdeaProjects\Reid\agencys\agencys-deploy
.venv\Scripts\python -m pytest tests -v
```
Expected: 全部 PASS（含既有 log/alert/db 相关用例，确认无回归）。

- [ ] **Step 2: 更新规格文档状态**

规格文档 `docs/superpowers/specs/2026-08-19-alert-log-context-design.md` 无需改动（实现与规格一致：复合游标、兜底标记 fallback、reason 分发、lines 钳制均已落地）。

- [ ] **Step 3: 提交收尾**

```bash
git add docs/superpowers/plans/2026-08-19-alert-log-context.md
git commit -m "docs: 告警详情日志上下文实施计划"
```
