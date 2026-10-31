"""Task 9：WAR 策略注册、配置分派、unit 渲染分派与归属身份解析。

计划文档 docs/superpowers/plans/2026-10-30-war-tomcat-deploy.md Task 9 的验收用例；
全部走替身（Deployer.__new__ 跳过构造、exec 返回 base64 的假 server），
不做真实 SSH/网络。生产启动只导入包 `deploy_strategies`：漏掉 __init__ 的
war_tomcat 导入时 test_package_import_registers_war_mode 必须红（子进程隔离，
避免本文件顶部的 war_tomcat 导入把注册副作用带进来）。
"""
import unittest
from pathlib import Path
from unittest import mock

import service_unit
from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import (DeployStrategy, process_modes,
                                    registered_modes)
from deploy_strategies.process_config import (derive_unit_name,
                                              parse_runtime_config,
                                              validate_runtime_config)
from deploy_strategies.war_tomcat import WarTomcatStrategy

RUNTIME = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
}


def make_deployer():
    d = Deployer.__new__(Deployer)
    d.project = "orders"
    d.project_id = 3
    d.version = "2.0.0"
    d.previous_version = "1.0.0"
    d.env = "test"
    d.dist_dir = Path("/tmp/dist")
    d.ssh_cfg = {}
    d.config = {"deploy": {"canary_wait": 0}}
    d.proj_cfg = {"deploy_mode": "war_tomcat", "image_name": "orders",
                  "runtime_config": RUNTIME}
    d._strategy = None
    d.target_modified = False
    d._record_host_state = mock.Mock()
    d._nacos_ip = lambda entry: (entry or {}).get("ip", "")
    d.nacos = mock.Mock(configured=False)
    return d


class WarStrategyTest(unittest.TestCase):
    def test_registry_capabilities_and_unit(self):
        self.assertIn("war_tomcat", registered_modes())
        self.assertIn("war_tomcat", process_modes())
        d = make_deployer()
        s = create_strategy("war_tomcat", d)
        self.assertIsInstance(s, WarTomcatStrategy)
        self.assertTrue(s.strict_host_lifecycle)
        self.assertTrue(s.is_process)
        self.assertTrue(s.supports_canary and s.supports_restart)
        self.assertFalse(s.supports_prefetch)
        self.assertEqual(s.unit, "orders-war")
        self.assertEqual(s.artifact_name("2.0.0"), "orders-2.0.0.war")

    def test_package_import_registers_war_mode(self):
        """生产启动只导入包 `deploy_strategies`；漏掉 __init__ 的导入时本用例必须红。"""
        import subprocess
        import sys

        code = (
            "import deploy_strategies\n"
            "from deploy_strategies.base import process_modes, registered_modes\n"
            "assert 'war_tomcat' in registered_modes(), registered_modes()\n"
            "assert 'war_tomcat' in process_modes(), process_modes()\n"
            "print('ok')\n")
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "ok")

    def test_old_modes_do_not_enable_strict_lifecycle(self):
        self.assertFalse(DeployStrategy.strict_host_lifecycle)
        d = make_deployer()
        d.proj_cfg = {"deploy_mode": "jar_service", "image_name": "orders",
                      "runtime_config": {"app_home": "/opt/apps/orders",
                                         "java_bin": "/usr/bin/java"}}
        s = create_strategy("jar_service", d)
        self.assertFalse(s.strict_host_lifecycle)

    def test_config_and_unit_dispatch(self):
        cfg = parse_runtime_config(RUNTIME, "war_tomcat")
        self.assertEqual(cfg["tomcat_home"], "/opt/tomcat")
        self.assertEqual(validate_runtime_config(RUNTIME, "war_tomcat"), [])
        no_unit = {k: v for k, v in RUNTIME.items() if k != "unit_name"}
        self.assertEqual(derive_unit_name(no_unit, "orders", "orders",
                                          "war_tomcat"),
                         "smartops-orders-war")
        unit = service_unit.render_unit(RUNTIME, "orders", "war_tomcat")
        self.assertIn("org.apache.catalina.startup.Bootstrap start", unit)
        self.assertNotIn("catalina.sh", unit)

    def test_onboard_unit_refuses_war(self):
        with self.assertRaises(RuntimeError):
            service_unit.onboard_unit(object(), "orders-war", "[Service]\n",
                                      parse_runtime_config(RUNTIME, "war_tomcat"),
                                      "war_tomcat")

    def test_rollback_host_rejects_bad_version_name(self):
        """carry-forward：版本名先过字符集校验，防 ../ 形状越过 releases/ 解析"""
        d = make_deployer()
        s = create_strategy("war_tomcat", d)
        server = mock.Mock(host="a")
        out = s.rollback_host(server, "../evil")
        self.assertFalse(out.ok)
        self.assertEqual(out.recovery_status, "blocked")
        server.exec.assert_not_called()

    def test_resolve_instance_id_reads_manifest_only(self):
        import base64

        from deploy_strategies import tomcat_instance as ti

        class Server:
            def __init__(self, payload):
                self.payload = payload

            def exec(self, cmd, timeout=60, stdin_data=None):
                return base64.b64encode(
                    self.payload.encode("utf-8")).decode("ascii")

        server = Server('{"instance_id": "abc123"}')
        self.assertEqual(ti.resolve_instance_id(server, "/opt/apps/orders"),
                         "abc123")
        with self.assertRaises(RuntimeError):
            ti.resolve_instance_id(Server("{}"), "/opt/apps/orders")


class WarImportTest(unittest.TestCase):
    def test_yaml_import_rejects_invalid_war(self):
        import shutil
        import tempfile
        import time

        from webapp import db as dbm
        from webapp.importer import _import_projects

        tmp = tempfile.mkdtemp(prefix="war_import_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = str(Path(tmp) / "t.db")
        dbm.init_db(db)
        dbm.create_record(db, "projects", dict(
            name="p", code="p", description="", git_org="",
            created_at=int(time.time())))
        path = Path(tmp) / "projects.yaml"
        path.write_text(
            "projects:\n"
            "  war:\n"
            "    deploy_mode: war_tomcat\n"
            "    runtime_config:\n"
            "      app_home: /opt/war\n", encoding="utf-8")
        ok, msg = _import_projects(db, path)
        self.assertTrue(ok)
        self.assertIn("war", msg)
        self.assertIsNone(dbm.get_service_by_name(db, 1, "war"))


if __name__ == "__main__":
    unittest.main()
