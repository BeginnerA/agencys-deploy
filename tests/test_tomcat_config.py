import unittest
from deploy_strategies.tomcat_config import (context_filename, health_request_path,
                                            parse_war_config, validate_war_config,
                                            derive_war_unit)

VALID = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "artifact_pattern": "target/*.war",
    "jvm_opts": ["-Xms256m", "-Dfile.encoding=UTF-8"],
    "env_files": ["/etc/smartops/orders.env"],
    "readiness": {"path": "/health", "body_contains": "UP",
                  "timeout": 90, "interval": 3},
}

class WarConfigTest(unittest.TestCase):
    def test_minimal_valid(self):
        self.assertEqual(validate_war_config(VALID), [])
        cfg = parse_war_config(VALID)
        self.assertEqual(cfg["context_path"], "/orders")
        self.assertEqual(health_request_path(cfg), "/orders/health")
        self.assertEqual(cfg["unit_name"], "orders-war")

    def test_accepts_json_string_from_db_or_import(self):
        import json
        raw = json.dumps(VALID)
        self.assertEqual(validate_war_config(raw), [])
        self.assertEqual(parse_war_config(raw)["port"], 18080)

    def test_root_context(self):
        cfg = parse_war_config(dict(VALID, context_path="/"))
        self.assertEqual(cfg["context_path"], "")
        self.assertEqual(context_filename(""), "ROOT.xml")
        self.assertEqual(health_request_path(cfg), "/health")

    def test_nested_context(self):
        cfg = parse_war_config(dict(VALID, context_path="/team/orders/"))
        self.assertEqual(context_filename(cfg["context_path"]), "team#orders.xml")

    def test_rejects_paths_and_health_injection(self):
        bad = dict(VALID, app_home="/opt/../etc",
                   env_files=["/etc/x\nExecStart=/bin/sh"],
                   readiness={"path": "/health?token=1"})
        errors = "；".join(validate_war_config(bad))
        self.assertIn("app_home", errors)
        self.assertIn("env_files", errors)
        self.assertIn("readiness.path", errors)

    def test_rejects_unsafe_context_and_options(self):
        bad = dict(VALID, context_path="/a b", jvm_opts=["-Dcatalina.base=/tmp"])
        errors = "；".join(validate_war_config(bad))
        self.assertIn("context_path", errors)
        self.assertIn("jvm_opts", errors)

    def test_rejects_unit_and_port(self):
        bad = dict(VALID, unit_name="bad/unit", port=80)
        errors = "；".join(validate_war_config(bad))
        self.assertIn("unit_name", errors)
        self.assertIn("port", errors)

    def test_rejects_scalar_lists(self):
        # CLI/YAML 可能把数组写成标量；不能静默拆成字符逐个进 argv
        bad = dict(VALID, jvm_opts="-Xms256m", env_files="/etc/x.env")
        errors = "；".join(validate_war_config(bad))
        self.assertIn("jvm_opts 必须是数组", errors)
        self.assertIn("env_files 必须是数组", errors)

    def test_derive_unit_without_explicit_name(self):
        data = dict(VALID)
        data.pop("unit_name")
        cfg = parse_war_config(data)
        self.assertEqual(derive_war_unit(cfg, "orders"), "smartops-orders-war")

    def test_parse_never_raises_on_wrong_types(self):
        # 解析对任意 JSON 输入都必须收敛为配置字典，不能把 AttributeError 抛给校验
        raw = {"app_home": 123, "tomcat_home": ["/opt/tomcat"], "java_home": 1.5,
               "run_user": True, "unit_name": 42, "artifact_pattern": 7,
               "context_path": 9, "port": float("inf"), "stop_timeout": "abc",
               "readiness": {"path": 123, "body_contains": 5, "timeout": None}}
        cfg = parse_war_config(raw)
        self.assertEqual(cfg["unit_name"], "42")
        self.assertEqual(cfg["port"], 0)

    def test_rejects_non_string_app_home_and_unit_name(self):
        bad = dict(VALID, app_home=123, unit_name=123)
        errors = validate_war_config(bad)  # 不得抛异常
        self.assertTrue(errors)
        text = "；".join(errors)
        self.assertIn("app_home 必须是字符串", text)
        self.assertIn("unit_name 必须是字符串", text)
        # 非法 unit_name 不能被派生默认值静默顶替
        self.assertEqual(parse_war_config(bad)["unit_name"], "123")

    def test_rejects_non_string_readiness_fields(self):
        bad = dict(VALID, readiness={"path": 123, "body_contains": 5})
        text = "；".join(validate_war_config(bad))
        self.assertIn("readiness.path 必须是字符串", text)
        self.assertIn("readiness.body_contains 必须是字符串", text)

    def test_non_string_env_entry_does_not_raise(self):
        errors = validate_war_config(dict(VALID, env_files=[123]))
        self.assertIn("env_files 必须是安全的绝对路径数组", "；".join(errors))

    def test_rejects_root_run_user(self):
        # spec：run_user 必须是已存在的非 root 账号（UID≠0）
        for name in ("root", "ROOT", "Root"):
            self.assertEqual(validate_war_config(dict(VALID, run_user=name)),
                             ["run_user 必须是已存在的非 root 账号名"])

    def test_rejects_trailing_dotdot_in_app_home(self):
        # 尾部 ".." 不能借路径守卫绕过 app_home/tomcat_home 互斥判断
        bad = dict(VALID, app_home="/opt/apps/..", tomcat_home="/opt/tomcat")
        self.assertEqual(validate_war_config(bad),
                         ["app_home 必须是安全的绝对路径"])

    def test_rejects_trailing_dotdot_in_env_files(self):
        errors = validate_war_config(dict(VALID, env_files=["/etc/.."]))
        self.assertEqual(errors, ["env_files 必须是安全的绝对路径数组"])

    def test_rejects_control_chars_in_readiness_path(self):
        for path in ("/he\talth", "/he\x00alth"):
            errors = validate_war_config(dict(VALID, readiness={"path": path}))
            self.assertEqual(
                errors,
                ["readiness.path 必须是相对应用根的绝对路径，不能含查询、片段或穿越"])

    def test_rejects_non_string_context_path(self):
        raw = dict(VALID, context_path=123)
        self.assertEqual(validate_war_config(raw), ["context_path 必须是字符串"])
        # 解析侧仍收敛为字符串，不抛异常
        self.assertEqual(parse_war_config(raw)["context_path"], "/123")

    def test_rejects_java_home_inside_app_home(self):
        bad = dict(VALID, app_home="/opt/apps/orders",
                   java_home="/opt/apps/orders/jvm")
        self.assertEqual(validate_war_config(bad),
                         ["app_home 与 java_home 不得相同或互相包含"])

    def test_rejects_bool_timeouts(self):
        # True 不能当作 1 通过"正整数"检查
        self.assertEqual(validate_war_config(dict(VALID, stop_timeout=True)),
                         ["stop_timeout 必须是正整数"])
        self.assertEqual(validate_war_config(dict(VALID, start_timeout=True)),
                         ["start_timeout 必须是正整数"])

    def test_parse_and_validate_tolerate_none_and_broken_json(self):
        cfg = parse_war_config(None)
        self.assertEqual(cfg["context_path"], "")
        self.assertEqual(cfg["stop_timeout"], 60)
        for raw in (None, "", "{not json"):
            errors = validate_war_config(raw)  # 不得抛异常
            self.assertIsInstance(errors, list)
            self.assertIn("run_user 必须是已存在的非 root 账号名", errors)

    def test_parsed_lists_are_copies(self):
        opts = ["-Xmx256m"]
        files = ["/etc/smartops/orders.env"]
        cfg = parse_war_config(dict(VALID, jvm_opts=opts, env_files=files))
        opts.append("-Da=b")
        files.append("/etc/other.env")
        self.assertEqual(cfg["jvm_opts"], ["-Xmx256m"])
        self.assertEqual(cfg["env_files"], ["/etc/smartops/orders.env"])

    def test_rejects_root_home_dir(self):
        # parse 已把 "/" 归一为 ""，仍需按安全路径拒绝
        for field in ("app_home", "tomcat_home", "java_home"):
            self.assertEqual(validate_war_config(dict(VALID, **{field: "/"})),
                             [f"{field} 必须是安全的绝对路径"])

    def test_validate_skips_unit_check_when_derived(self):
        data = dict(VALID)
        data.pop("unit_name")
        self.assertEqual(validate_war_config(data), [])
