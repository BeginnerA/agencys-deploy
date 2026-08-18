"""通知渠道测试：配置解析/环境变量覆盖/payload 构造/失败重试（mock 网络）"""
import os
import tempfile
import unittest
from unittest import mock

from webapp import db as dbm
from webapp import alert_channels as ac


class TestChannels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.alert = dict(
            id=1, severity="P1", subject="服务下线: hqb-boot-system",
            description="服务 hqb-boot-system 在环境 test 未部署",
            target_info={"env": "test", "service": "hqb-boot-system"},
            first_seen_at=1700000000)

    def test_channel_config_env_override(self):
        os.environ["ALERT_SMTP_HOST"] = "smtp.example.com"
        try:
            cfg = ac.get_channel_config(self.db, "email")
            self.assertEqual(cfg["host"], "smtp.example.com")
        finally:
            del os.environ["ALERT_SMTP_HOST"]

    def test_format_alert_text(self):
        text = ac.format_alert_text(self.alert)
        self.assertIn("【P1】", text)
        self.assertIn("hqb-boot-system", text)

    def test_wecom_payload(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"errcode": 0}
            ac._send_wecom({"webhook": "https://qyapi.weixin.qq.com/hook"}, "text", {})
            args, kwargs = m.call_args
            self.assertEqual(kwargs["json"]["msgtype"], "text")
            self.assertEqual(kwargs["json"]["text"]["content"], "text")

    def test_wecom_error_raises(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"errcode": 93000}
            with self.assertRaises(RuntimeError):
                ac._send_wecom({"webhook": "https://x"}, "text", {})

    def test_aliyun_sms_payload_and_sign(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"Code": "OK"}
            ac._send_aliyun_sms(
                {"access_key": "ak", "secret": "sk", "sign": "S",
                 "template": "T1", "phones": "13800000000"}, "告警", self.alert)
            args, kwargs = m.call_args
            self.assertEqual(args[0], "https://dysmsapi.aliyuncs.com/")
            self.assertIn("AccessKeyId", kwargs["data"])
            self.assertIn("Signature", kwargs["data"])
            self.assertIn("TemplateParam", kwargs["data"])

    def test_send_alert_retry_then_success(self):
        """第一次失败第二次成功：重试生效且结果记录 ok"""
        with mock.patch.object(ac, "_send_wecom") as sw:
            sw.side_effect = [RuntimeError("boom"), None]
            results = ac.send_alert(self.db, self.alert, ["wecom"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1])
        self.assertEqual(sw.call_count, 2)
        evs = dbm.list_records(self.db, "alert_events")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["ok"], 1)

    def test_send_alert_all_fail_records_fail(self):
        with mock.patch.object(ac, "_send_wecom",
                               side_effect=RuntimeError("boom")):
            results = ac.send_alert(self.db, self.alert, ["wecom"])
        self.assertFalse(results[0][1])
        evs = dbm.list_records(self.db, "alert_events")
        self.assertEqual(evs[0]["ok"], 0)


if __name__ == "__main__":
    unittest.main()
