"""认证与用户路由"""
import time

from flask import Blueprint, request, jsonify, current_app, g

from .. import db as dbm
from ..auth import (auth_bp, require_auth, require_role, hash_password,
                    check_password, _sign_token, audit, _login_blocked,
                    _record_login_fail, _clear_login_fails, _DUMMY_HASH)

# ---------- 认证 ----------


def _json_body():
    """解析 JSON 请求体；非对象（数组/字符串/null/解析失败）返回 None"""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


@auth_bp.route("/auth/login", methods=["POST"])
def login():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    username = body.get("username", "")
    password = body.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        return jsonify({"error": "参数类型错误"}), 400
    username = username.strip()
    if _login_blocked(username):
        return jsonify({"error": "登录失败次数过多，请稍后再试"}), 429
    user = dbm.get_user_by_name(current_app.config["DB_PATH"], username)
    if not user:
        # 不存在也执行一次比较，抹平时序差异（防用户名枚举）
        check_password(password, _DUMMY_HASH)
        _record_login_fail(username)
        dbm.create_audit(current_app.config["DB_PATH"], 0, username,
                         "login_failed", "user", 0, {})
        return jsonify({"error": "用户名或密码错误"}), 401
    if not user["enabled"] or not check_password(password, user["password_hash"]):
        _record_login_fail(username)
        dbm.create_audit(current_app.config["DB_PATH"], 0, username,
                         "login_failed", "user", 0, {})
        return jsonify({"error": "用户名或密码错误"}), 401
    _clear_login_fails(username)
    dbm.create_audit(current_app.config["DB_PATH"], user["id"], user["username"],
                     "login", "user", user["id"], {})
    return jsonify({"token": _sign_token(user, current_app.config["SECRET_KEY"]),
                    "user": {"id": user["id"], "username": user["username"],
                             "role": user["role"],
                             "must_change_password": user["must_change_password"]}})


@auth_bp.route("/auth/me")
@require_auth
def me():
    user = g.user
    return jsonify({"id": user["id"], "username": user["username"],
                    "role": user["role"],
                    "must_change_password": user["must_change_password"]})


@auth_bp.route("/auth/password", methods=["POST"])
@require_auth
def change_password():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    old = body.get("old_password", "")
    new = body.get("new_password", "")
    if not isinstance(old, str) or not isinstance(new, str):
        return jsonify({"error": "参数类型错误"}), 400
    if len(new) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    user = g.user
    if not check_password(old, user["password_hash"]):
        return jsonify({"error": "原密码错误"}), 400
    dbm.update_record(current_app.config["DB_PATH"], "users", user["id"], dict(
        password_hash=hash_password(new), must_change_password=0))
    audit("change_password", "user", user["id"], {})
    return jsonify({"ok": True})


# ---------- 用户管理（admin） ----------

@auth_bp.route("/users", methods=["GET"])
@require_auth
@require_role("admin")
def list_users():
    users = dbm.list_users(current_app.config["DB_PATH"])
    return jsonify([{k: u[k] for k in ("id", "username", "role", "enabled",
                                       "must_change_password")} for u in users])


@auth_bp.route("/users", methods=["POST"])
@require_auth
@require_role("admin")
def create_user():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    username = body.get("username", "")
    password = body.get("password", "")
    role = body.get("role", "viewer")
    if not isinstance(username, str) or not isinstance(password, str) \
            or not isinstance(role, str):
        return jsonify({"error": "参数类型错误"}), 400
    username = username.strip()
    if not username or len(password) < 6:
        return jsonify({"error": "用户名必填，密码至少 6 位"}), 400
    if role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "角色非法"}), 400
    if dbm.get_user_by_name(current_app.config["DB_PATH"], username):
        return jsonify({"error": "用户名已存在"}), 400
    uid = dbm.create_record(current_app.config["DB_PATH"], "users", dict(
        username=username, password_hash=hash_password(password), role=role,
        must_change_password=1, enabled=1,
        created_at=time.time()))
    audit("create_user", "user", uid, {"username": username, "role": role})
    return jsonify({"id": uid}), 201


@auth_bp.route("/users/<int:uid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_user(uid):
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    user = dbm.get_record(current_app.config["DB_PATH"], "users", uid)
    if not user:
        return jsonify({"error": "用户不存在"}), 404
    if uid == g.user["id"] and body.get("role") and body["role"] != "admin":
        return jsonify({"error": "不能降级自己"}), 400
    if uid == g.user["id"] and body.get("enabled") is False:
        return jsonify({"error": "不能禁用自己"}), 400
    fields = {}
    if "role" in body:
        if body["role"] not in ("admin", "operator", "viewer"):
            return jsonify({"error": "角色非法"}), 400
        fields["role"] = body["role"]
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            return jsonify({"error": "enabled 必须为布尔值"}), 400
        fields["enabled"] = 1 if body["enabled"] else 0
    if "password" in body:
        if not isinstance(body["password"], str) or len(body["password"]) < 6:
            return jsonify({"error": "密码至少 6 位"}), 400
        fields["password_hash"] = hash_password(body["password"])
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "users", uid, fields)
    # 审计不落凭据材料（password_hash 过滤）
    audit("update_user", "user", uid,
          {k: v for k, v in fields.items() if k != "password_hash"})
    return jsonify({"ok": True})


@auth_bp.route("/users/<int:uid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_user(uid):
    if uid == g.user["id"]:
        return jsonify({"error": "不能删除自己"}), 400
    if not dbm.delete_record(current_app.config["DB_PATH"], "users", uid):
        return jsonify({"error": "用户不存在"}), 404
    audit("delete_user", "user", uid, {})
    return jsonify({"ok": True})
