"""jar_service 配置契约：解析兜底、校验规则、产物命名与校验和"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy_strategies.base import registered_modes
from deploy_strategies.jar_config import (
    artifact_filename, derive_unit_name, parse_runtime_config, sha256_file,
    validate_runtime_config)


class ParseTest(unittest.TestCase):
    def test_defaults(self):
        cfg = parse_runtime_config({})
        self.assertEqual(cfg["java_bin"], "/usr/bin/java")
        self.assertEqual(cfg["readiness"]["type"], "http")
        self.assertEqual(cfg["readiness"]["path"], "/actuator/health")
        self.assertEqual(cfg["readiness"]["timeout"], 90)
        self.assertEqual(cfg["readiness"]["interval"], 5)
        self.assertEqual(cfg["stop_timeout"], 60)
        self.assertEqual(cfg["start_timeout"], 120)
        self.assertEqual(cfg["artifact_pattern"], "target/*.jar")
        self.assertEqual(cfg["env_files"], [])

    def test_accepts_json_string_and_dict(self):
        raw = {"app_home": "/opt/a/", "port": 8080}
        self.assertEqual(parse_runtime_config(raw)["app_home"], "/opt/a")
        self.assertEqual(parse_runtime_config(json.dumps(raw))["port"], 8080)

    def test_broken_json_falls_back_to_empty(self):
        self.assertEqual(parse_runtime_config("{not json")["app_home"], "")


class ValidateTest(unittest.TestCase):
    def test_non_jar_mode_is_not_validated(self):
        self.assertEqual(validate_runtime_config({}, "container"), [])

    def test_valid_config(self):
        cfg = {"app_home": "/opt/a", "port": 8080,
               "readiness": {"type": "http", "timeout": 30, "interval": 2}}
        self.assertEqual(validate_runtime_config(cfg, "jar_service"), [])

    def test_app_home_required_and_absolute(self):
        errs = validate_runtime_config({"port": 8080}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))
        errs = validate_runtime_config({"app_home": "opt/a", "port": 8080}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))

    def test_port_required_for_network_readiness(self):
        errs = validate_runtime_config({"app_home": "/opt/a"}, "jar_service")
        self.assertTrue(any("port" in e for e in errs))

    def test_process_readiness_needs_no_port(self):
        cfg = {"app_home": "/opt/a", "readiness": {"type": "process"}}
        self.assertEqual(validate_runtime_config(cfg, "jar_service"), [])

    def test_bad_unit_name_rejected(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "unit_name": "bad name;rm"}
        self.assertTrue(any("unit_name" in e for e in validate_runtime_config(cfg, "jar_service")))

    def test_bad_timeouts_rejected(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "stop_timeout": 0}
        self.assertTrue(any("stop_timeout" in e for e in validate_runtime_config(cfg, "jar_service")))

    def test_env_files_must_be_absolute(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "env_files": ["rel.env"]}
        self.assertTrue(any("env_files" in e for e in validate_runtime_config(cfg, "jar_service")))


class ArtifactTest(unittest.TestCase):
    def test_name(self):
        self.assertEqual(artifact_filename("hqb-auth", "1.0.31"), "hqb-auth-1.0.31.jar")

    def test_sha256_file(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / "a.jar"
            p.write_bytes(b"abc")
            self.assertEqual(
                sha256_file(p),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DeriveUnitNameTest(unittest.TestCase):
    def test_priority(self):
        self.assertEqual(derive_unit_name(
            {"unit_name": "hqb-auth"}, "img", "svc"), "hqb-auth")
        self.assertEqual(derive_unit_name({"unit_name": ""}, "img", "svc"), "img")
        self.assertEqual(derive_unit_name({}, "", "svc"), "svc")
        self.assertEqual(derive_unit_name({}, "", ""), "app")

    def test_accepts_json_string(self):
        self.assertEqual(derive_unit_name('{"unit_name": "u1"}', "img"), "u1")
        self.assertEqual(derive_unit_name("{bad json", "img"), "img")


class RegistryTest(unittest.TestCase):
    def test_registered_modes_is_public(self):
        modes = registered_modes()
        self.assertIn("container", modes)
        self.assertIn("static_archive", modes)


if __name__ == "__main__":
    unittest.main()
