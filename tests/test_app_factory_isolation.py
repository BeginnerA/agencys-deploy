"""应用工厂目录注入与入口惰性化（P2-6）

背景：`create_app()` 此前只读模块级 `webapp.DATA_DIR` / `webapp.CONFIG_DIR`，
测试靠「改全局变量再调 create_app」实现隔离——同一进程内多个测试模块互相覆盖，
且 `webapp/app.py` 在 **import 时** 就执行 `create_app()`：任何脚本只要
`import webapp.app`（哪怕只为取一个函数），就会打开真实 `data/platform.db`、
触发首启引导并写 `data/secret_key`。

本文件锁定的行为：
  1. `create_app(data_dir=, config_dir=, log_dir=)` 全部落点在注入目录内，模块全局不被改写；
  2. 密钥文件按 data_dir 隔离，注入的 config_dir 决定引导导入来源（空目录 = 不导入）；
  3. import 入口模块不再创建 app，`get_app()` 惰性构建且只构建一次；
  4. CLI 的 `--data-dir / --config-dir / --log-dir` 原样传给工厂（不传 = None，回落默认）。

安全约束：全程只使用临时目录，绝不触碰仓库 `data/`（内含真实库）。
"""
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg
from webapp import db as dbm


class InjectedDirsTest(unittest.TestCase):
    """create_app 的目录参数必须成为唯一的落点来源"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="factory_"))
        self.data_dir = self.tmp / "data"
        self.config_dir = self.tmp / "config"
        self.log_dir = self.tmp / "data" / "logs"
        self._orig_env = os.environ.pop("DASHBOARD_SECRET_KEY", None)

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["DASHBOARD_SECRET_KEY"] = self._orig_env
        else:
            os.environ.pop("DASHBOARD_SECRET_KEY", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_paths_land_in_injected_data_dir(self):
        app = webapp_pkg.create_app(data_dir=self.data_dir, config_dir=self.config_dir)
        self.assertEqual(Path(app.config["DB_PATH"]).resolve(),
                         (self.data_dir / "platform.db").resolve())
        self.assertEqual(Path(app.config["LOG_DIR"]).resolve(),
                         (self.data_dir / "logs").resolve())
        self.assertEqual(Path(app.config["CONFIG_DIR"]).resolve(),
                         self.config_dir.resolve())
        # 密钥文件也必须在注入目录内（否则测试进程会写真实 data/secret_key）
        self.assertTrue((self.data_dir / "secret_key").exists())

    def test_explicit_log_dir_overrides_data_dir_default(self):
        custom = self.tmp / "task-logs"
        app = webapp_pkg.create_app(data_dir=self.data_dir, config_dir=self.config_dir,
                                    log_dir=custom)
        self.assertEqual(Path(app.config["LOG_DIR"]).resolve(), custom.resolve())

    def test_module_globals_are_not_rewritten(self):
        """注入不得改写模块全局（全局被改写 = 后续测试的隐式依赖）"""
        before_data = webapp_pkg.DATA_DIR
        before_config = webapp_pkg.CONFIG_DIR
        webapp_pkg.create_app(data_dir=self.data_dir, config_dir=self.config_dir)
        self.assertEqual(webapp_pkg.DATA_DIR, before_data)
        self.assertEqual(webapp_pkg.CONFIG_DIR, before_config)

    def test_injected_config_dir_drives_onboarding(self):
        """引导导入来源 = 注入的 config_dir；空目录时不导入任何项目，但仍建 admin"""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        app = webapp_pkg.create_app(data_dir=self.data_dir, config_dir=self.config_dir)
        db_path = app.config["DB_PATH"]
        self.assertEqual(dbm.project_count(db_path), 0)
        self.assertIsNotNone(dbm.get_user_by_name(db_path, "admin"))

    def test_secret_key_is_per_data_dir(self):
        """两个 data_dir 各自持久化独立密钥（不共享、不回退开发密钥）"""
        a = self.tmp / "a"
        b = self.tmp / "b"
        app_a = webapp_pkg.create_app(data_dir=a, config_dir=self.config_dir)
        app_b = webapp_pkg.create_app(data_dir=b, config_dir=self.config_dir)
        key_a = app_a.config["SECRET_KEY"]
        key_b = app_b.config["SECRET_KEY"]
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, webapp_pkg.DEV_SECRET_KEY)
        # 重建同一 data_dir 时密钥稳定（会话不因重启失效）
        app_a2 = webapp_pkg.create_app(data_dir=a, config_dir=self.config_dir)
        self.assertEqual(app_a2.config["SECRET_KEY"], key_a)


class CliDirectoryArgsTest(unittest.TestCase):
    """`--data-dir / --config-dir / --log-dir` 必须把落点交给工厂

    工厂能注入目录还不够：生产上跑第二套实例（同一份代码、另一份数据）只能靠
    CLI 传参，否则它必然读写仓库默认的 data/。这里用 mock 拦住真实的
    create_app 与后台线程，只验证参数传递，不建库、不起服务。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli_dir_"))
        self._saved = sys.modules.pop("webapp.app", None)

    def tearDown(self):
        sys.modules.pop("webapp.app", None)
        if self._saved is not None:
            sys.modules["webapp.app"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_main(self, module, argv):
        with mock.patch.object(module, "create_app") as created, \
                mock.patch.object(module, "start_background_services"):
            module.main(argv)
        return created

    def test_directory_args_reach_factory(self):
        module = importlib.import_module("webapp.app")
        data_dir = self.tmp / "data"
        config_dir = self.tmp / "config"
        log_dir = self.tmp / "logs"
        created = self._run_main(module, [
            "--data-dir", str(data_dir), "--config-dir", str(config_dir),
            "--log-dir", str(log_dir)])
        kwargs = created.call_args.kwargs
        self.assertEqual(Path(kwargs["data_dir"]).resolve(), data_dir.resolve())
        self.assertEqual(Path(kwargs["config_dir"]).resolve(), config_dir.resolve())
        self.assertEqual(Path(kwargs["log_dir"]).resolve(), log_dir.resolve())

    def test_defaults_pass_none_so_factory_keeps_module_globals(self):
        """不传目录时必须传 None（由工厂回落到模块默认），不能是空串"""
        module = importlib.import_module("webapp.app")
        created = self._run_main(module, [])
        kwargs = created.call_args.kwargs
        self.assertIsNone(kwargs["data_dir"])
        self.assertIsNone(kwargs["config_dir"])
        self.assertIsNone(kwargs["log_dir"])


class LazyEntrypointTest(unittest.TestCase):
    """webapp/app.py 不得在 import 时创建 app"""

    def setUp(self):
        self._saved = sys.modules.pop("webapp.app", None)

    def tearDown(self):
        sys.modules.pop("webapp.app", None)
        if self._saved is not None:
            sys.modules["webapp.app"] = self._saved

    def test_import_does_not_build_app(self):
        with mock.patch.object(webapp_pkg, "create_app",
                               side_effect=AssertionError("import 时不应创建 app")) as spy:
            module = importlib.import_module("webapp.app")
        self.assertFalse(spy.called)
        self.assertTrue(callable(module.get_app))

    def test_get_app_builds_once_and_caches(self):
        stub = mock.MagicMock(name="create_app")
        with mock.patch.object(webapp_pkg, "create_app", stub):
            module = importlib.import_module("webapp.app")
            first = module.get_app()
            second = module.get_app()
        self.assertIs(first, second)
        self.assertEqual(stub.call_count, 1)


class SuiteDefaultDirGuardTest(unittest.TestCase):
    """测试会话内裸 `create_app()` 不得落在仓库 data/

    工厂支持注入目录 ≠ 测试真的注入了：历史上 4 个用例写成
    `create_app()` + 事后改 `app.config["DB_PATH"]`，工厂已经拿仓库
    `data/platform.db`（用户生产库）跑过一遍建表迁移与首启引导，
    导出接口也顺着默认 DATA_DIR 覆盖了 `data/export/config/`。
    这类污染不会让用例失败，只会静默改坏用户数据，所以必须有会话级护栏。

    护栏落在 tests/conftest.py：会话开始把 webapp.DATA_DIR / CONFIG_DIR 指向临时目录。
    需要验证"生产缺省落点 = 仓库 data/"的用例请显式传参或断言模块常量。
    """

    def test_bare_create_app_does_not_land_on_repo_data(self):
        repo_data = Path(__file__).resolve().parent.parent / "data"
        app = webapp_pkg.create_app()
        self.assertNotEqual(Path(app.config["DATA_DIR"]).resolve(),
                            repo_data.resolve(),
                            "裸 create_app() 仍指向仓库 data/：测试会写用户生产库")
        self.assertNotEqual(Path(app.config["DB_PATH"]).resolve(),
                            (repo_data / "platform.db").resolve())


if __name__ == "__main__":
    unittest.main()
