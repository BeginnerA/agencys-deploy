"""process_config 共享层：分派解析/校验、ExecStart 拼装与接管特征串、process_modes"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy_strategies import process_config as pc
from deploy_strategies.jar_config import parse_runtime_config as parse_jar


class ParseDispatchTest(unittest.TestCase):
    def test_go_minimal_defaults(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/goapp"}, "go_binary")
        self.assertEqual(cfg["app_home"], "/opt/goapp")
        self.assertEqual(cfg["readiness"]["type"], "process")
        self.assertEqual(cfg["readiness"]["path"], "")
        self.assertEqual(cfg["stop_timeout"], 60)
        self.assertEqual(cfg["start_timeout"], 120)

    def test_node_fields(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/napp", "start_file": "dist/server.js",
             "node_bin": "/usr/local/bin/node"}, "node_process")
        self.assertEqual(cfg["start_file"], "dist/server.js")
        self.assertEqual(cfg["node_bin"], "/usr/local/bin/node")

    def test_node_default_node_bin(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/napp"}, "node_process")
        self.assertEqual(cfg["node_bin"], "/usr/bin/node")

    def test_python_fields(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/papp", "start_file": "main.py",
             "python_version": "3.11"}, "python_process")
        self.assertEqual(cfg["requirements"], "requirements.txt")
        self.assertEqual(cfg["python_version"], "3.11")

    def test_jar_delegates_and_keeps_java_keys(self):
        raw = {"app_home": "/opt/a", "jvm_opts": "-Xmx1g"}
        cfg = pc.parse_runtime_config(raw, "jar_service")
        self.assertEqual(cfg["java_bin"], "/usr/bin/java")
        self.assertEqual(cfg, parse_jar(raw))

    def test_unknown_mode_returns_empty(self):
        self.assertEqual(pc.parse_runtime_config({"app_home": "/x"}, "container"), {})


class ValidateDispatchTest(unittest.TestCase):
    def test_go_ok(self):
        self.assertEqual(pc.validate_runtime_config(
            {"app_home": "/opt/goapp"}, "go_binary"), [])

    def test_app_home_required_per_mode(self):
        errs = pc.validate_runtime_config({}, "rust_binary")
        self.assertTrue(any("app_home" in e for e in errs))

    def test_start_file_rules(self):
        for bad in ("", "/abs/server.js", "../x.js", "a b.js"):
            errs = pc.validate_runtime_config(
                {"app_home": "/opt/n", "start_file": bad}, "node_process")
            self.assertTrue(any("start_file" in e for e in errs), bad)
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/n", "start_file": "dist/server.js"}, "node_process")
        self.assertEqual(errs, [])

    def test_python_version_format(self):
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/p", "start_file": "m.py", "python_version": "3"},
            "python_process")
        self.assertTrue(any("python_version" in e for e in errs))

    def test_jar_validation_still_enforced(self):
        errs = pc.validate_runtime_config({}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/a", "java_bin": "java"}, "jar_service")
        self.assertTrue(any("java_bin" in e for e in errs))

    def test_non_process_mode_no_validation(self):
        self.assertEqual(pc.validate_runtime_config({}, "container"), [])


class ExecStartTest(unittest.TestCase):
    def test_jar(self):
        cfg = parse_jar({"app_home": "/opt/a", "java_bin": "/usr/bin/java",
                         "jvm_opts": "-Xmx1g", "app_args": "--p=1"})
        self.assertEqual(pc.build_exec_start(cfg, "jar_service"),
                         "/usr/bin/java -Xmx1g -jar /opt/a/CURRENT/app.jar --p=1")
        self.assertEqual(pc.exec_start_marker(cfg, "jar_service"),
                         "/opt/a/CURRENT/app.jar")

    def test_go_rust(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/g", "app_args": "--port=9"}, "go_binary")
        self.assertEqual(pc.build_exec_start(cfg, "go_binary"),
                         "/opt/g/CURRENT/app --port=9")
        self.assertEqual(pc.exec_start_marker(cfg, "rust_binary"),
                         "/opt/g/CURRENT/app")

    def test_node(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/n", "start_file": "dist/server.js"}, "node_process")
        self.assertEqual(pc.build_exec_start(cfg, "node_process"),
                         "/usr/bin/node /opt/n/CURRENT/dist/server.js")
        self.assertEqual(pc.exec_start_marker(cfg, "node_process"),
                         "/opt/n/CURRENT/dist/server.js")

    def test_python(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/p", "start_file": "main.py"}, "python_process")
        self.assertEqual(pc.build_exec_start(cfg, "python_process"),
                         "/opt/p/venv/bin/python /opt/p/CURRENT/main.py")
        self.assertEqual(pc.exec_start_marker(cfg, "python_process"),
                         "/opt/p/venv/bin/python")

    def test_no_args(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        self.assertEqual(pc.build_exec_start(cfg, "go_binary"), "/opt/g/CURRENT/app")


class ProcessModesTest(unittest.TestCase):
    def test_registry_collects_process_family(self):
        from deploy_strategies.base import process_modes
        modes = process_modes()
        self.assertIn("jar_service", modes)
        self.assertNotIn("container", modes)
        self.assertNotIn("static_archive", modes)


if __name__ == "__main__":
    unittest.main()
