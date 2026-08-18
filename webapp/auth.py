"""多用户 JWT 认证：PBKDF2 哈希 + 角色装饰器"""
import time
from functools import wraps

import jwt
from flask import Blueprint, request, jsonify, current_app, g
from werkzeug.security import generate_password_hash, check_password_hash

from . import db as dbm

auth_bp = Blueprint("auth", __name__)

ROLES = ("admin", "operator", "viewer")

# 登录限流（进程内存）：连续失败 MAX_LOGIN_FAILS 次锁定 LOCK_SECONDS 秒
MAX_LOGIN_FAILS = 5
LOCK_SECONDS = 300
_LOGIN_FAILS: dict = {}
# 失败跟踪条目上限：防止海量不存在的用户名使内存无界增长
_MAX_FAIL_TRACK = 10000

# 用户不存在时也执行一次比较，抹平时序差异（防用户名枚举）
_DUMMY_HASH = generate_password_hash("platform-dummy-placeholder")


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def check_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def _sign_token(user: dict, secret: str) -> str:
    # sub 必须是字符串（RFC 7519；PyJWT >= 2.10 对非字符串 sub 解码报 InvalidSubjectError）
    payload = {"sub": str(user["id"]), "name": user["username"],
               "role": user["role"], "exp": int(time.time()) + 12 * 3600}
    return jwt.encode(payload, secret, algorithm="HS256")


def _load_user_from_token(token: str):
    """解析 JWT 并加载用户；token 非法或用户被禁用时返回 None（DB 故障照常上抛）"""
    try:
        payload = jwt.decode(token, current_app.config["SECRET_KEY"],
                             algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    try:
        uid = int(payload.get("sub", 0))
    except (TypeError, ValueError):
        return None
    user = dbm.get_record(current_app.config["DB_PATH"], "users", uid)
    if not user or not user["enabled"]:
        return None
    return user


def _login_key(username: str) -> str:
    """限流维度：IP + 用户名（防攻击者换用户名绕过单用户爆破）"""
    ip = request.remote_addr or "?"
    return f"{ip}:{username}"


def _login_blocked(username: str) -> bool:
    """登录失败计数限流；返回 True 表示当前锁定中"""
    rec = _LOGIN_FAILS.get(_login_key(username))
    if rec and rec["lock_until"] > time.time():
        return True
    return False


def _record_login_fail(username: str) -> bool:
    """记录一次失败；返回 True 表示本次已达锁定阈值"""
    now = time.time()
    key = _login_key(username)
    if len(_LOGIN_FAILS) > _MAX_FAIL_TRACK:
        # 超出跟踪上限：清理未锁定/已过期的条目，防内存无界增长
        stale = [k for k, v in _LOGIN_FAILS.items()
                 if not v["lock_until"] or v["lock_until"] <= now]
        for k in stale:
            _LOGIN_FAILS.pop(k, None)
    rec = _LOGIN_FAILS.get(key)
    if not rec:
        rec = {"count": 0, "lock_until": 0}
        _LOGIN_FAILS[key] = rec
    elif rec["lock_until"] and rec["lock_until"] <= now:
        # 上一轮锁定已过期，开启新一轮计数
        rec["count"] = 0
        rec["lock_until"] = 0
    rec["count"] += 1
    if rec["count"] >= MAX_LOGIN_FAILS:
        rec["lock_until"] = now + LOCK_SECONDS
        return True
    return False


def _clear_login_fails(username: str):
    _LOGIN_FAILS.pop(_login_key(username), None)


def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        user = _load_user_from_token(token)
        if not user:
            return jsonify({"error": "未登录或登录已过期"}), 401
        # 首次登录强制改密（服务端强制）：仅放行 me 与改密接口
        if user["must_change_password"] and request.endpoint not in (
                "auth.me", "auth.change_password"):
            return jsonify({"error": "首次登录须修改密码"}), 403
        g.user = user
        return f(*args, **kwargs)
    return wrapper


def require_role(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # 防御：漏写 @require_auth 时返回 401 而非 AttributeError 500
            if not getattr(g, "user", None):
                return jsonify({"error": "未登录或登录已过期"}), 401
            if g.user["role"] not in roles:
                return jsonify({"error": "无权限"}), 403
            return f(*args, **kwargs)
        return wrapper
    return deco


def audit(action: str, target_type: str = "", target_id: int = 0,
          detail: dict = None):
    """在 require_auth 视图中写审计日志"""
    user = getattr(g, "user", None) or {}
    dbm.create_audit(current_app.config["DB_PATH"], user.get("id", 0),
                     user.get("username", ""), action, target_type,
                     target_id, detail or {})
