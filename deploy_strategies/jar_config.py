"""jar_service 运行时配置契约：解析、校验、产物命名与校验和

纯标准库实现，CLI 子进程（策略层/构建）与 Web 路由共用同一份规则——
校验口径只允许有一个来源，避免「前端允许、后端拒绝」或反之。
"""
import hashlib
import json
import re

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

DEFAULTS = {
    "java_bin": "/usr/bin/java",
    "stop_timeout": 60,
    "start_timeout": 120,
    "artifact_pattern": "target/*.jar",
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
}


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_runtime_config(raw) -> dict:
    """把 DB/YAML 中的 JSON（dict 或字符串）归一为策略可直接消费的完整结构"""
    cfg = _as_dict(raw)
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    return {
        "app_home": str(cfg.get("app_home") or "").rstrip("/"),
        "java_bin": str(cfg.get("java_bin") or DEFAULTS["java_bin"]),
        "jvm_opts": str(cfg.get("jvm_opts") or ""),
        "app_args": str(cfg.get("app_args") or ""),
        "run_user": str(cfg.get("run_user") or ""),
        "work_dir": str(cfg.get("work_dir") or ""),
        "unit_name": str(cfg.get("unit_name") or ""),
        "port": _as_int(cfg.get("port"), 0),
        "readiness": {
            "type": str(rt.get("type") or DEFAULTS["readiness"]["type"]),
            "path": str(rt.get("path") or DEFAULTS["readiness"]["path"]),
            "timeout": _as_int(rt.get("timeout"), DEFAULTS["readiness"]["timeout"]),
            "interval": _as_int(rt.get("interval"), DEFAULTS["readiness"]["interval"]),
        },
        "stop_timeout": _as_int(cfg.get("stop_timeout"), DEFAULTS["stop_timeout"]),
        "start_timeout": _as_int(cfg.get("start_timeout"), DEFAULTS["start_timeout"]),
        "artifact_pattern": str(cfg.get("artifact_pattern") or DEFAULTS["artifact_pattern"]),
        "env_files": [str(p) for p in (cfg.get("env_files") or [])
                      if isinstance(p, str) and p],
    }


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """返回错误消息列表；仅 jar_service 需要校验（其它方式配置原样忽略）"""
    if (deploy_mode or "container") != "jar_service":
        return []
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return ["deploy_mode=jar_service 必须配置 runtime_config.app_home"]
    errors = []
    app_home = cfg.get("app_home")
    if not isinstance(app_home, str) or not app_home.startswith("/"):
        errors.append("runtime_config.app_home 必填，且必须是绝对路径")
    java_bin = cfg.get("java_bin", DEFAULTS["java_bin"])
    if not isinstance(java_bin, str) or not java_bin.startswith("/"):
        errors.append("runtime_config.java_bin 必须是绝对路径")
    unit_name = cfg.get("unit_name") or ""
    if unit_name and (not isinstance(unit_name, str) or not _UNIT_RE.match(unit_name)):
        errors.append("runtime_config.unit_name 只能包含字母、数字与 _ . @ -")
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rtype = rt.get("type") or DEFAULTS["readiness"]["type"]
    if rtype not in ("http", "tcp", "process"):
        errors.append("runtime_config.readiness.type 仅支持 http / tcp / process")
    port = cfg.get("port", 0)
    if rtype in ("http", "tcp"):
        if not isinstance(port, int) or isinstance(port, bool) or not (0 < port < 65536):
            errors.append("runtime_config.port 必填，且必须是 1-65535 的整数")
    elif port not in (0, None) and (not isinstance(port, int) or isinstance(port, bool)
                                    or not (0 < port < 65536)):
        errors.append("runtime_config.port 必须是 1-65535 的整数")
    for key in ("timeout", "interval"):
        value = rt.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value <= 0):
            errors.append(f"runtime_config.readiness.{key} 必须为正整数")
    for key in ("stop_timeout", "start_timeout"):
        value = cfg.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value <= 0):
            errors.append(f"runtime_config.{key} 必须为正整数")
    pattern = cfg.get("artifact_pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        errors.append("runtime_config.artifact_pattern 不能为空")
    env_files = cfg.get("env_files") or []
    if not isinstance(env_files, list) or any(
            not isinstance(p, str) or not p.startswith("/") for p in env_files):
        errors.append("runtime_config.env_files 必须是绝对路径数组")
    return errors


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
