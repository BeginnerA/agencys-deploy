"""Agent 工具的目录落点隔离（P2-6 收尾）

背景：Web 侧目录已经全部走 `create_app(data_dir=/config_dir=/log_dir=)` 注入，
后台线程靠 `task_manager.LOG_DIR` / `set_log_dir()` 取得同一个目录；但
`agent/tools.py` 里还有三处按「仓库根/data」算路径：

  - `logs_context` 的路径遍历防护（只认仓库 data/logs/collect）
  - `manage_log_storage` 的存储概况（扫仓库 data/logs）
  - `manage_config_backup` 的导出目标（写仓库 data/export/config）

用 `--data-dir/--log-dir` 起第二套实例时，这些工具会读错目录、把导出物写进
仓库真实 data/——所以既是不一致，也是真实的写副作用。

本文件锁定的行为：三处一律以「db 所在目录 / task_manager.LOG_DIR」为准。

安全约束：全程临时目录；导出用 stub，绝不写仓库 data/。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp import task_manager


class AgentDirBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agent_dir_"))
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True)
        self.db = str(self.data_dir / "platform.db")
        dbm.init_db(self.db)
        self.log_dir = self.tmp / "instance-logs"
        self._orig_log_dir = task_manager.LOG_DIR
        task_manager.set_log_dir(self.log_dir)

    def tearDown(self):
        task_manager.set_log_dir(self._orig_log_dir)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ctx(self, role="admin"):
        return type("Ctx", (), {"db_path": self.db, "user_id": 1,
                                "username": "tester", "role": role})()

    def _call(self, name, args):
        from agent import tools as tools_mod
        return json.loads(tools_mod.execute_tool("admin", name, args, self._ctx()))


class LogsContextDirTest(AgentDirBase):
    """logs_context 的合法目录 = 实例日志目录下的 collect/"""

    def _entry(self, log_root: Path):
        collect = log_root / "collect"
        collect.mkdir(parents=True, exist_ok=True)
        path = collect / "auth.log"
        path.write_text("line-1\nline-2\nline-3\n")
        return dbm.create_record(self.db, "log_entries", dict(
            ts=int(time.time()), env="prod", service="auth", container="auth",
            file_path=str(path), line_offset=0, line_count=3,
            content="line-2", keywords="[]"))

    def test_accepts_file_under_injected_log_dir(self):
        entry_id = self._entry(self.log_dir)
        out = self._call("logs_context", {"id": entry_id, "lines": 3})
        self.assertIn("line-2", json.dumps(out, ensure_ascii=False))

    def test_rejects_file_outside_injected_log_dir(self):
        entry_id = self._entry(self.tmp / "elsewhere")
        out = self._call("logs_context", {"id": entry_id})
        self.assertIn("error", out)


class LogStorageStatusDirTest(AgentDirBase):
    """manage_log_storage status 统计的是实例日志目录，不是仓库 data/logs"""

    def test_collect_dir_stats_follow_injected_log_dir(self):
        collect = self.log_dir / "collect"
        collect.mkdir(parents=True, exist_ok=True)
        (collect / "a.log").write_text("x" * 1024)
        out = self._call("manage_log_storage", {"action": "status"})
        self.assertEqual(out["collect_dir"]["files"], 1)


class ConfigBackupDirTest(AgentDirBase):
    """manage_config_backup export 的目标目录跟随 db 所在目录"""

    def test_export_target_is_under_data_dir_of_db(self):
        expected = self.data_dir / "export" / "config"
        with mock.patch("webapp.exporter.export_all",
                        return_value=["projects.yaml"]) as spy:
            out = self._call("manage_config_backup", {"action": "export"})
        self.assertEqual(Path(spy.call_args[0][1]).resolve(), expected.resolve())
        self.assertEqual(Path(out["dir"]).resolve(), expected.resolve())
        self.assertNotIn(str(Path(__file__).resolve().parent.parent / "data"),
                         str(out["dir"]))


if __name__ == "__main__":
    unittest.main()
