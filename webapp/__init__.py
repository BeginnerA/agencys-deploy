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


def _resolve_secret_key(app) -> str:
    """优先环境变量；否则用 data/secret_key 持久化随机密钥；读写失败才回退开发密钥"""
    key = os.environ.get("DASHBOARD_SECRET_KEY")
    if key:
        return key
    key_file = DATA_DIR / "secret_key"
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
            "[platform] 无法读写 data/secret_key，回退内置开发密钥——生产环境请设置 DASHBOARD_SECRET_KEY")
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
    """空库时自动导入 config/ 旧 YAML，确保存在默认 admin（幂等，可自愈并发残留）"""
    from .importer import import_from_config
    results = import_from_config(app.config["DB_PATH"], CONFIG_DIR)
    ok = sum(1 for _, (success, _) in results.items() if success)
    app.logger.warning("[onboarding] 导入完成 %d/%d 文件: %s", ok, len(results), results)
    _ensure_admin(app)


def create_app():
    app = Flask(__name__)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.config["SECRET_KEY"] = _resolve_secret_key(app)
    app.config["DB_PATH"] = str(DATA_DIR / "platform.db")
    app.config["MONITOR_INTERVAL"] = int(os.environ.get("DASHBOARD_MONITOR_INTERVAL", "30"))
    init_db(app.config["DB_PATH"])
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
    app.register_blueprint(auth_bp, url_prefix="/api")
    app.register_blueprint(config_bp, url_prefix="/api")
    app.register_blueprint(deploy_bp, url_prefix="/api")
    app.register_blueprint(dash_bp, url_prefix="/api")
    app.register_blueprint(alert_bp, url_prefix="/api")
    app.register_blueprint(log_bp, url_prefix="/api")
    app.register_blueprint(deploy_enhance_bp, url_prefix="/api")

    from flask import render_template

    @app.route("/")
    def index():
        return render_template("login.html")

    @app.route("/screen")
    def screen():
        return render_template("screen.html")

    @app.route("/favicon.ico")
    def favicon():
        return "", 204

    return app
