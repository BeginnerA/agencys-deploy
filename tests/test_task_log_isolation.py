"""任务日志目录注入与越界防护（P2-6）

背景：`task_manager` 的日志目录是模块级常量 `LOG_DIR = <root>/data/logs`，
既不受 `app.config["LOG_DIR"]` 约束，也无法在测试里改指到临时目录：
  - `_run_task` 把任务日志写进真实 `data/logs/`；
  - `tail_log` 的落点校验拿真实目录比对，注入目录内的合法日志反被判为越界。

本文件锁定：日志落点可由 `set_log_dir()` 或调用点显式 `log_dir=` 指定，
且 `tail_log` 的越界防护以**当前生效目录**为边界（换目录后旧目录文件读不到）。

安全约束：不执行任何真实发布——子进程命令被替换为本地 `python -c` 打印，
不触碰 SSH / 容器 / 网络；数据库与日志全在临时目录。
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg
from webapp import task_manager
from webapp import db as dbm

_MARKER = "SUBPROCESS-MARKER-P26"
_TEST_CMD = [sys.executable, "-c", f"print('{_MARKER}')"]


class TaskLogDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tasklog_"))
        self.db_path = str(self.tmp / "platform.db")
        dbm.init_db(self.db_path)
        self.log_dir = self.tmp / "logs"
        self._orig_log_dir = task_manager.LOG_DIR
        task_manager.set_log_dir(self.log_dir)

    def tearDown(self):
        task_manager.set_log_dir(self._orig_log_dir)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_task(self, db_path=None, extra=None):
        db_path = db_path or self.db_path
        pid = dbm.create_record(db_path, "projects", dict(
            name="隔离测试项目", code=f"iso{int(time.time() * 1000) % 100000}",
            description="", git_org="", created_at=int(time.time())))
        task_id = dbm.create_task(db_path, pid, "project", 1, "svc-demo",
                                  "test", "1.0.1", "", 1, extra=extra or [])
        return dbm.get_task(db_path, task_id)

    def _await_terminal(self, db_path, task_id):
        """_run_task 同步执行，终态应已落库；留轮询仅为暴露异常路径"""
        deadline = time.time() + 10
        while time.time() < deadline:
            status = dbm.get_task(db_path, task_id)["status"]
            if status in ("success", "failed"):
                return status
            time.sleep(0.05)
        self.fail(f"任务未进入终态：{dbm.get_task(db_path, task_id)}")

    def _log_text(self, db_path, task_id):
        path = dbm.get_task(db_path, task_id)["log_file"]
        if not path:
            return "<任务记录无 log_file>"
        try:
            return Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            return f"<log_file 不可读 {path}: {exc}>"

    def test_run_task_writes_into_configured_dir(self):
        """日志文件必须落在注入目录，而不是仓库真实 data/logs"""
        task = self._new_task()
        with mock.patch.object(task_manager, "build_cli", return_value=_TEST_CMD):
            task_manager._run_task(self.db_path, task, [])
        status = self._await_terminal(self.db_path, task["id"])
        self.assertEqual(status, "success", msg=self._log_text(self.db_path, task["id"]))
        recorded = Path(dbm.get_task(self.db_path, task["id"])["log_file"]).resolve()
        self.assertTrue(recorded.is_relative_to(self.log_dir.resolve()),
                        f"日志落点越界：{recorded}")
        self.assertIn(_MARKER, recorded.read_text(encoding="utf-8"))

    def test_explicit_log_dir_wins_over_module_default(self):
        """调用点显式 log_dir（来自 app.config）优先于模块配置"""
        other = self.tmp / "app-logs"
        task = self._new_task()
        with mock.patch.object(task_manager, "build_cli", return_value=_TEST_CMD):
            task_manager._run_task(self.db_path, task, [], log_dir=other)
        self.assertEqual(self._await_terminal(self.db_path, task["id"]), "success")
        recorded = Path(dbm.get_task(self.db_path, task["id"])["log_file"]).resolve()
        self.assertTrue(recorded.is_relative_to(other.resolve()),
                        f"显式 log_dir 未生效：{recorded}")

    def test_tail_log_confines_to_effective_dir(self):
        """越界防护以当前生效目录为边界：换目录后不得继续读旧目录日志"""
        inside = self.log_dir / "task_9.log"
        inside.parent.mkdir(parents=True, exist_ok=True)
        inside.write_text("inside log\n", encoding="utf-8")
        outside = self.tmp / "elsewhere.log"
        outside.write_text("outside log\n", encoding="utf-8")

        text, offset, truncated = task_manager.tail_log(str(inside))
        self.assertEqual(text, "inside log\n")
        self.assertEqual(offset, len("inside log\n"))
        self.assertFalse(truncated)

        self.assertEqual(task_manager.tail_log(str(outside)), ("", 0, False))

        # 切到另一个目录后，原目录内的日志即视为越界（防跨实例读取）
        task_manager.set_log_dir(self.tmp / "second-logs")
        self.assertEqual(task_manager.tail_log(str(inside)), ("", 0, False))

    def test_tail_log_accepts_explicit_dir_for_route_isolation(self):
        """路由把 app.config['LOG_DIR'] 传入：与模块配置不一致时以传入值为准"""
        route_dir = self.tmp / "route-logs"
        route_dir.mkdir(parents=True, exist_ok=True)
        target = route_dir / "task_11.log"
        target.write_text("routed\n", encoding="utf-8")
        text, _, _ = task_manager.tail_log(str(target), log_dir=route_dir)
        self.assertEqual(text, "routed\n")
        # 同一文件在「模块目录」视角下仍属越界
        self.assertEqual(task_manager.tail_log(str(target)), ("", 0, False))

    def test_create_app_configures_task_log_dir(self):
        """应用工厂注入 log_dir → 后台线程（无 app 上下文）也写到同一目录"""
        task_manager.set_log_dir(None)  # 回到「未配置」态，由 create_app 负责配置
        data_dir = self.tmp / "app-data"
        config_dir = self.tmp / "app-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        app = webapp_pkg.create_app(data_dir=data_dir, config_dir=config_dir,
                                    log_dir=self.log_dir)
        self.assertEqual(Path(app.config["LOG_DIR"]).resolve(), self.log_dir.resolve())
        task = self._new_task(app.config["DB_PATH"])
        with mock.patch.object(task_manager, "build_cli", return_value=_TEST_CMD):
            task_manager._run_task(app.config["DB_PATH"], task, [])
        db_path = app.config["DB_PATH"]
        status = dbm.get_task(db_path, task["id"])["status"]
        self.assertEqual(status, "success", msg=self._log_text(db_path, task["id"]))
        recorded = Path(dbm.get_task(db_path, task["id"])["log_file"]).resolve()
        self.assertTrue(recorded.is_relative_to(self.log_dir.resolve()),
                        f"create_app 未把 LOG_DIR 交给任务执行器：{recorded}")


if __name__ == "__main__":
    unittest.main()
