"""Agent 模块配置：LLM 网关与运行参数

读取优先级：环境变量（AGENT_LLM_*）> platform_settings 表（agent_llm_* 键）> 默认值。
敏感信息（api_key）只经环境变量/DB 读取，绝不写入日志与工具输出。
"""
import os
import time

# 环境变量前缀
_ENV_PREFIX = "AGENT_LLM_"
# platform_settings 键前缀（admin 可在配置管理/直接 SQL 中维护）
_DB_PREFIX = "agent_llm_"

DEFAULTS = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "temperature": 0.2,
    "timeout": 60,
    "max_tool_rounds": 8,
    "max_result_chars": 6000,
}


def _env(name: str) -> str:
    return os.environ.get(_ENV_PREFIX + name, "").strip()


def _bool_env(name: str, default: bool = False) -> bool:
    val = _env(name).lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _db_settings(db_path: str = None) -> dict:
    """从 platform_settings 表读取 agent_llm_* 键（DB 不可用/无记录时返回空）"""
    if not db_path:
        return {}
    try:
        from webapp import db as dbm
        rows = dbm.list_records(db_path, "platform_settings")
    except Exception:
        return {}
    out = {}
    for r in rows:
        key = r.get("key", "")
        if key.startswith(_DB_PREFIX):
            out[key[len(_DB_PREFIX):]] = r.get("value", "")
    return out


def get_llm_settings(db_path: str = None) -> dict:
    """聚合后的 LLM 网关配置；显式环境变量优先于 DB 配置"""
    db_cfg = _db_settings(db_path)
    def pick(name: str) -> str:
        if _env(name):
            return _env(name)
        return str(db_cfg.get(name, DEFAULTS[name]))
    return {
        "base_url": pick("base_url"),
        "api_key": pick("api_key"),
        "model": pick("model"),
        "temperature": float(pick("temperature") or DEFAULTS["temperature"]),
        "timeout": int(float(pick("timeout") or DEFAULTS["timeout"])),
    }


def llm_available(db_path: str = None) -> bool:
    """网关是否可用：base_url 与 api_key 齐备即可（model 缺省由服务端决定）"""
    s = get_llm_settings(db_path)
    return bool(s["base_url"] and s["api_key"])


def get_max_tool_rounds() -> int:
    val = os.environ.get("AGENT_MAX_TOOL_ROUNDS", "").strip()
    try:
        return max(1, min(20, int(val))) if val else DEFAULTS["max_tool_rounds"]
    except ValueError:
        return DEFAULTS["max_tool_rounds"]


def get_max_result_chars() -> int:
    val = os.environ.get("AGENT_MAX_RESULT_CHARS", "").strip()
    try:
        return max(500, min(20000, int(val))) if val else DEFAULTS["max_result_chars"]
    except ValueError:
        return DEFAULTS["max_result_chars"]


def save_llm_settings(db_path: str, base_url: str = "", api_key: str = "",
                      model: str = "") -> None:
    """将网关配置持久化到 platform_settings（空值不覆盖已有配置）"""
    from webapp import db as dbm
    now = int(time.time())
    for key, val in (("base_url", base_url), ("api_key", api_key),
                     ("model", model)):
        if not val:
            continue
        rows = dbm.list_records(db_path, "platform_settings",
                                "key=?", (_DB_PREFIX + key,))
        if rows:
            dbm.update_record(db_path, "platform_settings", rows[0]["id"],
                              {"value": val, "updated_at": now})
        else:
            dbm.create_record(db_path, "platform_settings", dict(
                key=_DB_PREFIX + key, value=val, updated_at=now))
