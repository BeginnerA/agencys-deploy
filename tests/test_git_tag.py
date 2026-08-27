"""Git Tag 拼接与调用测试：v{version} / v{version}-{suffix}"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from git_tag import tag_project  # noqa: E402


class TestGitTag(unittest.TestCase):
    def _make_repo(self) -> str:
        """创建带 .git 目录的假仓库，返回其绝对路径"""
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".git").mkdir()
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        return str(tmp)

    def test_tag_without_suffix(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run") as run, mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False)
        self.assertTrue(ok)
        self.assertEqual(run.call_args_list[0].args[0][-1], "v1.0.30")

    def test_tag_with_suffix(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run") as run, mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False,
                             suffix="test")
        self.assertTrue(ok)
        self.assertEqual(run.call_args_list[0].args[0][-1], "v1.0.30-test")

    def test_push_flag_controls_second_call(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run") as run, mock.patch("git_tag.log"):
            tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                        "1.0.30", str(Path(repo).parent), push=True, suffix="rc1")
        self.assertEqual(len(run.call_args_list), 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], "v1.0.30-rc1")
        self.assertEqual(run.call_args_list[1].args[0][-1], "v1.0.30-rc1")

    def test_skip_when_no_repo_configured(self):
        with mock.patch("git_tag.log") as log:
            ok = tag_project({"git_repo": "", "image_name": "svc"},
                             "1.0.30", "D:/x")
        self.assertFalse(ok)
        log.assert_called_once()

    def test_skip_when_not_git_repo(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(tmp).name, "image_name": "svc"},
                             "1.0.30", str(Path(tmp).parent))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
