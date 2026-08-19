"""日志中心 API：检索 + 上下文 + 服务列表 + 保留设置"""
import json
import os
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import db as dbm
from ..auth import audit, require_auth, require_role

log_bp = Blueprint("log", __name__)


@log_bp.route("/logs")
@require_auth
def search_logs():
    """日志检索：FTS5 全文 + 环境/服务/时间范围过滤"""
    env = request.args.get("env", "")
    service = request.args.get("service", "")
    keyword = request.args.get("keyword", "")
    from_ts = request.args.get("from", 0, type=int)
    to_ts = request.args.get("to", 0, type=int)
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 50, type=int)))

    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        conditions = []
        params = []

        if env:
            conditions.append("le.env = ?")
            params.append(env)
        if service:
            conditions.append("le.service = ?")
            params.append(service)
        if from_ts:
            conditions.append("le.ts >= ?")
            params.append(from_ts)
        if to_ts:
            conditions.append("le.ts <= ?")
            params.append(to_ts)

        if keyword:
            # FTS5 检索（仅搜索 content 列，避免匹配服务名/环境名）
            try:
                fts_sql = "SELECT rowid FROM log_fts WHERE log_fts MATCH ?"
                fts_rows = conn.execute(fts_sql, (f"content:{keyword}",)).fetchall()
            except Exception:
                return jsonify({"rows": [], "total": 0, "error": "搜索关键词包含非法字符"})
            if not fts_rows:
                return jsonify({"rows": [], "total": 0})
            rowids = [r["rowid"] for r in fts_rows]
            placeholders = ",".join("?" * len(rowids))
            conditions.append(f"le.id IN ({placeholders})")
            params.extend(rowids)

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        total = conn.execute(
            f"SELECT COUNT(*) FROM log_entries le WHERE {where_clause}", params
        ).fetchone()[0]

        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT le.id, le.ts, le.env, le.service, le.container, le.file_path, "
            f"le.line_offset, le.line_count, le.content, le.keywords "
            f"FROM log_entries le WHERE {where_clause} "
            f"ORDER BY le.ts DESC LIMIT ? OFFSET ?",
            params + [page_size, offset]
        ).fetchall()

        return jsonify({
            "rows": [dbm._deserialize("log_entries", dict(r)) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        })
    finally:
        conn.close()


@log_bp.route("/logs/context")
@require_auth
def log_context():
    """上下文查看：按文件 + 行偏移取前后 N 行"""
    entry_id = request.args.get("id", 0, type=int)
    lines = min(50, max(1, request.args.get("lines", 5, type=int)))

    if not entry_id:
        return jsonify({"error": "缺少参数 id"}), 400

    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        entry = conn.execute(
            "SELECT * FROM log_entries WHERE id=?", (entry_id,)
        ).fetchone()
        if not entry:
            return jsonify({"error": "日志条目不存在"}), 404

        file_path = entry["file_path"]
        # 路径遍历防护：确保文件在预期的日志目录内
        log_base = os.path.realpath(
            os.path.join(current_app.root_path, "..", "data", "logs", "collect"))
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(log_base + os.sep):
            return jsonify({"error": "非法日志文件路径"}), 403
        if not os.path.isfile(real_path):
            return jsonify({"error": "日志文件不存在", "file": file_path}), 404

        with open(real_path, "r", encoding="utf-8", errors="ignore") as f:
            all_lines = f.readlines()

        offset = entry["line_offset"]
        byte_pos = 0
        target_line = 0
        for i, line in enumerate(all_lines):
            if byte_pos >= offset:
                target_line = i
                break
            byte_pos += len(line.encode("utf-8", "ignore"))

        start = max(0, target_line - lines)
        end = min(len(all_lines), target_line + lines + 1)
        context_lines = all_lines[start:end]

        return jsonify({
            "file": file_path,
            "target_line": target_line,
            "start": start,
            "end": end,
            "lines": context_lines,
        })
    finally:
        conn.close()


@log_bp.route("/logs/services")
@require_auth
def log_services():
    """服务列表：返回所有已采集的环境 × 服务组合"""
    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT env, service FROM log_entries ORDER BY env, service"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ---------- 日志保留/归档设置 ----------

_LOG_SETTING_KEYS = {
    "log_retention_days": {"default": 7, "label": "日志保留天数"},
    "log_archive_days": {"default": 30, "label": "归档保留天数"},
}


@log_bp.route("/logs/settings", methods=["GET"])
@require_auth
@require_role("admin")
def get_log_settings():
    """获取日志保留/归档设置"""
    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        out = {}
        for key, meta in _LOG_SETTING_KEYS.items():
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            val = meta["default"]
            if row:
                try:
                    val = int(json.loads(row["value"]))
                except (ValueError, TypeError):
                    pass
            out[key] = val
        return jsonify(out)
    finally:
        conn.close()


@log_bp.route("/logs/settings", methods=["PUT"])
@require_auth
@require_role("admin")
def put_log_settings():
    """更新日志保留/归档设置"""
    body = request.get_json(silent=True) or {}
    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        for key, meta in _LOG_SETTING_KEYS.items():
            if key not in body:
                continue
            val = int(body[key])
            if val < 1 or val > 365:
                return jsonify({"error": f"{meta['label']}需在 1~365 之间"}), 400
            now = int(time.time())
            existing = conn.execute(
                "SELECT id FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE platform_settings SET value=?, updated_at=? WHERE key=?",
                    (json.dumps(val), now, key)
                )
            else:
                conn.execute(
                    "INSERT INTO platform_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(val), now)
                )
        conn.commit()
    finally:
        conn.close()
    audit("update_log_settings", "config", 0, {"settings": list(body.keys())})
    return jsonify({"ok": True})
