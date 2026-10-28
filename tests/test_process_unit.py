"""进程族 unit 渲染与接入门槛：go/node/python 形态 + 接管特征串按模式分派"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import service_unit
from deploy_strategies.process_config import parse_runtime_config


class _Server:
    def __init__(self, existing=""):
        self.commands = []
        self._existing = existing

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.commands.append(cmd)
        return self._existing

    def try_exec(self, cmd, timeout=60):
        self.commands.append(cmd)
        return True


class RenderUnitTest(unittest.TestCase):
    def test_jar_default_mode_unchanged(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/a", "java_bin": "/usr/bin/java"}, "asvc")
        self.assertIn("ExecStart=/usr/bin/java -jar /opt/a/CURRENT/app.jar\n", content)
        self.assertIn("SuccessExitStatus=143", content)

    def test_go_exec_start(self):
        content = service_unit.render_unit({"app_home": "/opt/g"}, "gsvc", "go_binary")
        self.assertIn("ExecStart=/opt/g/CURRENT/app\n", content)
        self.assertIn("SuccessExitStatus=143", content)

    def test_node_exec_start(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/n", "node_bin": "/usr/bin/node",
             "start_file": "dist/server.js"}, "nsvc", "node_process")
        self.assertIn("ExecStart=/usr/bin/node /opt/n/CURRENT/dist/server.js\n", content)

    def test_python_exec_start_uses_venv(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/p", "start_file": "main.py"}, "psvc", "python_process")
        self.assertIn("ExecStart=/opt/p/venv/bin/python /opt/p/CURRENT/main.py\n",
                      content)


class OnboardMarkerTest(unittest.TestCase):
    def test_rejects_foreign_unit_per_mode(self):
        cfg = parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        server = _Server(existing="ExecStart={ path=/bin/other }")
        with self.assertRaises(RuntimeError) as ctx:
            service_unit.onboard_unit(server, "gsvc", "[Unit]\n", cfg, "go_binary")
        self.assertIn("拒绝接管", str(ctx.exception))

    def test_accepts_platform_unit(self):
        cfg = parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        server = _Server(existing="ExecStart=/opt/g/CURRENT/app")
        service_unit.onboard_unit(server, "gsvc", "[Unit]\n", cfg, "go_binary")
        self.assertTrue(any("base64 -d" in c for c in server.commands))
        self.assertTrue(any("daemon-reload" in c for c in server.commands))


if __name__ == "__main__":
    unittest.main()
