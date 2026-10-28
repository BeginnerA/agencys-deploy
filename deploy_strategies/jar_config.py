"""jar_service 运行时配置契约：Java 专属字段、产物命名

公共字段解析/校验、sha256、单元名派生已上移到 process_config.py（进程族共享层）；
本模块保留 Java 专属部分并维持既有公共函数签名不变——既有导入
（deploy_strategies.jar_config import ...）不受影响。
"""
from .process_config import (derive_unit_name,  # noqa: F401 — re-export 兼容
                             parse_common, sha256_file,  # noqa: F401 — re-export
                             validate_common, _as_dict, _as_int)

DEFAULTS = {
    "java_bin": "/usr/bin/java",
    "stop_timeout": 60,
    "start_timeout": 120,
    "artifact_pattern": "target/*.jar",
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
}


def parse_runtime_config(raw) -> dict:
    """把 DB/YAML 中的 JSON（dict 或字符串）归一为策略可直接消费的完整结构"""
    cfg = _as_dict(raw)
    out = parse_common(cfg, "jar_service")
    out["java_bin"] = str(cfg.get("java_bin") or DEFAULTS["java_bin"])
    out["jvm_opts"] = str(cfg.get("jvm_opts") or "")
    out["artifact_pattern"] = str(cfg.get("artifact_pattern")
                                  or DEFAULTS["artifact_pattern"])
    return out


def validate_jar_config(raw) -> list:
    """jar 专属校验（Java 字段）；公共字段委托 process_config.validate_common"""
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return ["deploy_mode=jar_service 必须配置 runtime_config.app_home"]
    errors = validate_common(cfg, "jar_service")
    java_bin = cfg.get("java_bin", DEFAULTS["java_bin"])
    if not isinstance(java_bin, str) or not java_bin.startswith("/"):
        errors.append("runtime_config.java_bin 必须是绝对路径")
    pattern = cfg.get("artifact_pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        errors.append("runtime_config.artifact_pattern 不能为空")
    return errors


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """兼容入口：全量分派给 process_config（jar/Go/Rust/Node/Python）"""
    from .process_config import validate_runtime_config as validate
    return validate(raw, deploy_mode)


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"
