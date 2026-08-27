"""Git Tag 拼接与调用测试：v{version} / v{version}-{suffix} / tag 已存在时的更新语义"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from git_tag import tag_project  # noqa: E402

HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OLD = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def run_side_effect(existing):
    """模拟 git 命令：rev-parse HEAD 恒返回 HEAD；rev-parse <tag> 按 existing 返回"""
    def side_effect(cmd, **kwargs):
        r = mock.Mock()
        r.returncode = 0
        r.stderr = ""
        if len(cmd) >= 5 and cmd[3] == "rev-parse":
            if cmd[4] == "HEAD":
                r.stdout = HEAD + "\n"
            elif existing is None:
                raise subprocess.CalledProcessError(1, cmd)
            else:
                r.stdout = existing + "\n"
        else:
            r.stdout = ""
        return r
    return side_effect


def git_calls(run):
    """提取所有 git 命令（含完整参数）"""
    return [c.args[0] for c in run.call_args_list
            if isinstance(c.args[0], list) and len(c.args[0]) >= 2
            and c.args[0][0] == "git"]


def has_tag_call(calls, tag_name, force=False):
    for cmd in calls:
        if len(cmd) >= 5 and cmd[3] == "tag" and cmd[-1] == tag_name:
            if force == ("-f" in cmd):
                return True
    return False


def has_push_call(calls, tag_name, force=False):
    for cmd in calls:
        if len(cmd) >= 4 and cmd[3] == "push" and tag_name in cmd:
            if force == ("--force" in cmd):
                return True
    return False


class TestGitTag(unittest.TestCase):
    def _make_repo(self) -> str:
        """创建带 .git 目录的假仓库，返回其绝对路径"""
        tmp = Path(tempfile.mkdtemp())
        (tmp / ".git").mkdir()
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        return str(tmp)

    def test_tag_without_suffix(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(None)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False)
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertTrue(has_tag_call(calls, "v1.0.30"))
        self.assertFalse(has_tag_call(calls, "v1.0.30", force=True))

    def test_tag_with_suffix(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(None)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False,
                             suffix="test")
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertTrue(has_tag_call(calls, "v1.0.30-test"))

    def test_push_flag_controls_push_call(self):
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(None)) as run, \
                mock.patch("git_tag.log"):
            tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                        "1.0.30", str(Path(repo).parent), push=True, suffix="rc1")
        calls = git_calls(run)
        self.assertTrue(has_push_call(calls, "v1.0.30-rc1"))
        self.assertFalse(has_push_call(calls, "v1.0.30-rc1", force=True))

    def test_existing_tag_same_commit_skips_retag(self):
        """tag 已存在且指向当前 HEAD：不重新打标（push=False 时无 tag 命令）"""
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(HEAD)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False)
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertFalse(has_tag_call(calls, "v1.0.30"))

    def test_existing_tag_same_commit_still_confirms_push(self):
        """tag 已存在且指向当前 HEAD 但 push=True：普通推送确认远端存在"""
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(HEAD)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=True)
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertTrue(has_push_call(calls, "v1.0.30"))
        self.assertFalse(has_push_call(calls, "v1.0.30", force=True))

    def test_existing_tag_old_commit_force_updates(self):
        """同版本重复发布：tag 指向旧提交时强制移动到当前 HEAD"""
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(OLD)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=False)
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertTrue(has_tag_call(calls, "v1.0.30", force=True))

    def test_existing_tag_old_commit_force_push(self):
        """tag 强制更新时，远端推送使用 --force"""
        repo = self._make_repo()
        with mock.patch("subprocess.run", side_effect=run_side_effect(OLD)) as run, \
                mock.patch("git_tag.log"):
            ok = tag_project({"git_repo": Path(repo).name, "image_name": "svc"},
                             "1.0.30", str(Path(repo).parent), push=True)
        self.assertTrue(ok)
        calls = git_calls(run)
        self.assertTrue(has_push_call(calls, "v1.0.30", force=True))

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
