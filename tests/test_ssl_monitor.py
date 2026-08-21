"""SSL 监控线程测试：验证探测循环和数据库更新"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from webapp import db as dbm
from webapp.ssl_monitor import SSLMonitor


class TestSSLMonitor(unittest.TestCase):
    """SSLMonitor 守护线程：单轮探测更新 custom_monitors 记录"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.now = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_ssl_domain(self, target, port=443, **kw):
        fields = dict(type="ssl", name=f"test-{target}", target=target, port=port,
                      enabled=1, created_at=self.now, updated_at=self.now)
        fields.update(kw)
        return dbm.create_record(self.db, "custom_monitors", fields)

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_probe_updates_record(self, mock_probe):
        """探测成功：更新 last_checked, days_left, detail"""
        mock_probe.return_value = {
            "cert_subject": "CN=api.example.com",
            "cert_issuer": "CN=Let's Encrypt",
            "cert_expiry": self.now + 86400 * 60,
            "days_left": 60,
            "error": None,
        }
        mid = self._add_ssl_domain("api.example.com")
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        row = dbm.get_record(self.db, "custom_monitors", mid)
        self.assertEqual(row["days_left"], 60)
        self.assertIsNotNone(row["last_checked"])
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
        self.assertEqual(detail["cert_subject"], "CN=api.example.com")
        self.assertEqual(row["error"], "")

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_probe_failure_preserves_old_data(self, mock_probe):
        """探测失败：保留旧 days_left，记录 error"""
        mock_probe.return_value = {
            "cert_subject": None, "cert_issuer": None,
            "cert_expiry": None, "days_left": None,
            "error": "Connection timed out",
        }
        mid = self._add_ssl_domain("bad.example.com",
                                   days_left=30, last_checked=self.now - 3600)
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        row = dbm.get_record(self.db, "custom_monitors", mid)
        # days_left 保留旧值
        self.assertEqual(row["days_left"], 30)
        # error 记录本次失败
        self.assertEqual(row["error"], "Connection timed out")

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_skips_disabled(self, mock_probe):
        """禁用的监控项不探测"""
        self._add_ssl_domain("disabled.example.com", enabled=0)
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        mock_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
