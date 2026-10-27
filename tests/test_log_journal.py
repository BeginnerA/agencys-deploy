"""journalctl 采集纯函数：游标、命令构造、JSON → 时间戳前缀行"""
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp.log_collector import (_build_journal_cmd, _journal_lines,
                                  _journal_since)


class JournalSinceTest(unittest.TestCase):
    def test_iso_cursor_is_converted_to_epoch(self):
        cursor = "2026-08-18T10:30:01.123457+00:00"
        since, min_us = _journal_since(cursor)
        expected_us = int(datetime.fromisoformat(cursor).timestamp() * 1_000_000)
        self.assertEqual(min_us, expected_us)
        self.assertEqual(since, f"@{expected_us // 1_000_000 - 1}")

    def test_invalid_cursor_falls_back_to_five_minutes(self):
        since, min_us = _journal_since("not-a-time", now=1000)
        self.assertEqual((since, min_us), ("@700", 0))

    def test_empty_cursor_uses_server_side_now(self):
        since, min_us = _journal_since("", now=1000)
        self.assertEqual((since, min_us), ("@700", 0))


class JournalCmdTest(unittest.TestCase):
    def test_command_quotes_unit_and_limits_entries(self):
        cmd = _build_journal_cmd("hqb-auth", "@123", 500)
        self.assertIn("journalctl -u hqb-auth", cmd)
        self.assertIn("--since='@123'", cmd)
        self.assertIn("-o json", cmd)
        self.assertIn("--no-pager", cmd)
        self.assertIn("-n 500", cmd)

    def test_malicious_unit_is_quoted(self):
        cmd = _build_journal_cmd("a;rm -rf /", "@1", 1)
        self.assertIn("'a;rm -rf /'", cmd)


class JournalLinesTest(unittest.TestCase):
    def _entry(self, ts_us, msg):
        return json.dumps({"__REALTIME_TIMESTAMP": str(ts_us), "MESSAGE": msg})

    def test_filters_at_cursor_and_formats(self):
        raw = "\n".join([
            self._entry(1787049001123456, "old"),
            self._entry(1787049002123456, "new"),
        ])
        lines = _journal_lines(raw, min_us=1787049001123457)
        self.assertEqual(lines, ["2026-08-18T10:30:02.123456Z new\n"])

    def test_multiline_message_gets_prefix_per_line(self):
        raw = self._entry(1787049002123456, "line one\nline two")
        lines = _journal_lines(raw)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("Z line one\n"))
        self.assertTrue(lines[1].endswith("Z line two\n"))

    def test_byte_array_and_bad_json(self):
        raw = "\n".join([
            json.dumps({"__REALTIME_TIMESTAMP": 1787049002123456,
                        "MESSAGE": [104, 105]}),
            "{not json",
            json.dumps({"MESSAGE": "no timestamp"}),
            json.dumps({"__REALTIME_TIMESTAMP": 1787049003123456,
                        "MESSAGE": ""}),
        ])
        self.assertEqual(_journal_lines(raw),
                         ["2026-08-18T10:30:02.123456Z hi\n"])


if __name__ == "__main__":
    unittest.main()
