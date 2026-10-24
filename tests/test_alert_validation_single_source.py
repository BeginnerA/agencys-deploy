"""校验口径单一来源（P2-6 质量门禁）

背景：同一套规则校验存在两份镜像——
  - `webapp/routes/alert_routes._validate_rule`（Web API）
  - `agent/tools._validate_alert_rule`（Agent 工具，注释自陈「镜像 alert_routes」）
窗口 scope 校验同样有 `deploy_enhance_routes._validate_scope` 与
`agent/tools._validate_window_scope` 两份。
镜像实现的代价是漂移：一侧改了阈值/枚举，另一侧静默保持旧口径，
同一份数据经不同入口会得出不同结论。

本文件锁定：三个入口只委托 `webapp/alert_spec.py` 的一份实现，
入口侧仅保留各自的附加校验（Agent 侧的 scope id 存在性检查）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import alert_spec
from webapp.routes import alert_routes
from webapp.routes import deploy_enhance_routes
from agent import tools as tools_mod


class SharedImplementationTest(unittest.TestCase):
    """入口不得自带第二份规则校验"""

    def test_alert_rule_entrypoints_delegate_to_shared_spec(self):
        sentinel = "shared-verdict"
        body = {"name": "CPU", "metric_type": "metric", "metric": "cpu"}
        with mock.patch.object(alert_spec, "validate_alert_rule",
                               return_value=sentinel) as spy:
            web_result = alert_routes._validate_rule(body)
            agent_result = tools_mod._validate_alert_rule(None, body)
        self.assertEqual(web_result, sentinel)
        self.assertEqual(agent_result, sentinel)
        # 两个入口必须调用同一个函数对象（各自复制一份即失败）
        self.assertEqual(spy.call_count, 2)

    def test_scope_validation_is_one_function(self):
        self.assertIs(deploy_enhance_routes._validate_scope, alert_spec.validate_scope)
        self.assertIs(tools_mod._validate_window_scope, alert_spec.validate_scope)


class SilenceSharedImplementationTest(unittest.TestCase):
    """静默窗口校验同样只允许一份实现（Web 与 Agent 过去各写了一遍）"""

    def _silence(self, **kw):
        base = dict(name="夜间静默", type="one_time", start_at=1000, end_at=2000,
                    scope={"rules": [], "envs": [], "servers": []})
        base.update(kw)
        return base

    def test_silence_entrypoints_delegate_to_shared_spec(self):
        sentinel = "shared-silence-verdict"
        body = self._silence()
        with mock.patch.object(alert_spec, "validate_silence",
                               return_value=sentinel) as spy:
            web_result = alert_routes._validate_silence(body)
            agent_result = tools_mod._validate_silence(None, body)
        self.assertEqual(web_result, sentinel)
        self.assertEqual(agent_result, sentinel)
        # Agent 侧在共享口径报错时不得继续跑自己的检查
        self.assertEqual(spy.call_count, 2)

    def test_valid_silence_passes(self):
        self.assertEqual(alert_spec.validate_silence(self._silence()), "")
        self.assertEqual(alert_spec.validate_silence(
            self._silence(type="recurring", start_at="01:30", end_at="02:45",
                          weekdays=[1, 2, 3])), "")

    def test_name_and_type(self):
        self.assertIn("名称", alert_spec.validate_silence(self._silence(name="  ")))
        self.assertIn("type", alert_spec.validate_silence(self._silence(type="weekly")))

    def test_one_time_needs_epoch(self):
        err = alert_spec.validate_silence(self._silence(start_at="01:30"))
        self.assertIn("start_at", err)

    def test_recurring_needs_hhmm(self):
        bad = self._silence(type="recurring", start_at="24:00", end_at="02:00")
        self.assertIn("HH:MM", alert_spec.validate_silence(bad))
        ok = self._silence(type="recurring", start_at="9:05", end_at="10:00")
        self.assertEqual(alert_spec.validate_silence(ok), "")

    def test_scope_structure_and_weekdays(self):
        self.assertIn("scope", alert_spec.validate_silence(self._silence(scope={"rules": []})))
        self.assertIn("scope", alert_spec.validate_silence(
            self._silence(scope={"rules": 1, "envs": [], "servers": []})))
        self.assertIn("weekdays", alert_spec.validate_silence(self._silence(weekdays="1,2")))

    def test_partial_skips_absent_fields(self):
        """局部更新只校验请求体里出现的字段（update 端点用）"""
        self.assertEqual(alert_spec.validate_silence({"enabled": False}, partial=True), "")
        self.assertIn("HH:MM", alert_spec.validate_silence(
            {"type": "recurring", "start_at": "25:00"}, partial=True))


class SpecBehaviourTest(unittest.TestCase):
    """共享实现本身的行为口径（原两份镜像的并集，取更明确的那套文案）"""

    def _rule(self, **kw):
        # scope 必填（完整校验下缺省即 "scope type 非法"，与 Web 表单一致：前端总是提交作用域）
        base = dict(name="CPU 过高", metric_type="metric", metric="cpu",
                    compare=">", threshold=80, severity="P2",
                    channels=["wecom"], scope={"type": "all", "ids": []})
        base.update(kw)
        return base

    def test_valid_rule_passes(self):
        self.assertEqual(alert_spec.validate_alert_rule(self._rule()), "")

    def test_metric_type_enums(self):
        self.assertIn("metric_type", alert_spec.validate_alert_rule(
            self._rule(metric_type="bogus")))
        self.assertIn("metric", alert_spec.validate_alert_rule(
            self._rule(metric_type="status", metric="cpu")))
        self.assertIn("metric", alert_spec.validate_alert_rule(
            self._rule(metric="disk_usage")))

    def test_log_keyword_rules(self):
        self.assertIn("关键词", alert_spec.validate_alert_rule(
            self._rule(metric_type="log_keyword", metric="  ")))
        self.assertIn("100", alert_spec.validate_alert_rule(
            self._rule(metric_type="log_keyword", metric="x" * 101)))
        self.assertIn("100", alert_spec.validate_alert_rule(
            self._rule(exclude_keyword="y" * 101)))

    def test_metric_duration_window_cap(self):
        err = alert_spec.validate_alert_rule(self._rule(duration_seconds=1201))
        self.assertIn("1200", err)
        # 非 metric 型不受滑动窗口上限约束
        self.assertEqual(alert_spec.validate_alert_rule(
            self._rule(metric_type="status", metric="service_down",
                       duration_seconds=3600)), "")

    def test_cross_field_scope_constraints(self):
        self.assertIn("服务作用域", alert_spec.validate_alert_rule(
            self._rule(scope={"type": "service", "ids": [1]})))
        self.assertIn("服务器作用域", alert_spec.validate_alert_rule(
            self._rule(metric_type="status", metric="service_down",
                       scope={"type": "server", "ids": [1]})))

    def test_partial_skips_absent_fields(self):
        body = {"threshold": "abc"}
        self.assertIn("threshold", alert_spec.validate_alert_rule(body, partial=True))
        # 非 partial 时 name 等必填字段仍被要求
        self.assertIn("名称", alert_spec.validate_alert_rule(body))

    def test_validate_scope_structure(self):
        self.assertEqual(alert_spec.validate_scope({"type": "env", "ids": []}), "")
        self.assertIn("type", alert_spec.validate_scope({"type": "zone", "ids": []}))
        self.assertIn("ids", alert_spec.validate_scope({"type": "env"}))
        self.assertIn("env_ids", alert_spec.validate_scope(
            {"type": "env", "ids": [], "env_ids": "1"}))
        self.assertIn("格式", alert_spec.validate_scope(None))

    def test_severity_and_channel_enums(self):
        self.assertIn("severity", alert_spec.validate_alert_rule(self._rule(severity="P9")))
        self.assertIn("channels", alert_spec.validate_alert_rule(
            self._rule(channels=["pager"])))

    def test_constants_have_single_source_for_channels(self):
        """渠道枚举与通知实现同源（新增渠道只需改 alert_channels）"""
        from webapp import alert_channels
        self.assertEqual(tuple(alert_spec.CHANNELS), tuple(alert_channels.CHANNELS))


if __name__ == "__main__":
    unittest.main()
