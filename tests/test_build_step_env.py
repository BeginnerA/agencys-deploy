# -*- coding: utf-8 -*-
"""build_order 步骤级 env（构建环境变量）专项测试

背景：Vue CLI4/webpack4 在 Node17+ 必须 NODE_OPTIONS=--openssl-legacy-provider，
此前只能靠 CLI 调用方透传进程环境变量，Web UI/定时计划/Agent 等平台入口触发必挂。
整改为 build_order 步骤声明 "env": {KEY: VAL}，本文件锁三段行为：
构建执行合并生效且不污染全局、exporter DB→YAML 保留、importer YAML→DB 保留。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import build  # noqa: E402
from webapp import db as dbm, exporter, importer  # noqa: E402

STEP_ENV = {"NODE_OPTIONS": "--openssl-legacy-provider"}


class TestStepEnvBuild(unittest.TestCase):
    def test_frontend_step_env_merged_and_not_polluting(self):
        proj = {"build_order": [{"name": "fe", "path": ".",
                                 "commands": ["npm run build:stage"],
                                 "env": STEP_ENV}]}
        global_env = {"PATH": "keepme"}
        with mock.patch.object(build, "find_node", return_value="node"), \
                mock.patch.object(build, "run_shell") as rs, \
                mock.patch.object(build, "log"):
            build._run_frontend_steps(proj, Path(tempfile.mkdtemp()),
                                      "20261012", global_env)
        sent_env = rs.call_args.kwargs["env"]
        self.assertEqual(sent_env["NODE_OPTIONS"], "--openssl-legacy-provider")
        self.assertEqual(sent_env["PATH"], "keepme")     # 继承全局
        self.assertNotIn("NODE_OPTIONS", global_env)     # 不污染全局 env_vars

    def test_frontend_step_without_env_unchanged(self):
        proj = {"build_order": [{"name": "fe", "path": ".",
                                 "commands": ["npm run build"]}]}
        with mock.patch.object(build, "find_node", return_value="node"), \
                mock.patch.object(build, "run_shell") as rs, \
                mock.patch.object(build, "log"):
            build._run_frontend_steps(proj, Path(tempfile.mkdtemp()),
                                      "1", {"K": "V"})
        self.assertEqual(rs.call_args.kwargs["env"], {"K": "V"})

    def test_maven_step_env_merged(self):
        root = Path(tempfile.mkdtemp())
        (root / "mod").mkdir()
        (root / "mod" / "pom.xml").write_text("<project/>", encoding="utf-8")
        proj = {"build_order": [{"name": "m", "path": "mod", "goal": "package",
                                 "env": {"MAVEN_OPTS": "-Xmx1g"}}]}
        with mock.patch.object(build, "find_mvn", return_value="mvn"), \
                mock.patch.object(build, "find_java_home", return_value=""), \
                mock.patch.object(build, "run") as r, \
                mock.patch.object(build, "log"):
            build._run_maven_steps(proj, root, "1.0.0", "prod", {"PATH": "p"}, {})
        self.assertEqual(r.call_args.kwargs["env"]["MAVEN_OPTS"], "-Xmx1g")


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "p.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=1))

    def _svc_with(self, build_order, **extra):
        fields = dict(project_id=self.pid, name="talent-web",
                      image_name="talent-web", service_name="talent-web",
                      docker_context="", dockerfile="",
                      build_order=build_order, nacos_groups=[],
                      dubbo_services=[], snailjob_group="", health_url="",
                      health_port=0, log_error_keywords=[], clear_log_dir="",
                      git_repo="", enabled=1)
        fields.update(extra)
        return dbm.create_record(self.db, "services", fields)

    def test_exporter_preserves_step_env(self):
        self._svc_with([{"name": "fe", "path": "new-talent-admin",
                         "commands": ["npm run build:stage"], "env": STEP_ENV}],
                       build_type="frontend", deploy_mode="static_archive",
                       site_root="/srv/site/index", zip_prefix="talent-web",
                       static_dist_dir="dist")
        text = exporter.export_projects_yaml(self.db, self.pid)
        self.assertIn("NODE_OPTIONS", text)
        self.assertIn("--openssl-legacy-provider", text)

    def test_exporter_no_step_env_omits_key(self):
        # 向后兼容：无 env 的老配置导出 YAML 不新增 env 键
        self._svc_with([{"name": "m", "path": "mod", "goal": "package"}])
        text = exporter.export_projects_yaml(self.db, self.pid)
        self.assertNotIn("env:", text)

    def test_importer_preserves_step_env(self):
        y = self.tmp / "projects.yaml"
        y.write_text(
            "projects:\n"
            "  talent-web:\n"
            "    build_type: frontend\n"
            "    build_order:\n"
            "      - name: fe\n"
            "        path: new-talent-admin\n"
            "        commands: [\"npm run build:stage\"]\n"
            "        env:\n"
            "          NODE_OPTIONS: --openssl-legacy-provider\n",
            encoding="utf-8")
        ok, msg = importer._import_projects(self.db, y)
        self.assertTrue(ok, msg)
        svc = dbm.get_service_by_name(self.db, self.pid, "talent-web")
        self.assertEqual(svc["build_order"][0]["env"], STEP_ENV)


if __name__ == "__main__":
    unittest.main()
