"""测试会话护栏：把 `create_app()` 的默认落点整体移出仓库 `data/`

工厂支持目录注入 ≠ 测试真的注入了。历史上多个用例写成
`create_app()` + 事后改 `app.config["DB_PATH"]`：等它改的时候，工厂已经拿
仓库 `data/platform.db`（用户生产库）跑过一遍 `init_db` 迁移 + 自动版本回填 +
admin 引导，导出接口也顺着默认 `DATA_DIR` 覆盖了 `data/export/config/`。
这类副作用不会让任何用例失败，只会静默改坏用户数据。

护栏做法：会话开始把 `webapp.DATA_DIR` / `webapp.CONFIG_DIR` 指向 pytest 临时目录，
结束再还原。于是"忘传目录"最坏也只污染临时目录。
注意两点边界：
  1. 需要断言生产缺省落点的用例请显式传参，或断言 `webapp.DATA_DIR` 还原后的值；
  2. 路由内 `current_app.config` 缺失时的兜底常量（各自模块的 CONFIG_DIR /
     DEPLOY_ROOT）不受本护栏影响，那些路径只在无 app 上下文时才会被读到。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session", autouse=True)
def isolate_default_dirs(tmp_path_factory):
    """重定向工厂默认目录（autouse，整个会话生效）"""
    import webapp
    from webapp import task_manager

    root = tmp_path_factory.mktemp("default-dirs")
    orig_data = webapp.DATA_DIR
    orig_config = webapp.CONFIG_DIR
    orig_log = task_manager.LOG_DIR
    webapp.DATA_DIR = root / "data"
    webapp.CONFIG_DIR = root / "config"
    try:
        yield root
    finally:
        webapp.DATA_DIR = orig_data
        webapp.CONFIG_DIR = orig_config
        task_manager.set_log_dir(orig_log)
