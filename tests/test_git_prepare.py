"""git_prepare 远程 URL 自动 clone 测试：目录推导 / clone 失败阻断 / 已存在复用 / 日志脱敏"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build import git_prepare  # noqa: E402
from common import mask_git_url, repo_local_path  # noqa: E402
from git_tag import tag_project  # noqa: E402


def _ok_run():
    r = mock.Mock()
    r.returncode = 0
    r.stderr = ""
    r.stdout = ""
    return r


class TestGitPrepare(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "workspace"
        self.root.mkdir()

    def test_clone_on_first_build(self):
        url = "https://git.example.com/order-service.git"
        with mock.patch("build.subprocess.run", return_value=_ok_run()) as run:
            git_prepare({"git_repo": url}, self.root)
        # 首次构建：clone 到 project_root/order-service（末段去 .git）
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["git", "clone", url, str(self.root / "order-service")])

    def test_clone_failure_blocks_build(self):
        # clone 失败 fail 阻断（区别于 checkout/pull 的仅告警：clone 是源码唯一来源）
        url = "https://git.example.com/order-service.git"
        bad = mock.Mock()
        bad.returncode = 128
        bad.stderr = "fatal: repository not found"
        with mock.patch("build.subprocess.run", return_value=bad):
            with self.assertRaises(SystemExit):
                git_prepare({"git_repo": url}, self.root)

    def test_existing_repo_reused_no_clone(self):
        url = "https://git.example.com/order-service.git"
        (self.root / "order-service").mkdir()
        (self.root / "order-service" / ".git").mkdir()
        with mock.patch("build.subprocess.run", return_value=_ok_run()) as run:
            # 已 clone 过且未配 branch/pull → 复用本地，不调任何 git 命令
            git_prepare({"git_repo": url}, self.root)
        run.assert_not_called()

    def test_url_with_credentials_masked_in_log(self):
        url = "https://ci:token123@git.example.com/order-service.git"
        with mock.patch("build.subprocess.run", return_value=_ok_run()):
            with mock.patch("build.log") as log:
                git_prepare({"git_repo": url}, self.root)
        logged = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertNotIn("token123", logged)
        self.assertIn("***", logged)

    def test_illegal_url_suffix_skipped(self):
        url = "https://git.example.com/非法目录.git"
        with mock.patch("build.subprocess.run", return_value=_ok_run()) as run:
            git_prepare({"git_repo": url}, self.root)
        run.assert_not_called()

    def test_local_repo_without_git_warns_and_skips(self):
        with mock.patch("build.subprocess.run", return_value=_ok_run()) as run:
            # 本地相对路径：不是 git 仓库 → 告警跳过（无 clone，历史行为不变）
            git_prepare({"git_repo": "service-biz"}, self.root)
        run.assert_not_called()


class TestRepoHelpers(unittest.TestCase):

    def test_repo_local_path_url(self):
        p = repo_local_path("https://git.example.com/app.git", "D:/ws")
        self.assertEqual(p, Path("D:/ws") / "app")

    def test_repo_local_path_local(self):
        p = repo_local_path("service-biz", "D:/ws")
        self.assertEqual(p, Path("D:/ws") / "service-biz")

    def test_repo_local_path_illegal(self):
        self.assertIsNone(
            repo_local_path("https://git.example.com/非法.git", "D:/ws"))

    def test_mask_git_url(self):
        self.assertEqual(
            mask_git_url("https://ci:tok@git.example.com/app.git"),
            "https://***@git.example.com/app.git")
        self.assertEqual(
            mask_git_url("https://git.example.com/app.git"),
            "https://git.example.com/app.git")


class TestTagProjectUrl(unittest.TestCase):
    """git_tag 的远程 URL 解析：打标落到 clone 同名目录"""

    def test_url_repo_resolves_to_clone_dir(self):
        tmp = Path(tempfile.mkdtemp())
        repo_dir = tmp / "order-service"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        head = "a" * 40

        def fake_run(cmd, **kw):
            r = mock.Mock()
            r.returncode = 0
            r.stderr = ""
            if len(cmd) >= 5 and cmd[3] == "rev-parse":
                if cmd[4] == "HEAD":
                    r.stdout = head + "\n"
                    return r
                raise subprocess.CalledProcessError(1, cmd)  # tag 不存在
            return r

        with mock.patch("git_tag.subprocess.run", side_effect=fake_run):
            with mock.patch("git_tag.log"):
                ok = tag_project(
                    {"git_repo": "https://git.example.com/order-service.git",
                     "image_name": "order"},
                    "1.0.0", str(tmp), push=False)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
