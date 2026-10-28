"""systemd 单元生成与接入：只写 unit + daemon-reload + enable"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import service_unit
from deploy_strategies.process_config import parse_runtime_config


class FakeRemote:
    def __init__(self, rules=None, try_exec_ok=None):
        self.exec_calls = []
        self.rules = rules or {}
        self.try_exec_ok = try_exec_ok or {}

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.exec_calls.append(cmd)
        for key, val in self.rules.items():
            if key in cmd:
                if isinstance(val, Exception):
                    raise val
                return val
        return ""

    def try_exec(self, cmd, timeout=60):
        self.exec_calls.append(cmd)
        return self.try_exec_ok.get("systemctl show", True)


CFG = {
    "app_home": "/opt/auth", "java_bin": "/usr/bin/java",
    "jvm_opts": "-Xms512m -Xmx1g", "app_args": "--spring.profiles.active=prod",
    "run_user": "appuser", "work_dir": "", "unit_name": "hqb-auth",
    "stop_timeout": 45, "start_timeout": 180,
    "env_files": ["/opt/auth/.env"],
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
    "port": 8080, "artifact_pattern": "target/*.jar",
}


class RenderTest(unittest.TestCase):
    def test_core_directives(self):
        content = service_unit.render_unit(CFG, "hqb-auth")
        self.assertIn("[Unit]", content)
        self.assertIn("User=appuser", content)
        self.assertIn("WorkingDirectory=/opt/auth/CURRENT", content)
        self.assertIn(
            "ExecStart=/usr/bin/java -Xms512m -Xmx1g -jar /opt/auth/CURRENT/app.jar "
            "--spring.profiles.active=prod", content)
        self.assertIn("SuccessExitStatus=143", content)
        self.assertIn("TimeoutStopSec=45", content)
        self.assertIn("TimeoutStartSec=180", content)
        self.assertIn("EnvironmentFile=/opt/auth/.env", content)
        self.assertIn("WantedBy=multi-user.target", content)

    def test_custom_work_dir(self):
        cfg = dict(CFG, work_dir="/srv/work")
        self.assertIn("WorkingDirectory=/srv/work", service_unit.render_unit(cfg, "a"))

    def test_no_run_user_means_no_directive(self):
        cfg = dict(CFG, run_user="")
        self.assertNotIn("User=", service_unit.render_unit(cfg, "a"))


class OnboardTest(unittest.TestCase):
    def test_writes_reloads_and_enables_only(self):
        server = FakeRemote(try_exec_ok={"systemctl show": True})
        with mock.patch("service_unit.log"):
            service_unit.onboard_unit(
                server, "hqb-auth", "[Unit]\n",
                parse_runtime_config({"app_home": "/opt/auth"}, "jar_service"))
        joined = "\n".join(server.exec_calls)
        self.assertIn("/etc/systemd/system/hqb-auth.service", joined)
        self.assertIn("systemctl daemon-reload", joined)
        self.assertIn("systemctl enable hqb-auth.service", joined)
        self.assertNotIn("systemctl start", joined)
        self.assertNotIn("apt", joined)
        self.assertNotIn("java -version", joined)

    def test_refuses_foreign_existing_unit(self):
        server = FakeRemote(rules={
            "systemctl show": "ExecStart={ path=/usr/bin/java ; argv[]=java -jar /srv/x.jar }"})
        with mock.patch("service_unit.log"):
            with self.assertRaises(RuntimeError) as ctx:
                service_unit.onboard_unit(
                    server, "hqb-auth", "[Unit]\n",
                    parse_runtime_config({"app_home": "/opt/auth"}, "jar_service"))
        self.assertIn("拒绝接管", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
