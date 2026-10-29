"""build.py 的 node 分支：npm ci/build/prune 序列与 tar.gz 产物（Task 10 追加 python 用例）"""
import sys
import tarfile
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


def make_node_project(root: Path, with_build=False) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "index.js").write_text("console.log(1)\n", encoding="utf-8")
    scripts = {"start": "node src/index.js"}
    if with_build:
        scripts["build"] = "echo built"
    (root / "package.json").write_text(
        '{"name":"nsvc","version":"0.0.0","scripts":' +
        str(scripts).replace("'", '"') + '}', encoding="utf-8")
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")


class NodeBuildTest(unittest.TestCase):
    def test_steps_and_package_content(self):
        root = Path(tempfile.mkdtemp(prefix="node_build_"))
        make_node_project(root, with_build=True)
        calls = []
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "find_node", return_value="node"), \
                mock.patch.object(build_mod, "_find_tool", return_value="npm"):
            build_mod._run_node_steps({"image_name": "nsvc"}, root, "1.0.0", {},
                                      base_config(root))
        self.assertEqual(calls[0][:2], ["npm", "ci"])
        self.assertEqual(calls[1][:3], ["npm", "run", "build"])
        self.assertEqual(calls[2][:2], ["npm", "prune"])
        out = root / "out" / "nsvc-1.0.0.tar.gz"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "nsvc-1.0.0.tar.gz.sha256").exists())
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        self.assertTrue(any(n == "./src/index.js" or n.endswith("src/index.js")
                            for n in names))
        self.assertFalse(any(".git/" in n or n.endswith("/.git/HEAD")
                             for n in names))
        self.assertFalse(any(n.endswith("/.env") or n == "./.env" for n in names))

    def test_no_build_script_skips_run_build(self):
        root = Path(tempfile.mkdtemp(prefix="node_build_"))
        make_node_project(root, with_build=False)
        calls = []
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "find_node", return_value="node"), \
                mock.patch.object(build_mod, "_find_tool", return_value="npm"):
            build_mod._run_node_steps({"image_name": "nsvc"}, root, "1.0.0", {},
                                      base_config(root))
        self.assertEqual(len(calls), 2)  # ci + prune


if __name__ == "__main__":
    unittest.main()
