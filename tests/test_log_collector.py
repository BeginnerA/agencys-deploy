"""日志采集器单元测试"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webapp import db as dbm
from webapp.log_collector import (
    LogCollector, _parse_docker_log_line, _build_log_file_path, _cursor_from_ts,
)


class TestParseDockerLogLine(unittest.TestCase):
    """docker logs 行解析"""

    def test_parse_with_timestamp(self):
        line = "2026-08-18T10:30:45.123456789Z ERROR something failed"
        ts, content = _parse_docker_log_line(line)
        self.assertEqual(ts, 1787049045)  # 2026-08-18T10:30:45 UTC
        self.assertEqual(content, "ERROR something failed")

    def test_parse_without_timestamp(self):
        line = "plain log line without timestamp"
        ts, content = _parse_docker_log_line(line)
        self.assertEqual(ts, 0)
        self.assertEqual(content, line)

    def test_parse_empty_line(self):
        ts, content = _parse_docker_log_line("")
        self.assertEqual(ts, 0)
        self.assertEqual(content, "")


class TestBuildLogFilePath(unittest.TestCase):
    """日志文件路径构建"""

    def test_build_path(self):
        base = "/tmp/logs"
        path = _build_log_file_path(base, "prod", "api-service", "2026-08-18")
        # 跨平台兼容：统一用正斜杠比较
        normalized = path.replace("\\", "/")
        self.assertEqual(normalized, "/tmp/logs/collect/prod/api-service/2026-08-18.log")


class TestCursorFromTs(unittest.TestCase):
    """#4 秒级游标改微秒：+1μs 跳过 docker logs --since 的含边界（>= 过滤），
    避免最后一行（及同秒所有行）每轮重复采集"""

    def test_plus_one_microsecond(self):
        # 2026-08-18T10:30:45.123456Z + 1μs
        self.assertEqual(_cursor_from_ts(1787049045, 123456),
                         "2026-08-18T10:30:45.123457+00:00")

    def test_microsecond_boundary_rolls_over(self):
        # 2026-08-18T10:30:45.999999Z + 1μs → 10:30:46（微秒归零，isoformat 省略）
        self.assertEqual(_cursor_from_ts(1787049045, 999999),
                         "2026-08-18T10:30:46+00:00")

    def test_zero_microseconds_advances(self):
        self.assertEqual(_cursor_from_ts(1787049045, 0),
                         "2026-08-18T10:30:45.000001+00:00")


class TestLogCollector(unittest.TestCase):
    """采集器核心逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        self.collector = LogCollector(self.db, self.log_dir, ssh_func=self._fake_ssh)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        return self._fake_ssh_output

    def test_append_to_file_creates_directory(self):
        """文件追加：自动创建目录"""
        path = os.path.join(self.log_dir, "collect", "prod", "api", "2026-08-18.log")
        self.collector._append_to_file(path, ["line1\n", "line2\n"])
        self.assertTrue(os.path.isfile(path))
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("line1", content)
        self.assertIn("line2", content)

    def test_update_scan_state_insert_and_update(self):
        """游标更新：首次插入 + 后续更新"""
        self.collector._update_scan_state("prod", "10.0.0.1", "api-container", "api",
                                          "2026-08-18T10:30:00Z", 100, "2026-08-18")
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_ts"], "2026-08-18T10:30:00Z")
            self.assertEqual(row["last_line"], 100)
            self.assertEqual(row["file_date"], "2026-08-18")
        finally:
            conn.close()
        # 更新
        self.collector._update_scan_state("prod", "10.0.0.1", "api-container", "api",
                                          "2026-08-18T10:31:00Z", 200, "2026-08-18")
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertEqual(row["last_ts"], "2026-08-18T10:31:00Z")
            self.assertEqual(row["last_line"], 200)
            self.assertEqual(row["file_date"], "2026-08-18")
        finally:
            conn.close()

    def test_load_scan_states_structure(self):
        """#2 游标结构：last_ts + file_offset（文件级累计偏移）+ file_date（跨日重置依据）"""
        self.collector._update_scan_state("prod", "10.0.0.1", "c1", "s1",
                                          "2026-08-18T10:30:00Z", 1500, "2026-08-18")
        states = self.collector._load_scan_states("prod", "10.0.0.1")
        self.assertEqual(states["c1"]["last_ts"], "2026-08-18T10:30:00Z")
        self.assertEqual(states["c1"]["file_offset"], 1500)
        self.assertEqual(states["c1"]["file_date"], "2026-08-18")
        # 无记录容器 → 空状态（file_offset 默认 0）
        self.assertEqual(states.get("missing", {}).get("file_offset", 0), 0)

    def test_collect_container_output_line_offset_continuous(self):
        """#2 多轮采集：line_offset 从 file_offset 基准继续累加（文件级偏移连续）"""
        out1 = "2026-08-18T10:30:01.123456789Z INFO first\n"
        out2 = "2026-08-18T10:30:02.123456789Z ERROR second\n"
        self.collector._collect_container_output(
            "prod", 1, "10.0.0.1", "api", out1, "")
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT last_line, file_date FROM log_scan_state "
                "WHERE container='api'"
            ).fetchone()
            first_end = row["last_line"]
        finally:
            conn.close()
        # 第二轮带上第一轮累计偏移 → 新行偏移接续，且游标 +1μs
        self.collector._collect_container_output(
            "prod", 1, "10.0.0.1", "api", out2, "", first_end)
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT line_offset, content FROM log_entries ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)
        # 第一行 offset 0；第二行 offset = 第一行编码字节长
        self.assertEqual(rows[0]["line_offset"], 0)
        self.assertEqual(rows[1]["line_offset"], first_end)
        self.assertEqual(rows[1]["content"], "ERROR second")

    def test_index_log_lines_writes_fts(self):
        """FTS5 索引：写入 log_entries + log_fts"""
        path = os.path.join(self.log_dir, "test.log")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("line0\nline1\nline2\n", encoding="utf-8")
        entries = [
            (1787049045, "prod", "api", "line1", path, 1, 1),
            (1787049046, "prod", "api", "line2", path, 2, 1),
        ]
        self.collector._index_log_entries(entries)
        conn = dbm.get_conn(self.db)
        try:
            count = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
            self.assertEqual(count, 2)
            fts_count = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'line1'"
            ).fetchone()[0]
            self.assertEqual(fts_count, 1)
        finally:
            conn.close()

    # ---- 关键词扫描 ----

    def _make_kw_rule(self, metric="ERROR", enabled=1, exclude_keyword=""):
        return dbm.create_record(self.db, "alert_rules", dict(
            name="kw test", metric_type="log_keyword", metric=metric,
            exclude_keyword=exclude_keyword,
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P3",
            channels=["wecom"], escalation_interval=30, enabled=enabled,
            remark="", created_at=1, updated_at=1))

    def test_load_log_keyword_rules(self):
        """加载已启用 log_keyword 规则（过滤未启用）"""
        self._make_kw_rule("ERROR", enabled=1)
        self._make_kw_rule("FATAL", enabled=0)
        rules = self.collector._load_log_keyword_rules()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["metric"], "ERROR")

    def test_scan_keywords_triggers_alert(self):
        """关键词命中 → 调用 trigger_log_alert"""
        self._make_kw_rule("ERROR")
        entries = [
            (1787049045, "prod", "api", "INFO normal log", "/tmp/f", 0, 1),
            (1787049046, "prod", "api", "ERROR something broke", "/tmp/f", 10, 1),
        ]
        # 使用 mock 引擎验证调用
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_called_once()
        call_kwargs = mock_engine.trigger_log_alert.call_args
        self.assertEqual(call_kwargs[1]["matched_keyword"], "ERROR")

    def test_scan_keywords_no_match_no_trigger(self):
        """关键词未命中 → 不触发告警"""
        self._make_kw_rule("FATAL")
        entries = [
            (1787049045, "prod", "api", "INFO normal log", "/tmp/f", 0, 1),
            (1787049046, "prod", "api", "ERROR something broke", "/tmp/f", 10, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_not_called()

    def test_scan_keywords_multi_keyword_first_match(self):
        """多关键词规则: 命中第一个即触发"""
        self._make_kw_rule("FATAL,ERROR")
        entries = [
            (1787049045, "prod", "api", "ERROR early", "/tmp/f", 0, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_called_once()
        call_kwargs = mock_engine.trigger_log_alert.call_args
        self.assertEqual(call_kwargs[1]["matched_keyword"], "ERROR")

    def test_scan_keywords_no_rules_no_error(self):
        """无规则时不报错"""
        entries = [
            (1787049045, "prod", "api", "ERROR log", "/tmp/f", 0, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = []
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_not_called()

    def test_scan_keywords_engine_none_no_error(self):
        """告警引擎未启动时不报错"""
        self._make_kw_rule("ERROR")
        entries = [
            (1787049045, "prod", "api", "ERROR log", "/tmp/f", 0, 1),
        ]
        with patch("webapp.log_collector.get_engine", return_value=None):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            # 不应抛异常
            self.collector._scan_keywords_in_entries(entries, "prod", 1)

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

    def test_scan_keywords_exclude_suppresses_alert(self):
        """关键词命中但排除关键词也命中 → 不触发告警"""
        self._make_kw_rule("ERROR", exclude_keyword="heartbeat")
        entries = [
            (1787049045, "prod", "api", "ERROR heartbeat check", "/tmp/f", 0, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_not_called()

    def test_scan_keywords_exclude_no_match_still_triggers(self):
        """关键词命中且排除关键词未命中 → 正常触发告警"""
        self._make_kw_rule("ERROR", exclude_keyword="heartbeat")
        entries = [
            (1787049045, "prod", "api", "ERROR something broke", "/tmp/f", 0, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_called_once()

    def test_scan_keywords_exclude_continues_to_next_line(self):
        """第一行被排除后，继续扫描后续行，命中则触发（排除窗口外）"""
        self._make_kw_rule("ERROR", exclude_keyword="heartbeat")
        # 中间填充 6 条无关日志，使两条 ERROR 间隔超过上下文窗口 ±5
        padding = [(1787049046 + i, "prod", "api", "INFO normal log",
                    "/tmp/f", 10 + i * 50, 1) for i in range(6)]
        entries = [
            (1787049045, "prod", "api", "ERROR heartbeat check", "/tmp/f", 0, 1),
            *padding,
            (1787049052, "prod", "api", "ERROR real failure", "/tmp/f", 500, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_called_once()
        call_kwargs = mock_engine.trigger_log_alert.call_args[1]
        self.assertEqual(call_kwargs["matched_keyword"], "ERROR")

    def test_scan_keywords_exclude_context_window_stacktrace(self):
        """多行堆栈跟踪：排除词在首行，后续行也含关键词 → 上下文窗口内一并排除"""
        self._make_kw_rule("Exception", exclude_keyword="已被冻结")
        entries = [
            (1787049045, "prod", "auth",
             "cn.dev33.satoken.exception.NotLoginException: token 已被冻结",
             "/tmp/f", 0, 1),
            (1787049045, "prod", "auth",
             "\tat cn.dev33.satoken.exception.NotLoginException.newInstance(NotLoginException.java:134)",
             "/tmp/f", 50, 1),
        ]
        from unittest.mock import MagicMock, patch
        mock_engine = MagicMock()
        with patch("webapp.log_collector.get_engine", return_value=mock_engine):
            self.collector._current_kw_rules = self.collector._load_log_keyword_rules()
            self.collector._scan_keywords_in_entries(entries, "prod", 1)
        mock_engine.trigger_log_alert.assert_not_called()

    def test_fts_maintenance_orphan_cleanup(self):
        """每日维护：兜底清理超 archive_days 的孤儿日志行并保持 FTS 一致"""
        now = int(time.time())
        self.collector._index_log_entries([
            (now - 40 * 86400, "prod", "api", "ancient orphan line",
             "/tmp/old.log", 0, 1),
            (now, "prod", "api", "recent keep line",
             "/tmp/new.log", 0, 1),
        ])
        self.collector._fts_maintenance()
        conn = dbm.get_conn(self.db)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
            self.assertEqual(cnt, 1)
            hit_keep = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'recent'"
            ).fetchone()[0]
            hit_old = conn.execute(
                "SELECT COUNT(*) FROM log_fts WHERE log_fts MATCH 'ancient'"
            ).fetchone()[0]
            self.assertEqual(hit_keep, 1)
            self.assertEqual(hit_old, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
