"""日志采集器单元测试"""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from webapp import db as dbm
from webapp.log_collector import LogCollector, _parse_docker_log_line, _build_log_file_path


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
                                          "2026-08-18T10:30:00Z", 100)
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["last_ts"], "2026-08-18T10:30:00Z")
            self.assertEqual(row["last_line"], 100)
        finally:
            conn.close()
        # 更新
        self.collector._update_scan_state("prod", "10.0.0.1", "api-container", "api",
                                          "2026-08-18T10:31:00Z", 200)
        conn = dbm.get_conn(self.db)
        try:
            row = conn.execute(
                "SELECT * FROM log_scan_state WHERE env='prod' AND server='10.0.0.1'"
            ).fetchone()
            self.assertEqual(row["last_ts"], "2026-08-18T10:31:00Z")
            self.assertEqual(row["last_line"], 200)
        finally:
            conn.close()

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

    def _make_kw_rule(self, metric="ERROR", enabled=1):
        return dbm.create_record(self.db, "alert_rules", dict(
            name="kw test", metric_type="log_keyword", metric=metric,
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


if __name__ == "__main__":
    unittest.main()
