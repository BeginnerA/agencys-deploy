"""日志中心 API：检索 + 上下文 + 服务列表 + 保留设置 + 存储维护"""
import json
import os
import time
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from .. import db as dbm
from ..auth import audit, require_auth, require_role
from ..log_collector import get_collector, run_manual_cleanup

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

        if keyword:
            # FTS5 检索（仅搜索 content 列，避免匹配服务名/环境名）；
            # 用 IN 子查询而非展开 rowid 列表：命中结果可能超过
            # SQLITE_MAX_VARIABLE_NUMBER(999)，展开会报 "too many SQL variables"。
            # env/service 下推进 FTS 子查询（TEXT 相等比较），缩小中间集合；
            # 时间范围保留外层 le.ts 数值比较（FTS 表 ts 为文本列）
            fts_where = ["log_fts MATCH ?"]
            fts_params = [f"content:{keyword}"]
            if env:
                fts_where.append("env = ?")
                fts_params.append(env)
            if service:
                fts_where.append("service = ?")
                fts_params.append(service)
            conditions.append(
                f"le.id IN (SELECT rowid FROM log_fts WHERE {' AND '.join(fts_where)})")
            params.extend(fts_params)
        else:
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

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM log_entries le WHERE {where_clause}", params
            ).fetchone()[0]
        except Exception:
            # FTS5 非法字符（未配对引号等）在子查询执行时才暴露
            return jsonify({"rows": [], "total": 0, "error": "搜索关键词包含非法字符"})

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
        # 路径遍历防护：确保文件在本实例日志目录的 collect/ 内
        # （根目录取 app.config['LOG_DIR']，与采集器写入点同源；
        #  root_path 推导出来的仓库 data/ 只对新部署的实例误判为"非法路径"）
        log_base = os.path.realpath(os.path.join(_log_dir(), "collect"))
        real_path = os.path.realpath(file_path)
        if not real_path.startswith(log_base + os.sep):
            return jsonify({"error": "非法日志文件路径"}), 403
        if not os.path.isfile(real_path):
            return jsonify({"error": "日志文件不存在", "file": file_path}), 404

        offset = entry["line_offset"]
        # 按行定位读取目标行前后文，避免 readlines 全量读入内存
        # （大日志文件可达数十 MB~上百 MB）。
        # line_offset 是“逻辑字节偏移”（采集端按 Linux 源行长度累加，无 \r）；
        # Windows 文本模式追加会把 \n 写为 \r\n，故按行读取时需补偿行尾 \r，
        # 保证与 line_offset 同单位定位（跨平台自洽）
        try:
            target_line = 0
            context_lines = []
            with open(real_path, "rb") as f:
                prev = []
                pos = 0
                while True:
                    if pos >= offset:
                        break
                    line = f.readline()
                    if not line:
                        break
                    pos += len(line) - (1 if line.endswith(b"\r\n") else 0)
                    target_line += 1
                    if len(prev) >= lines:
                        prev.pop(0)
                    prev.append(line)
                # 目标行（循环 break 时尚未读入）+ 后 N 行
                tail = []
                target = f.readline()
                if target:
                    tail.append(target)
                for _ in range(lines):
                    line = f.readline()
                    if not line:
                        break
                    tail.append(line)
            # 前 N 行（prev 尾部）+ 目标行 + 后 N 行；target_line = 目标行行号（0 基）
            context_lines = [l.decode("utf-8", "ignore")
                             for l in prev[-lines:] + tail]
        except (ValueError, OSError):
            return jsonify({"error": "日志文件读取失败"}), 500

        start = max(0, target_line - lines)
        end = start + len(context_lines)

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
    """服务列表：从项目配置表返回环境 × 服务组合（支持 project_id 过滤）"""
    project_id = request.args.get("project_id", 0, type=int)
    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        # jar_service 无容器名（service_name 为空）→ 回退到服务名，
        # 与采集器写入 log_entries.service 的口径（service_name or name）一致
        sql = (
            "SELECT DISTINCT e.name AS env, "
            "CASE WHEN s.service_name != '' THEN s.service_name ELSE s.name END AS service "
            "FROM services s "
            "JOIN environments e ON s.project_id = e.project_id "
            "WHERE s.enabled = 1"
        )
        params = []
        if project_id:
            sql += " AND s.project_id = ?"
            params.append(project_id)
        sql += " ORDER BY e.name, service"
        rows = conn.execute(sql, params).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ---------- 服务日志尾部（大屏实时日志抽屉用）----------

@log_bp.route("/logs/tail")
@require_auth
def tail_service_logs():
    """获取指定服务的最近 N 条日志（供大屏服务卡片日志抽屉轮询）

    参数：
      service — 服务显示名（services.name），自动映射到 service_name（容器名）
      env     — 环境名（可选，缩小范围）
      lines   — 返回条数（默认 200，上限 500）
      after   — 仅返回 id > after 的条目（增量轮询）
    """
    service = request.args.get("service", "")
    env = request.args.get("env", "")
    lines = min(500, max(1, request.args.get("lines", 200, type=int)))
    after = request.args.get("after", 0, type=int)

    if not service:
        return jsonify({"rows": [], "total": 0})

    db_path = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db_path)
    try:
        # 将服务显示名映射到 service_name（Docker 容器名）/服务名
        # （jar_service 无容器名，log_entries.service = service_name or name）
        row = conn.execute(
            "SELECT service_name, name FROM services WHERE name = ? LIMIT 1",
            (service,)
        ).fetchone()
        svc_key = ((row["service_name"] or row["name"]) if row else service)

        # 查询最近的日志条目
        if after:
            sql = ("SELECT id, ts, env, service, content FROM log_entries "
                   "WHERE service = ? AND id > ?")
            params = [svc_key, after]
        else:
            sql = ("SELECT id, ts, env, service, content FROM log_entries "
                   "WHERE service = ?")
            params = [svc_key]

        if env:
            sql += " AND env = ?"
            params.append(env)

        if after:
            sql += " ORDER BY id ASC LIMIT ?"
        else:
            # 取最近 N 条，然后反转为时间正序
            sql += " ORDER BY id DESC LIMIT ?"
        params.append(lines)

        rows = conn.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        if not after:
            result.reverse()

        return jsonify({"rows": result, "total": len(result)})
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


# ---------- 存储概况与手动清理 ----------

def _log_dir():
    """日志目录：优先 app.config（与采集器同源），兜底 root_path 推导"""
    return current_app.config.get("LOG_DIR") or os.path.join(
        current_app.root_path, "..", "data", "logs")


def _dir_stats(base: str) -> dict:
    """目录统计：文件数 + 总字节数（不存在返回零值）"""
    files = bytes_total = 0
    if os.path.isdir(base):
        for p in Path(base).rglob("*"):
            if p.is_file():
                try:
                    files += 1
                    bytes_total += p.stat().st_size
                except OSError:
                    pass
    return {"files": files, "bytes": bytes_total}


@log_bp.route("/logs/storage")
@require_auth
@require_role("admin")
def log_storage_stats():
    """存储概况：日志行数/目录占用/DB 大小与空闲页（手动清理入口展示用）"""
    db_path = current_app.config["DB_PATH"]
    log_dir = _log_dir()
    conn = dbm.get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM log_entries"
        ).fetchone()
        entries, oldest_ts = row[0], row[1]
        # freelist 空闲页（auto_vacuum 库可被手动/增量清理回收）
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        settings = {}
        for key, default in (("log_retention_days", 7),
                             ("log_archive_days", 30)):
            srow = conn.execute(
                "SELECT value FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            try:
                settings[key] = int(json.loads(srow["value"])) if srow else default
            except (ValueError, TypeError):
                settings[key] = default
    finally:
        conn.close()

    try:
        db_size = os.path.getsize(db_path)
    except OSError:
        db_size = 0

    return jsonify({
        "log_entries": entries,
        "oldest_ts": oldest_ts,
        "db_size": db_size,
        "db_freelist_bytes": freelist * page_size,
        "collect": _dir_stats(os.path.join(log_dir, "collect")),
        "archive": _dir_stats(os.path.join(log_dir, "archive")),
        "retention_days": settings["log_retention_days"],
        "archive_days": settings["log_archive_days"],
        # 采集器未启动（监控关闭）时手动清理仍可用，仅提示状态
        "collector_running": get_collector() is not None,
    })


@log_bp.route("/logs/maintenance", methods=["POST"])
@require_auth
@require_role("admin")
def run_log_maintenance():
    """手动清理：按保留策略立即清理过期日志并回收空间（见
    log_collector.run_manual_cleanup；与每日自动维护同一套逻辑）"""
    stats = run_manual_cleanup(current_app.config["DB_PATH"], _log_dir())
    if stats.get("busy"):
        return jsonify({"error": "清理正在进行中，请稍后重试"}), 409
    audit("manual_log_cleanup", "config", 0, {
        "deleted_entries": stats.get("deleted_entries", 0),
        "archived_files": stats.get("archived_files", 0),
        "purged_files": stats.get("purged_files", 0),
        "db_mb_before": round(stats.get("db_size_before", 0) / 1048576, 1),
        "db_mb_after": round(stats.get("db_size_after", 0) / 1048576, 1),
    })
    return jsonify(stats)
