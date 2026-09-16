"""公共配置加载与日志工具"""
import os
import sys
import yaml
import paramiko
from paramiko.ssh_exception import AuthenticationException
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parent.parent

# 全局配置目录覆盖：deploy.py/deploy_pipeline.py 以 --config-source db 运行时指向导出目录
CONFIG_DIR_OVERRIDE: Path = None


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _config_path(name: str, env: str = None) -> Path:
    """根据环境返回配置文件路径（绝对路径）；未指定环境则使用默认文件"""
    base = CONFIG_DIR_OVERRIDE if CONFIG_DIR_OVERRIDE else (DEPLOY_ROOT / "config")
    if env:
        path = base / f"{name}.{env}.yaml"
        if path.exists():
            return path
        # prod 回退到默认文件是正常语义（默认文件即生产配置），不警告；其他环境缺失才警告防误发布
        if env != "prod":
            log(f"警告: 环境配置文件 {path.name} 不存在，回退到默认 {name}.yaml（生产环境）")
    return base / f"{name}.yaml"


def load_config(env: str = None):
    if env is None:
        env = os.environ.get("PLATFORM_DEPLOY_ENV")
    path = _config_path("deploy", env)
    log(f"加载配置: {path.name}（环境: {env or '默认/生产'}）")
    return load_yaml(path)


def load_projects():
    return load_yaml(_config_path("projects"))


def load_pipelines():
    return load_yaml(_config_path("pipelines"))


def default_servers_file(env: str = None) -> str:
    """返回默认服务器清单文件的绝对路径（db 模式时位于运行时导出目录内）"""
    return str(_config_path("servers", env))


def get_project_config(project_name: str) -> dict:
    projects = load_projects()
    cfg = projects["projects"].get(project_name)
    if not cfg:
        raise ValueError(f"projects.yaml 中未找到项目: {project_name}")
    return cfg


def get_pipeline_config(pipeline_name: str) -> dict:
    pipelines = load_pipelines()
    cfg = pipelines["pipelines"].get(pipeline_name)
    if not cfg:
        raise ValueError(f"pipelines.yaml 中未找到流水线: {pipeline_name}")
    return cfg


def env_or_config(config_value: str, env_name: str) -> str:
    """优先从环境变量读取敏感信息，否则使用配置文件值"""
    return os.environ.get(env_name) or config_value or ""


def get_registry_auth(config: dict):
    return {
        "host": config["registry"]["host"],
        "namespace": config["registry"]["namespace"],
        "username": env_or_config(config["registry"].get("username"), "REGISTRY_USERNAME"),
        "password": env_or_config(config["registry"].get("password"), "REGISTRY_PASSWORD"),
    }


def get_registry_mode(config: dict) -> str:
    """镜像分发模式: push = 推送到远程仓库; direct = scp 直传（无需镜像仓库）"""
    mode = (config.get("registry") or {}).get("mode", "push")
    if mode not in ("push", "direct"):
        fail(f"registry.mode 配置非法: {mode}（可选值: push / direct）")
    return mode


def get_nacos_auth(config: dict):
    """Nacos 连接信息；url 为空表示环境未接入 Nacos（发布时跳过全部 Nacos 操作）"""
    nacos_cfg = config.get("nacos") or {}
    return {
        "url": (nacos_cfg.get("url") or "").rstrip("/"),
        "namespace": nacos_cfg.get("namespace") or "",
        "group": nacos_cfg.get("group") or "DEFAULT_GROUP",
        "username": env_or_config(nacos_cfg.get("username"), "NACOS_USERNAME"),
        "password": env_or_config(nacos_cfg.get("password"), "NACOS_PASSWORD"),
    }


def get_mysql_auth(config: dict):
    return {
        "host": config["mysql_snailjob"]["host"],
        "port": config["mysql_snailjob"].get("port", 3306),
        "database": config["mysql_snailjob"]["database"],
        "username": env_or_config(config["mysql_snailjob"].get("username"), "MYSQL_USERNAME"),
        "password": env_or_config(config["mysql_snailjob"].get("password"), "MYSQL_PASSWORD"),
    }


def get_ssh_config(config: dict):
    return {
        "user": config["ssh"]["user"],
        "key_file": Path(config["ssh"]["key_file"]).expanduser(),
        "port": config["ssh"].get("port", 22),
        "password": env_or_config(config["ssh"].get("password"), "SSH_PASSWORD"),
    }


def connect_ssh(client: paramiko.SSHClient, host: str, ssh_cfg: dict, timeout: int = 15):
    """建立 SSH 连接，密码优先、密钥回退。

    配置了密码（或环境变量 SSH_PASSWORD）时先尝试密码认证；
    密码认证失败自动回退到密钥认证；未配置密码时保持纯密钥连接。
    网络类异常（超时、拒绝连接等）直接抛出，不做回退。
    """
    password = ssh_cfg.get("password") or None
    if password:
        try:
            client.connect(
                hostname=host,
                port=ssh_cfg["port"],
                username=ssh_cfg["user"],
                password=password,
                allow_agent=False,
                look_for_keys=False,
                timeout=timeout,
            )
            return
        except AuthenticationException as e:
            log(f"[{host}] SSH 密码认证失败: {e}，回退密钥认证")
    key_file = ssh_cfg.get("key_file")
    connect_kwargs = dict(
        hostname=host,
        port=ssh_cfg["port"],
        username=ssh_cfg["user"],
        timeout=timeout,
    )
    if key_file:
        key_file = Path(key_file)
        if not key_file.exists():
            raise FileNotFoundError(f"[{host}] SSH 密钥文件不存在: {key_file}")
        connect_kwargs["key_filename"] = str(key_file)
    client.connect(**connect_kwargs)


def merge_server_ssh(base_ssh: dict, server: dict) -> dict:
    """合并服务器清单中可选的 ssh 覆盖配置，未写的字段继承全局。

    例：servers.yaml 中某台服务器配置了 ssh.user/ssh.password，
    则仅这两个字段覆盖全局；其余（key_file、port）沿用全局值。
    """
    override = server.get("ssh") or {}
    merged = dict(base_ssh)
    for key in ("user", "password", "key_file", "port"):
        if key in override and override[key] is not None:
            merged[key] = Path(override[key]).expanduser() if key == "key_file" else override[key]
    return merged


def log(msg: str):
    print(f"[platform] {msg}", flush=True)


def fail(msg: str, code: int = 1):
    print(f"[platform] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)
