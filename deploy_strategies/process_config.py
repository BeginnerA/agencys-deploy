"""进程族（jar_service/go_binary/rust_binary/node_process/python_process）
运行时配置共享层：公共字段解析、按模式分派的解析/校验、ExecStart 拼装与接管特征串。

纯标准库实现，CLI 子进程（策略层/构建）与 Web 路由共用同一份规则——校验口径只允许
一个来源。jar 专属字段与缺省值留在 jar_config.py（公共函数签名不变），本模块按
deploy_mode 分派并惰性导入，避免 jar_config ↔ process_config 的模块级循环。
"""
import hashlib
import json
import re

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_PYVER_RE = re.compile(r"^\d+\.\d+$")

PROCESS_MODES = ("jar_service", "go_binary", "rust_binary",
                 "node_process", "python_process")

# readiness 缺省按模式分派：jar 维持首期行为（Actuator 语义）；其余模式没有
# 通用健康端点，缺省只判进程存活，有端点的服务应显式配置 http/tcp + port
_READINESS_JAR = {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5}
_READINESS_BASE = {"type": "process", "path": "", "timeout": 90, "interval": 5}


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


def _readiness_default(mode: str) -> dict:
    return dict(_READINESS_JAR if mode == "jar_service" else _READINESS_BASE)


def parse_common(cfg: dict, mode: str) -> dict:
    """进程族公共字段（五种模式同构）；jar 专属键由 jar_config.parse 追加"""
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rd = _readiness_default(mode)
    return {
        "app_home": str(cfg.get("app_home") or "").rstrip("/"),
        "run_user": str(cfg.get("run_user") or ""),
        "work_dir": str(cfg.get("work_dir") or ""),
        "unit_name": str(cfg.get("unit_name") or ""),
        "port": _as_int(cfg.get("port"), 0),
        "app_args": str(cfg.get("app_args") or ""),
        "readiness": {
            "type": str(rt.get("type") or rd["type"]),
            "path": str(rt.get("path") or rd["path"]),
            "timeout": _as_int(rt.get("timeout"), rd["timeout"]),
            "interval": _as_int(rt.get("interval"), rd["interval"]),
        },
        "stop_timeout": _as_int(cfg.get("stop_timeout"), 60),
        "start_timeout": _as_int(cfg.get("start_timeout"), 120),
        "env_files": [str(p) for p in (cfg.get("env_files") or [])
                      if isinstance(p, str) and p],
    }


def parse_runtime_config(raw, deploy_mode: str = "jar_service") -> dict:
    """按 deploy_mode 分派；未知模式返回 {}（container/static 不消费该配置）"""
    mode = deploy_mode or "container"
    if mode == "jar_service":
        from .jar_config import parse_runtime_config as parse_jar
        return parse_jar(raw)
    if mode not in ("go_binary", "rust_binary", "node_process", "python_process"):
        return {}
    cfg = _as_dict(raw)
    out = parse_common(cfg, mode)
    if mode == "node_process":
        out["node_bin"] = str(cfg.get("node_bin") or "/usr/bin/node")
        out["start_file"] = str(cfg.get("start_file") or "")
    elif mode == "python_process":
        out["start_file"] = str(cfg.get("start_file") or "")
        out["requirements"] = str(cfg.get("requirements") or "requirements.txt")
        out["python_version"] = str(cfg.get("python_version") or "")
    return out


def validate_common(cfg: dict, mode: str) -> list:
    """进程族公共字段校验（cfg 非空前提下调用）"""
    errors = []
    app_home = cfg.get("app_home")
    if not isinstance(app_home, str) or not app_home.startswith("/"):
        errors.append("runtime_config.app_home 必填，且必须是绝对路径")
    unit_name = cfg.get("unit_name") or ""
    if unit_name and (not isinstance(unit_name, str) or not _UNIT_RE.match(unit_name)):
        errors.append("runtime_config.unit_name 只能包含字母、数字与 _ . @ -")
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rtype = rt.get("type") or _readiness_default(mode)["type"]
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
    env_files = cfg.get("env_files") or []
    if not isinstance(env_files, list) or any(
            not isinstance(p, str) or not p.startswith("/") for p in env_files):
        errors.append("runtime_config.env_files 必须是绝对路径数组")
    return errors


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """按 deploy_mode 分派；非进程族方式配置原样忽略（返回 []）"""
    mode = deploy_mode or "container"
    if mode == "jar_service":
        from .jar_config import validate_jar_config
        return validate_jar_config(raw)
    if mode not in ("go_binary", "rust_binary", "node_process", "python_process"):
        return []
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return [f"deploy_mode={mode} 必须配置 runtime_config.app_home"]
    errors = validate_common(cfg, mode)
    if mode in ("node_process", "python_process"):
        sf = cfg.get("start_file")
        if (not isinstance(sf, str) or not sf or sf.startswith("/")
                or ".." in sf.split("/") or any(c.isspace() for c in sf)):
            errors.append("runtime_config.start_file 必填，且为 CURRENT 下的相对路径"
                          "（不含空白与 .. 段）")
        if mode == "node_process":
            node_bin = cfg.get("node_bin", "/usr/bin/node")
            if not isinstance(node_bin, str) or not node_bin.startswith("/"):
                errors.append("runtime_config.node_bin 必须是绝对路径")
            elif any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F
                     for c in node_bin):
                # node_bin 会拼进 systemd unit 与 shell 命令，空白/控制字符
                # 会改变命令结构
                errors.append("runtime_config.node_bin 必须是绝对路径，"
                              "且不含空白与控制字符")
        else:
            pv = cfg.get("python_version")
            if pv is not None and str(pv) and not _PYVER_RE.match(str(pv)):
                errors.append("runtime_config.python_version 形如 3.11（major.minor）")
            req = cfg.get("requirements")
            if req is not None:
                if not isinstance(req, str) or not req.strip() or req.startswith("/"):
                    errors.append("runtime_config.requirements 须为项目内相对路径")
                elif (".." in req.replace("\\", "/").split("/")
                      or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F
                             for c in req)):
                    # .. 段会让构建读到 project_root 之外、目标机读到 releases/{v}
                    # 之外；空白/控制字符则会被拼进 shell 命令
                    errors.append("runtime_config.requirements 须为项目内相对路径"
                                  "（不含空白、控制字符与 .. 段）")
    return errors


def build_exec_start(cfg: dict, deploy_mode: str) -> str:
    """ExecStart 拼装（cfg 为 parse_runtime_config 的产物）；固定引用 CURRENT/venv"""
    mode = deploy_mode or "jar_service"
    curr = f"{cfg['app_home']}/CURRENT"
    if mode == "jar_service":
        return " ".join(x for x in (cfg["java_bin"], cfg["jvm_opts"],
                                    "-jar", f"{curr}/app.jar", cfg["app_args"]) if x)
    if mode in ("go_binary", "rust_binary"):
        return " ".join(x for x in (f"{curr}/app", cfg["app_args"]) if x)
    if mode == "node_process":
        return " ".join(x for x in (cfg["node_bin"], f"{curr}/{cfg['start_file']}",
                                    cfg["app_args"]) if x)
    return " ".join(x for x in (f"{cfg['app_home']}/venv/bin/python",
                                f"{curr}/{cfg['start_file']}", cfg["app_args"]) if x)


def exec_start_marker(cfg: dict, deploy_mode: str) -> str:
    """接管门槛特征串：ExecStart 必含该子串才认为是平台创建的单元"""
    mode = deploy_mode or "jar_service"
    curr = f"{cfg['app_home']}/CURRENT"
    if mode == "jar_service":
        return f"{curr}/app.jar"
    if mode in ("go_binary", "rust_binary"):
        return f"{curr}/app"
    if mode == "node_process":
        return f"{curr}/{cfg['start_file']}"
    return f"{cfg['app_home']}/venv/bin/python"


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_unit_name(runtime_config, image_name: str = "",
                     service_name: str = "") -> str:
    """平台统一口径：unit_name → image_name → 服务名 → app（不含 .service）"""
    cfg = _as_dict(runtime_config)
    return str(cfg.get("unit_name") or image_name or service_name or "app")
