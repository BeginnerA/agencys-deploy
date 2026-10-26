"""jar_service 配置导出→导入往返：runtime_config 不丢失、不误伤 container"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.exporter import export_projects_yaml
from webapp.importer import _import_projects


class RoundtripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_roundtrip_")
        self.src = os.path.join(self.tmp, "src.db")
        self.dst = os.path.join(self.tmp, "dst.db")
        dbm.init_db(self.src)
        dbm.init_db(self.dst)
        pid = dbm.create_record(self.src, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_record(self.src, "services", dict(
            project_id=pid, name="auth", image_name="auth", service_name="auth",
            deploy_mode="jar_service", runtime_config={
                "app_home": "/opt/auth", "port": 8080,
                "jvm_opts": "-Xmx1g", "env_files": ["/opt/auth/.env"]}))
        dbm.create_record(self.src, "services", dict(
            project_id=pid, name="web", image_name="web", service_name="web",
            deploy_mode="container", runtime_config={}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_then_import_preserves_runtime_config(self):
        pid = dbm.list_records(self.src, "projects")[0]["id"]
        text = export_projects_yaml(self.src, pid)
        path = Path(self.tmp) / "projects.yaml"
        path.write_text(text, encoding="utf-8")
        data = yaml.safe_load(text)
        self.assertEqual(data["projects"]["auth"]["runtime_config"]["app_home"], "/opt/auth")
        self.assertNotIn("runtime_config", data["projects"]["web"])
        ok, _ = _import_projects(self.dst, path)
        self.assertTrue(ok)
        rec = dbm.get_service_by_name(self.dst, 1, "auth")
        self.assertEqual(rec["runtime_config"]["app_home"], "/opt/auth")
        self.assertEqual(rec["deploy_mode"], "jar_service")

    def test_import_is_insert_only_and_does_not_clear_existing(self):
        # 既有服务同名 → 整条跳过，不能把线上 runtime_config 冲成 {}
        pid = dbm.create_record(self.dst, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_record(self.dst, "services", dict(
            project_id=pid, name="auth", image_name="auth", service_name="auth",
            deploy_mode="jar_service", runtime_config={"app_home": "/srv/keep"}))
        path = Path(self.tmp) / "empty.yaml"
        path.write_text("projects:\n  auth:\n    image_name: auth\n", encoding="utf-8")
        ok, _ = _import_projects(self.dst, path)
        self.assertTrue(ok)
        rec = dbm.get_service_by_name(self.dst, pid, "auth")
        self.assertEqual(rec["runtime_config"], {"app_home": "/srv/keep"})


if __name__ == "__main__":
    unittest.main()
