"""敏感字段脱敏：读接口只回显掩码，写接口不把掩码占位符落库。

约定：
- 读：所有含 ``password``/``token``/``secret`` 等字段的配置在输出前递归脱敏；
- 写：更新时值为 ``***``（或字段缺省）表示沿用旧值，空串表示显式清除；
- 新建/克隆时 ``***`` 视为未提供，避免把占位符当成真实凭据保存。
"""

MASK = "***"

# 精确字段名（大小写不敏感）
_SECRET_KEYS = frozenset({
    "password", "secret", "token",
    "access_token", "refresh_token",
    "access_key", "secret_key", "private_key", "api_key",
})
_SECRET_SUFFIXES = ("_password", "_secret", "_token", "_api_key",
                    "_access_key", "_secret_key")


def _is_secret_key(key) -> bool:
    name = str(key).lower()
    return name in _SECRET_KEYS or name.endswith(_SECRET_SUFFIXES)


def redact_secrets(value):
    """递归遮蔽敏感字段；``None``/空串保持原样（表示未配置）"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _is_secret_key(k) and v not in (None, ""):
                out[k] = MASK
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    return value


def strip_masked_secrets(value):
    """移除 ``***`` 占位符（新建/克隆场景：占位符不是真实凭据）"""
    if isinstance(value, dict):
        return {k: strip_masked_secrets(v) for k, v in value.items()
                if v != MASK}
    if isinstance(value, (list, tuple)):
        return [strip_masked_secrets(v) for v in value]
    return value


def preserve_masked_secrets(new_block, old_block):
    """更新配置块时保留未回填的敏感字段（缺省或 ``***`` → 沿用旧值）"""
    new_block = strip_masked_secrets(new_block)
    if not isinstance(new_block, dict) or not isinstance(old_block, dict):
        return new_block
    out = dict(new_block)
    for k, old_value in old_block.items():
        if _is_secret_key(k) and k not in out:
            out[k] = old_value
    return out
