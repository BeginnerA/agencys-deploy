"""构建产物契约：可执行 jar 选择规则、校验和生成、docker 零调用"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build as build_mod


class SelectJarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_build_")
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel, size):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def test_picks_largest_and_skips_noise(self):
        self._touch("target/app-1.0.jar", 10)
        self._touch("target/app-1.0-sources.jar", 999)
        self._touch("target/app-1.0-original.jar", 888)
        self._touch("target/app-1.0-exec.jar", 500)
        proj = {"runtime_config": {"artifact_pattern": "target/*.jar"}}
        picked = build_mod._select_jar(proj, self.root)
        self.assertEqual(picked.name, "app-1.0-exec.jar")

    def test_falls_back_to_build_step_paths(self):
        self._touch("svc-a/target/svc-a.jar", 100)
        proj = {"build_order": [{"path": "svc-a"}], "runtime_config": {}}
        picked = build_mod._select_jar(proj, self.root)
        self.assertEqual(picked.name, "svc-a.jar")

    def test_no_match_fails_with_listing(self):
        self._touch("target/only-sources.jar", 10)
        proj = {"runtime_config": {}}
        with self.assertRaises(SystemExit):
            build_mod._select_jar(proj, self.root)

    def test_make_artifact_and_sha256(self):
        jar = self._touch("target/app.jar", 123)
        proj = {"runtime_config": {}, "image_name": "hqb-auth"}
        with mock.patch.object(build_mod, "log"):
            out = build_mod._make_jar_artifact(proj, self.root, "1.0.31",
                                               {"deploy": {"dist_dir": str(self.root / "out")}})
        self.assertEqual(out.name, "hqb-auth-1.0.31.jar")
        self.assertEqual(out.stat().st_size, 123)
        side = out.with_name(out.name + ".sha256")
        self.assertTrue(side.exists())
        self.assertEqual(len(side.read_text(encoding="utf-8").strip().split()[0]), 64)


class BuildProjectBranchTest(unittest.TestCase):
    def test_jar_service_never_calls_docker(self):
        tmp = tempfile.mkdtemp(prefix="jar_build_project_")
        try:
            root = Path(tmp)
            (root / "target").mkdir()
            (root / "target" / "app.jar").write_bytes(b"x" * 10)
            proj = {"deploy_mode": "jar_service", "build_type": "none",
                    "image_name": "hqb-auth", "runtime_config": {},
                    "build_order": []}
            config = {"deploy": {"project_root": str(root), "dist_dir": str(root / "out"),
                                 "docker_compose_path": "", "remote_tmp_dir": "/tmp/x"},
                      "registry": {"host": "h", "namespace": "n"},
                      "deploy_mode": "direct"}
            with mock.patch.object(build_mod, "load_config", return_value=config), \
                    mock.patch.object(build_mod, "get_project_config", return_value=proj), \
                    mock.patch.object(build_mod, "get_registry_auth", return_value={}), \
                    mock.patch.object(build_mod, "get_registry_mode", return_value="direct"), \
                    mock.patch.object(build_mod, "git_prepare"), \
                    mock.patch.object(build_mod, "docker_login") as login, \
                    mock.patch.object(build_mod.subprocess, "run") as sp:
                build_mod.build_project("auth", "1.0.31", env="test")
            login.assert_not_called()
            sp.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
