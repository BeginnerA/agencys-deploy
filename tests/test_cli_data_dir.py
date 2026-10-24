"""发布子进程侧的实例目录（P2-6 目录注入收尾）

Web 侧目录已全部可注入，平台拉起发布子进程时又注入了 `PLATFORM_DATA_DIR`，
但 `deploy.py` / `deploy_pipeline.py` 的 **db 模式**仍写死仓库 `data/`：

  - `data/platform.db`（auto 判定、项目校验、灰度批次读取三处）
  - `data/runtime/`（导出的运行时 YAML）

后果是第二套实例（`webapp/app.py --data-dir /srv/b`）发布的仍然是第一套实例的
配置——灰度文件对了、配置源错了，属于静默错发，比报错更难发现。

本文件锁定：CLI 侧的库文件与运行时目录一律按
「显式参数 > `PLATFORM_DATA_DIR` > 项目内 `data/`」解析。

安全约束：不打开仓库真实库；sqlite 连接用 mock 捕获路径。
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deploy
import deploy_pipeline


class PlatformDbPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli_data_dir_"))
        self._orig = os.environ.pop("PLATFORM_DATA_DIR", None)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("PLATFORM_DATA_DIR", None)
        else:
            os.environ["PLATFORM_DATA_DIR"] = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_to_repo_data(self):
        self.assertEqual(deploy.platform_db_path(),
                         Path(__file__).resolve().parent.parent / "data" / "platform.db")

    def test_env_relocates_db_and_runtime_dir(self):
        os.environ["PLATFORM_DATA_DIR"] = str(self.tmp)
        self.assertEqual(deploy.platform_db_path(), self.tmp / "platform.db")
        self.assertEqual(deploy.runtime_config_dir(7, "test"),
                         self.tmp / "runtime" / "p7" / "test")

    def test_explicit_arg_beats_env(self):
        other = self.tmp / "other"
        os.environ["PLATFORM_DATA_DIR"] = str(self.tmp)
        self.assertEqual(deploy.platform_db_path(other), other / "platform.db")


class DbModeUsesInstanceDbTest(unittest.TestCase):
    """auto 判定与 db 模式切换必须打开注入库，而不是仓库库"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli_db_mode_"))
        self.db = self.tmp / "platform.db"
        self.db.write_bytes(b"")  # 仅需存在；连接被 mock，不会真读
        self._orig = os.environ.get("PLATFORM_DATA_DIR")
        os.environ["PLATFORM_DATA_DIR"] = str(self.tmp)
        # _switch_to_db_config 会写 common 的全局覆盖位，用例结束必须还原
        import common as common_mod
        self._common = common_mod
        self._orig_override = getattr(common_mod, "CONFIG_DIR_OVERRIDE", None)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("PLATFORM_DATA_DIR", None)
        else:
            os.environ["PLATFORM_DATA_DIR"] = self._orig
        if self._orig_override is None:
            if hasattr(self._common, "CONFIG_DIR_OVERRIDE"):
                del self._common.CONFIG_DIR_OVERRIDE
        else:
            self._common.CONFIG_DIR_OVERRIDE = self._orig_override
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture_connect(self, rows=None):
        """记录 sqlite3.connect 收到的路径，返回 (列表, patch 上下文)"""
        seen = []
        fake_conn = mock.MagicMock()
        fake_conn.execute.return_value.fetchone.return_value = rows
        fake_conn.__enter__ = lambda s: fake_conn
        fake_conn.__exit__ = lambda s, *a: False

        def _connect(target, *a, **kw):
            seen.append(str(target))
            return fake_conn

        return seen, mock.patch.object(sqlite3, "connect", _connect)

    def test_auto_detection_reads_injected_db(self):
        args = Namespace(config_source="auto", project="iso-svc")
        seen, patcher = self._capture_connect(rows=(1,))
        with patcher:
            mode = deploy._resolve_config_source(args)
        self.assertEqual(mode, "db")
        self.assertEqual(seen, [str(self.db)])

    def test_switch_to_db_config_reads_and_writes_injected_dirs(self):
        args = Namespace(config_source="db", project="iso-svc", project_id=3,
                         env="test")
        seen, patcher = self._capture_connect(rows=(1,))
        with patcher, mock.patch("webapp.exporter.export_runtime") as exp:
            deploy._switch_to_db_config(args)
        self.assertEqual(seen, [str(self.db)])
        self.assertEqual(Path(exp.call_args[0][0]), self.db)
        self.assertEqual(Path(exp.call_args[0][3]),
                         self.tmp / "runtime" / "p3" / "test")

    def test_pipeline_db_mode_reads_injected_db(self):
        args = Namespace(config_source="db", pipeline="iso-pipe", project_id=2,
                         env="test")
        seen, patcher = self._capture_connect(rows=(1,))
        with patcher, mock.patch("webapp.exporter.export_runtime") as exp:
            deploy_pipeline._switch_to_db_config(args)
        self.assertIn(str(self.db), seen)
        self.assertEqual(Path(exp.call_args[0][0]), self.db)
        self.assertEqual(Path(exp.call_args[0][3]),
                         self.tmp / "runtime" / "p2" / "test")


if __name__ == "__main__":
    unittest.main()
