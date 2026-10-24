"""智能运维平台应用工厂：SQLite + 首启引导 + 蓝图装配"""
import os
import secrets
import time
from pathlib import Path

from flask import Flask

from .db import init_db, project_count
from . import db as dbm

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


# 内置开发密钥：仅当 data/secret_key 读写失败时回退（生产须设 DASHBOARD_SECRET_KEY）
DEV_SECRET_KEY = "platform-dev-key"


def _resolve_secret_key(app, data_dir: Path) -> str:
    """优先环境变量；否则用 <data_dir>/secret_key 持久化随机密钥；读写失败才回退开发密钥"""
    key = os.environ.get("DASHBOARD_SECRET_KEY")
    if key:
        return key
    key_file = data_dir / "secret_key"
    try:
        if not key_file.exists():
            try:
                # O_EXCL 原子创建：并发首启时仅一个进程写入，其余进程读已生成的文件
                fd = os.open(key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_hex(32))
            except FileExistsError:
                pass
        return key_file.read_text().strip()
    except OSError:
        app.logger.warning(
            "[platform] 无法读写 %s，回退内置开发密钥——生产环境请设置 DASHBOARD_SECRET_KEY",
            key_file)
        return DEV_SECRET_KEY


def _ensure_admin(app) -> bool:
    """admin 缺失时补建（幂等，可自愈并发残留）；返回 True 表示本次新建"""
    from werkzeug.security import generate_password_hash
    if dbm.get_user_by_name(app.config["DB_PATH"], "admin") is not None:
        return False
    initial_pwd = os.environ.get("DASHBOARD_PASSWORD") or "admin"
    try:
        dbm.create_record(app.config["DB_PATH"], "users", dict(
            username="admin",
            password_hash=generate_password_hash(initial_pwd),
            role="admin", must_change_password=1, enabled=1,
            created_at=time.time()))
    except dbm.sqlite3.IntegrityError:
        # 多 worker 并发首启：另一进程已创建，直接跳过即可
        app.logger.warning("[onboarding] admin 已由并发进程创建，跳过")
        return False
    app.config["ONBOARDED_ADMIN"] = True
    app.logger.warning(
        "[onboarding] 已创建默认管理员 admin（初始密码见 DASHBOARD_PASSWORD，默认值见 README），首次登录强制改密")
    return True


def _onboarding(app) -> None:
    """空库时自动导入 app.config['CONFIG_DIR'] 旧 YAML，确保存在默认 admin（幂等，可自愈并发残留）"""
    from .importer import import_from_config
    results = import_from_config(app.config["DB_PATH"], Path(app.config["CONFIG_DIR"]))
    ok = sum(1 for _, (success, _) in results.items() if success)
    app.logger.warning("[onboarding] 导入完成 %d/%d 文件: %s", ok, len(results), results)
    _ensure_admin(app)


def create_app(data_dir=None, config_dir=None, log_dir=None):
    """应用工厂。

    三个目录参数为测试/多实例隔离而设：
      - data_dir：DB、secret_key、导出物、日志的根（默认模块 DATA_DIR）
      - config_dir：首启引导与 /api/config/import 的 YAML 来源（默认模块 CONFIG_DIR）
      - log_dir：任务/采集日志目录（默认 data_dir/logs）

    缺省行为与原实现一致（读模块 DATA_DIR / CONFIG_DIR），因此生产启动无需传参；
    传入时不改写 DATA_DIR / CONFIG_DIR 这两个模块全局，避免测试之间互相污染。
    唯一的例外是 `task_manager.LOG_DIR`：日志目录同时被无 app 上下文的后台线程
    （发布调度器 / 采集器 / Agent）读取，只能由 set_log_dir() 同步一次，
    因此注入 log_dir 时它会随之改变（这是有意的单实例约定）。
    """
    app = Flask(__name__)
    data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
    config_dir = Path(config_dir) if config_dir is not None else CONFIG_DIR
    log_dir = Path(log_dir) if log_dir is not None else data_dir / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config["DATA_DIR"] = str(data_dir)
    app.config["CONFIG_DIR"] = str(config_dir)
    app.config["SECRET_KEY"] = _resolve_secret_key(app, data_dir)
    app.config["DB_PATH"] = str(data_dir / "platform.db")
    # 日志目录（任务执行/采集/归档/手动清理共用；独立成配置便于测试隔离）
    app.config["LOG_DIR"] = str(log_dir)
    app.config["MONITOR_INTERVAL"] = int(os.environ.get("DASHBOARD_MONITOR_INTERVAL", "30"))
    init_db(app.config["DB_PATH"])
    # 任务日志落点交给执行器模块：后台线程（调度器/Agent）无 app 上下文，
    # 只能从这里取得目录，否则会把日志写进仓库默认 data/logs
    from .task_manager import set_log_dir
    set_log_dir(log_dir)
    # 历史自动版本任务回填：从日志解析实际版本号写入 DB（幂等，仅处理 version 为空的记录）
    from .task_manager import backfill_existing_auto_versions
    backfill_existing_auto_versions(app.config["DB_PATH"])
    # 空库引导（导入 config/ 旧 YAML + 建 admin）；
    # 非空库但 admin 缺失时仅补建 admin，不重导入（避免 config/ 旧文件覆盖 DB 权威配置）
    if project_count(app.config["DB_PATH"]) == 0:
        _onboarding(app)
    else:
        _ensure_admin(app)

    from .routes.auth_routes import auth_bp
    from .routes.config_routes import config_bp
    from .routes.deploy_routes import deploy_bp
    from .routes.dash_routes import dash_bp
    from .routes.alert_routes import alert_bp
    from .routes.log_routes import log_bp
    from .routes.deploy_enhance_routes import deploy_enhance_bp
    from .routes.stream_routes import stream_bp
    app.register_blueprint(auth_bp, url_prefix="/api")
    app.register_blueprint(config_bp, url_prefix="/api")
    app.register_blueprint(deploy_bp, url_prefix="/api")
    app.register_blueprint(dash_bp, url_prefix="/api")
    app.register_blueprint(alert_bp, url_prefix="/api")
    app.register_blueprint(log_bp, url_prefix="/api")
    app.register_blueprint(deploy_enhance_bp, url_prefix="/api")
    app.register_blueprint(stream_bp, url_prefix="/api")
    # Agent 模块（Phase 1-4）：对话 / 审批单 / 决策建议 / 故障案例
    from agent.routes import register_agent
    register_agent(app)

    from flask import redirect, render_template

    @app.route("/")
    def index():
        return render_template("login.html")

    @app.route("/screen")
    def screen():
        return render_template("screen.html")

    @app.route("/favicon.ico")
    def favicon():
        # 模板已声明 SVG favicon；此路由仅为兼容老客户端/直连 .ico 的请求
        return redirect("/static/favicon.svg")

    return app
