"""journalctl 采集纯函数：游标、命令构造、JSON → 时间戳前缀行"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.log_collector import (LogCollector, _archive_and_cleanup_files,
                                  _build_journal_cmd, _journal_lines,
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

    def test_entry_exactly_at_cursor_is_kept(self):
        # _cursor_from_ts 已 +1μs，恰好等于 min_us 的条目是新日志而非重复
        raw = self._entry(1787049002123456, "boundary")
        self.assertEqual(
            _journal_lines(raw, min_us=1787049002123456),
            ["2026-08-18T10:30:02.123456Z boundary\n"])

    def test_non_object_json_lines_are_skipped(self):
        # null/数组/数字等合法 JSON 但非对象：跳过该行，不得中断整批
        raw = "\n".join([
            "null",
            "[1, 2]",
            "123",
            self._entry(1787049002123456, "after-bad"),
        ])
        self.assertEqual(_journal_lines(raw),
                         ["2026-08-18T10:30:02.123456Z after-bad\n"])


class CollectorJournalTest(unittest.TestCase):
    """采集器接入 journalctl：单主机批量命令 + 游标去重 + 目录/服务键映射"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="log_journal_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        env = dbm.create_record(self.db, "environments", dict(
            project_id=pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}",
            ssh='{"user":"root","password":"pw"}',
            build="{}", deploy="{}", monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=pid, name="auth", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="9.9.9.9", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, env, srv)
        self.ssh_calls = []

        class _FakeMonitor:
            _server_containers = {
                "9.9.9.9": {"containers": [], "ts": time.time()}}

        self.output = "\n".join([
            "@@LOGCUT:deadbeef:journal:auth",
            json.dumps({"__REALTIME_TIMESTAMP": "1787049001123456",
                        "MESSAGE": "Started auth"}),
            json.dumps({"__REALTIME_TIMESTAMP": "1787049002123456",
                        "MESSAGE": "line one\nline two"}),
        ]) + "\n"
        self.collector = LogCollector(self.db, self.log_dir, interval=30,
                                      ssh_func=self._fake_ssh,
                                      monitor=_FakeMonitor())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        self.ssh_calls.append(cmd)
        return self.output

    def test_journal_batch_is_collected_and_indexed_once(self):
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        self.assertEqual(len(self.ssh_calls), 1)
        cmd = self.ssh_calls[0]
        self.assertIn("journalctl -u auth", cmd)
        self.assertIn("--since='@", cmd)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, "collect", "prod", "auth",
                            f"{today}.log")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("Started auth", content)
        self.assertIn("line one", content)
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT service, content FROM log_entries ORDER BY id"
            ).fetchall()
            st = conn.execute(
                "SELECT container, service, last_ts FROM log_scan_state"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([r["service"] for r in rows], ["auth"] * 3)
        self.assertEqual(len(st), 1)
        self.assertEqual(st[0]["container"], "systemd:auth")
        self.assertEqual(st[0]["service"], "auth")
        self.assertEqual(st[0]["last_ts"], "2026-08-18T10:30:02.123457+00:00")

        # 第二轮相同输出：游标严格过滤 → 不重复入库
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        conn = dbm.get_conn(self.db)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM log_entries").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 3)


class JournalArchiveCleanupTest(unittest.TestCase):
    """归档清理按 file_path 匹配：unit 与 service 显示名不同也要删掉索引行"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="log_journal_cleanup_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.log_dir = os.path.join(self.tmp, "logs")
        pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        env = dbm.create_record(self.db, "environments", dict(
            project_id=pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}",
            ssh='{"user":"root","password":"pw"}',
            build="{}", deploy="{}", monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=pid, name="auth-svc", image_name="", service_name="",
            deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth-svc", "unit_name": "auth",
                            "port": 8080},
            docker_context="", dockerfile="", build_order="[]",
            nacos_groups="[]", dubbo_services="[]", snailjob_group="",
            health_url="", health_port=0, log_error_keywords="[]",
            clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="9.9.9.9", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, env, srv)
        self.ssh_calls = []

        class _FakeMonitor:
            _server_containers = {
                "9.9.9.9": {"containers": [], "ts": time.time()}}

        self.output = "\n".join([
            "@@LOGCUT:deadbeef:journal:auth",
            json.dumps({"__REALTIME_TIMESTAMP": "1787049001123456",
                        "MESSAGE": "Started auth-svc"}),
        ]) + "\n"
        self.collector = LogCollector(self.db, self.log_dir, interval=30,
                                      ssh_func=self._fake_ssh,
                                      monitor=_FakeMonitor())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_ssh(self, host, port, ssh_cfg, cmd, timeout=10):
        self.ssh_calls.append(cmd)
        return self.output

    def test_archive_cleanup_matches_journal_rows_when_unit_differs(self):
        # 服务显示名 auth-svc，systemd 单元 auth → 文件在 collect/prod/auth/，
        # log_entries.service 为 auth-svc
        with mock.patch("webapp.log_collector.secrets.token_hex",
                        return_value="deadbeef"):
            self.collector._collect_round()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = os.path.join(self.log_dir, "collect", "prod", "auth",
                            f"{today}.log")
        self.assertTrue(os.path.isfile(path))
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT service, file_path FROM log_entries").fetchall()
        finally:
            conn.close()
        self.assertEqual([(r["service"], r["file_path"]) for r in rows],
                         [("auth-svc", path)])

        # retention=0 → 今天日期的文件也过期（确定性，无需 sleep）
        dbm.create_record(self.db, "platform_settings", dict(
            key="log_retention_days", value=json.dumps(0),
            updated_at=int(time.time())))
        result = _archive_and_cleanup_files(self.db, self.log_dir)

        self.assertEqual(result, {"archived": 1, "purged": 0})
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isfile(os.path.join(
            self.log_dir, "archive", "prod", "auth", f"{today}.log.gz")))
        conn = dbm.get_conn(self.db)
        try:
            entries = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
            fts = conn.execute("SELECT COUNT(*) FROM log_fts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(entries, 0)
        self.assertEqual(fts, 0)


if __name__ == "__main__":
    unittest.main()
