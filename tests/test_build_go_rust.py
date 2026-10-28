"""build.py 的 go/rust 分支：交叉编译命令、产物与 sha256、Windows 缺 target 报错、docker 零调用"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build as build_mod


def base_config(root: Path) -> dict:
    return {"deploy": {"project_root": str(root), "dist_dir": str(root / "out"),
                       "docker_compose_path": "", "remote_tmp_dir": "/tmp/x"},
            "registry": {"host": "h", "namespace": "n"},
            "deploy_mode": "direct"}


class GoBuildTest(unittest.TestCase):
    def test_cross_compile_env_and_artifact(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_"))
        calls = []

        def fake_run(cmd, cwd, env=None, timeout=None, **kw):
            calls.append((cmd, dict(env or {})))
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x7fELF")

        with mock.patch.object(build_mod, "run", side_effect=fake_run), \
                mock.patch.object(build_mod, "_find_tool", return_value="go"):
            build_mod._run_go_steps({"image_name": "report"}, root, "1.0.0", {},
                                    base_config(root))
        cmd, env = calls[0]
        self.assertEqual(cmd[:3], ["go", "build", "-trimpath"])
        self.assertEqual(env["CGO_ENABLED"], "0")
        self.assertEqual(env["GOOS"], "linux")
        self.assertEqual(env["GOARCH"], "amd64")
        out = root / "out" / "report-1.0.0"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "report-1.0.0.sha256").exists())

    def test_ldflags_and_arch_from_config(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_"))
        calls = []

        def fake_run(cmd, cwd, env=None, timeout=None, **kw):
            calls.append((cmd, dict(env or {})))
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")

        cfg = base_config(root)
        cfg["build"] = {"go_arch": "arm64", "go_ldflags": "-s -w"}
        with mock.patch.object(build_mod, "run", side_effect=fake_run), \
                mock.patch.object(build_mod, "_find_tool", return_value="go"):
            build_mod._run_go_steps({"image_name": "report"}, root, "1.0.0", {}, cfg)
        cmd, env = calls[0]
        # go_arch 无命令行形态（与上一用例同口径走 GOARCH 环境变量），此处断言其来自配置
        self.assertEqual(env["GOARCH"], "arm64")
        # go_ldflags 从配置进入命令行
        self.assertIn("-ldflags", cmd)
        self.assertIn("-s -w", cmd)

    def test_project_branch_never_calls_docker(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_proj_"))
        proj = {"deploy_mode": "go_binary", "build_type": "none",
                "image_name": "report"}
        config = base_config(root)
        with mock.patch.object(build_mod, "load_config", return_value=config), \
                mock.patch.object(build_mod, "get_project_config", return_value=proj), \
                mock.patch.object(build_mod, "get_registry_auth", return_value={}), \
                mock.patch.object(build_mod, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build_mod, "git_prepare"), \
                mock.patch.object(build_mod, "docker_login") as dl, \
                mock.patch.object(build_mod.subprocess, "run") as sp:
            build_mod.build_project("report", "1.0.0")
        dl.assert_not_called()
        sp.assert_not_called()  # 与 jar 分支同口径：docker/子进程调用计数为 0


class RustBuildTest(unittest.TestCase):
    def test_windows_without_target_fails(self):
        root = Path(tempfile.mkdtemp(prefix="rust_build_"))
        with mock.patch.object(build_mod, "run") as run_mock, \
                mock.patch.object(build_mod, "_find_tool", return_value="cargo"), \
                mock.patch.object(build_mod.os, "name", "nt"):
            with self.assertRaises(SystemExit):
                build_mod._run_rust_steps({"image_name": "report"}, root, "1.0.0",
                                          {}, base_config(root))
        run_mock.assert_not_called()  # 缺 target 必须先报错，不得尝试编译

    def test_build_with_target_and_copy(self):
        root = Path(tempfile.mkdtemp(prefix="rust_build_"))
        target_dir = root / "target" / "x86_64-unknown-linux-gnu" / "release"
        target_dir.mkdir(parents=True)
        (target_dir / "report").write_bytes(b"ELF")
        calls = []
        cfg = base_config(root)
        cfg["build"] = {"rust_target": "x86_64-unknown-linux-gnu"}
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "_find_tool", return_value="cargo"), \
                mock.patch.object(build_mod.os, "name", "nt"):
            build_mod._run_rust_steps({"image_name": "report"}, root, "1.0.0", {}, cfg)
        self.assertEqual(calls[0][:4], ["cargo", "build", "--release", "--target"])
        out = root / "out" / "report-1.0.0"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "report-1.0.0.sha256").exists())


if __name__ == "__main__":
    unittest.main()
