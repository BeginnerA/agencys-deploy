"""告警 API：列表/ack/resolve/统计 + 规则 CRUD + 静默窗口 CRUD + 渠道配置"""
import json
import re
import time
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .. import db as dbm
from ..auth import audit, require_auth, require_role

alert_bp = Blueprint("alert", __name__)

SEVERITIES = ("P0", "P1", "P2", "P3")
STATUSES = ("firing", "acked", "resolved")
CHANNELS = ("email", "wecom", "webhook", "sms_aliyun", "feishu", "dingtalk")

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _valid_scope(scope) -> bool:
    """静默窗口/规则 scope 结构校验：{rules,envs,servers} 均为列表"""
    return (isinstance(scope, dict)
            and all(isinstance(scope.get(k), list)
                    for k in ("rules", "envs", "servers")))


def _paged():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 20, type=int)))
    return page, page_size


# ---------- 告警实例 ----------

@alert_bp.route("/alerts")
@require_auth
def list_alerts():
    db = current_app.config["DB_PATH"]
    where, params = [], []
    st = request.args.get("status")
    if st in STATUSES:
        where.append("status=?"); params.append(st)
    sev = request.args.get("severity")
    if sev in SEVERITIES:
        where.append("severity=?"); params.append(sev)
    kw = (request.args.get("keyword") or "").strip()
    if kw:
        where.append("(subject LIKE ? OR description LIKE ?)")
        params += [f"%{kw}%"] * 2
    page, page_size = _paged()
    conn = dbm.get_conn(db)
    try:
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM alerts{wsql}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM alerts{wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
                            params + [page_size, (page - 1) * page_size]).fetchall()
        items = [dbm._deserialize("alerts", dict(r)) for r in rows]
    finally:
        conn.close()
    return jsonify({"rows": items, "total": total, "page": page, "page_size": page_size})


@alert_bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@require_auth
def ack_alert(alert_id):
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] == "resolved":
        return jsonify({"error": "已解决告警不能确认"}), 400
    rule = dbm.get_record(db, "alert_rules", a["rule_id"]) or {}
    dbm.update_record(db, "alerts", alert_id, dict(
        status="acked", ack_by=g.user["username"], ack_at=int(time.time()),
        escalation_at=int(time.time()) + int(rule.get("escalation_interval", 30)) * 60))
    audit("ack_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/<int:alert_id>/resolve", methods=["POST"])
@require_auth
def resolve_alert(alert_id):
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] == "resolved":
        return jsonify({"error": "已解决"}), 400
    dbm.update_record(db, "alerts", alert_id, dict(
        status="resolved", resolved_by=g.user["username"],
        resolved_at=int(time.time()), resolved_type="manual"))
    audit("resolve_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/stats")
@require_auth
def alert_stats():
    """近 N 天按天/级别聚合（ECharts 趋势图数据源）"""
    db = current_app.config["DB_PATH"]
    days = min(30, max(1, request.args.get("days", 7, type=int)))
    since = int(time.time()) - days * 86400
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT strftime('%m-%d', first_seen_at, 'unixepoch', 'localtime') d, "
            "severity, COUNT(*) n FROM alerts WHERE first_seen_at >= ? "
            "GROUP BY d, severity", (since,)).fetchall()
    finally:
        conn.close()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["d"], {})[r["severity"]] = r["n"]
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%m-%d")
        out.append({"date": d,
                    **{s: by_day.get(d, {}).get(s, 0) for s in SEVERITIES}})
    return jsonify(out)


@alert_bp.route("/alerts/<int:alert_id>/events")
@require_auth
def alert_events(alert_id):
    """告警通知投递历史"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT * FROM alert_events WHERE alert_id=? ORDER BY id DESC",
            (alert_id,)
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@alert_bp.route("/alerts/<int:alert_id>/logs")
@require_auth
def alert_logs(alert_id):
    """告警日志上下文：按 (ts, id) 复合游标实时查 log_entries，取命中行前后 N 行

    复合游标必要性：ts 秒级精度，同秒多条日志靠 id 自增保持稳定次序；
    按 env+service 过滤避免其他容器日志混入。不可用场景返回 reason 供前端分发。
    """
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    rule = dbm.get_record(db, "alert_rules", a["rule_id"]) or {}
    if rule.get("metric_type") != "log_keyword":
        return jsonify({"available": False, "reason": "not_log_alert"})
    info = a.get("target_info") or {}
    entry_id = info.get("entry_id")
    hit_ts = info.get("hit_ts")
    if not entry_id or not hit_ts:
        return jsonify({"available": False, "reason": "no_position"})
    env = info.get("env") or ""
    service = info.get("service") or ""
    keyword = info.get("keyword") or ""
    lines = min(200, max(1, request.args.get("lines", 100, type=int)))

    conn = dbm.get_conn(db)
    try:
        # before：倒序取 lines 条再翻转回正序
        before_rows = conn.execute(
            "SELECT id, ts, content FROM log_entries "
            "WHERE env=? AND service=? AND (ts < ? OR (ts = ? AND id < ?)) "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (env, service, hit_ts, hit_ts, entry_id, lines)
        ).fetchall()
        before = [dict(r) for r in reversed(before_rows)]
        # hit：精确取命中行
        hit_row = conn.execute(
            "SELECT id, ts, content FROM log_entries WHERE id=?",
            (entry_id,)
        ).fetchone()
        hit = dict(hit_row) if hit_row else None
        # after：正序取 lines 条
        after_rows = conn.execute(
            "SELECT id, ts, content FROM log_entries "
            "WHERE env=? AND service=? AND (ts > ? OR (ts = ? AND id > ?)) "
            "ORDER BY ts ASC, id ASC LIMIT ?",
            (env, service, hit_ts, hit_ts, entry_id, lines)
        ).fetchall()
        after = [dict(r) for r in after_rows]
    finally:
        conn.close()

    # 命中行已被清理（如超保留期）：用相邻行兜底标记，仍展示上下文。
    # 兜底行需从原列表移除，避免同一行在 hit 与上下文列表重复出现
    if not hit:
        if before:
            hit = before.pop()
        elif after:
            hit = after.pop(0)
        if hit:
            hit["fallback"] = True
    if not hit and not before and not after:
        return jsonify({"available": False, "reason": "expired",
                        "env": env, "service": service, "keyword": keyword,
                        "hit": None, "before": [], "after": [], "lines": lines})
    return jsonify({
        "available": True, "env": env, "service": service,
        "keyword": keyword, "hit": hit, "before": before, "after": after,
        "lines": lines,
    })


@alert_bp.route("/alerts/batch", methods=["POST"])
@require_auth
def alert_batch():
    """批量确认/解决告警"""
    body = request.get_json(silent=True) or {}
    action = body.get("action")  # ack / resolve
    ids = body.get("ids") or []
    if action not in ("ack", "resolve"):
        return jsonify({"error": "action 需为 ack 或 resolve"}), 400
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids 不能为空"}), 400
    if len(ids) > 100:
        return jsonify({"error": "单次最多操作 100 条"}), 400
    db = current_app.config["DB_PATH"]
    now = int(time.time())
    success_count = 0
    for aid in ids:
        a = dbm.get_record(db, "alerts", int(aid))
        if not a:
            continue
        if action == "ack" and a["status"] == "firing":
            rule = dbm.get_record(db, "alert_rules", a["rule_id"]) or {}
            dbm.update_record(db, "alerts", int(aid), dict(
                status="acked", ack_by=g.user["username"], ack_at=now,
                escalation_at=now + int(rule.get("escalation_interval", 30)) * 60))
            success_count += 1
        elif action == "resolve" and a["status"] != "resolved":
            dbm.update_record(db, "alerts", int(aid), dict(
                status="resolved", resolved_by=g.user["username"],
                resolved_at=now, resolved_type="manual"))
            success_count += 1
    audit(f"batch_{action}_alerts", "alert", 0,
          {"ids": ids, "success": success_count})
    return jsonify({"ok": True, "success": success_count})


# ---------- 作用域数据 API ----------

@alert_bp.route("/alerts/scope/environments")
@require_auth
def scope_environments():
    """全部环境列表（供级联选择第一级）"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT e.id, e.name, e.project_id, p.name as project_name "
            "FROM environments e JOIN projects p ON e.project_id=p.id "
            "ORDER BY p.name, e.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@alert_bp.route("/alerts/scope/servers")
@require_auth
def scope_servers():
    """指定环境下的服务器列表"""
    env_ids_raw = request.args.get("env_ids", "")
    if not env_ids_raw:
        return jsonify([])
    try:
        env_ids = [int(x) for x in env_ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify([])
    if not env_ids:
        return jsonify([])
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        placeholders = ",".join("?" * len(env_ids))
        rows = conn.execute(
            f"SELECT DISTINCT s.id, s.host FROM servers s "
            f"JOIN env_servers es ON es.server_id=s.id "
            f"WHERE es.env_id IN ({placeholders}) AND s.enabled=1 "
            f"ORDER BY s.host",
            env_ids
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@alert_bp.route("/alerts/scope/services")
@require_auth
def scope_services():
    """指定环境下的服务列表（通过环境→项目间接关联）"""
    env_ids_raw = request.args.get("env_ids", "")
    if not env_ids_raw:
        return jsonify([])
    try:
        env_ids = [int(x) for x in env_ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify([])
    if not env_ids:
        return jsonify([])
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        placeholders = ",".join("?" * len(env_ids))
        rows = conn.execute(
            f"SELECT DISTINCT s.id, s.name FROM services s "
            f"WHERE s.project_id IN ("
            f"  SELECT DISTINCT project_id FROM environments WHERE id IN ({placeholders})"
            f") ORDER BY s.name",
            env_ids
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ---------- 规则 CRUD ----------

@alert_bp.route("/alerts/rules")
@require_auth
def list_rules():
    return jsonify(dbm.list_records(current_app.config["DB_PATH"], "alert_rules",
                                    "", (), "id"))


def _validate_rule(body, partial=False) -> str:
    """规则校验；返回错误信息，空串=通过。partial=True 仅校验已出现的字段（局部更新）"""
    def has(k):
        return True if not partial else k in body

    if has("name") and not (body.get("name") or "").strip():
        return "规则名称必填"
    if has("metric_type") and body.get("metric_type") not in (
            "metric", "status", "log_keyword"):
        return "metric_type 非法"
    if has("metric"):
        if body.get("metric_type") == "metric" and body.get("metric") not in (
                "cpu", "mem", "disk", "load"):
            return "metric 非法"
        if body.get("metric_type") == "status" and body.get("metric") not in (
                "service_down", "task_failed"):
            return "metric 非法"
        if body.get("metric_type") == "log_keyword":
            kw_raw = (body.get("metric") or "").strip()
            if not kw_raw:
                return "日志关键词不能为空"
            keywords = [k.strip() for k in kw_raw.split(",") if k.strip()]
            if not keywords:
                return "日志关键词不能为空"
            if any(len(k) > 100 for k in keywords):
                return "单个关键词长度不能超过 100"
    if has("severity") and body.get("severity") not in SEVERITIES:
        return "severity 非法"
    if has("compare") and body.get("compare") not in ("<", "<=", ">", ">="):
        return "compare 非法"
    if has("threshold"):
        try:
            float(body.get("threshold", 0))
        except (TypeError, ValueError):
            return "threshold 必须为数字"
    if has("duration_seconds"):
        try:
            duration = int(body.get("duration_seconds", 0))
        except (TypeError, ValueError):
            return "duration_seconds 非法"
        if body.get("metric_type") == "metric" and duration > 1200:
            return "duration_seconds 不能超过 1200 秒（滑动窗口上限 20 分钟）"
    if has("escalation_interval"):
        try:
            int(body.get("escalation_interval", 30))
        except (TypeError, ValueError):
            return "escalation_interval 必须为数字"
    if has("scope"):
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            return "scope 格式错误"
        valid_types = ("all", "project", "env", "server", "service")
        if scope.get("type") not in valid_types:
            return "scope type 非法"
        if not isinstance(scope.get("ids"), list):
            return "scope.ids 需为列表"
        if "env_ids" in scope and not isinstance(scope["env_ids"], list):
            return "scope.env_ids 需为列表"
        mt = body.get("metric_type")
        if mt == "metric" and scope.get("type") == "service":
            return "指标规则不能选择服务作用域"
        if mt in ("status", "log_keyword") and scope.get("type") == "server":
            return "状态/日志规则不能选择服务器作用域"
    if has("channels"):
        channels = body.get("channels") or []
        if not channels:
            return "至少选择一个通知渠道"
        if not set(channels) <= set(CHANNELS):
            return "channels 包含非法渠道"
    return ""


@alert_bp.route("/alerts/rules", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_rule():
    body = request.get_json(silent=True) or {}
    err = _validate_rule(body)
    if err:
        return jsonify({"error": err}), 400
    now = int(time.time())
    rid = dbm.create_record(current_app.config["DB_PATH"], "alert_rules", dict(
        name=body["name"].strip(), metric_type=body["metric_type"],
        metric=body.get("metric", ""), compare=body.get("compare", ">"),
        threshold=float(body.get("threshold", 0)),
        duration_seconds=int(body.get("duration_seconds", 0)),
        scope=body.get("scope") or {"type": "all", "ids": []},
        severity=body["severity"], channels=body.get("channels") or [],
        escalation_interval=int(body.get("escalation_interval", 30)),
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), created_at=now, updated_at=now))
    audit("create_alert_rule", "alert_rule", rid, {"name": body["name"]})
    return jsonify({"id": rid})


@alert_bp.route("/alerts/rules/<int:rule_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_rule(rule_id):
    db = current_app.config["DB_PATH"]
    old = dbm.get_record(db, "alert_rules", rule_id)
    if not old:
        return jsonify({"error": "规则不存在"}), 404
    body = request.get_json(silent=True) or {}
    err = _validate_rule(body, partial=True)
    if err:
        return jsonify({"error": err}), 400
    # 合并 old + 已提供的字段后整体复校验：防止 metric_type/metric 等
    # 跨字段组合失配（如 status 规则配 cpu）导致规则被引擎静默忽略
    merged = dict(old)
    merged.update({k: v for k, v in body.items() if v is not None})
    err = _validate_rule(merged)
    if err:
        return jsonify({"error": err}), 400
    # 局部更新：未提供的字段保持原值，避免 PUT 部分提交静默重置配置
    dbm.update_record(db, "alert_rules", rule_id, dict(
        name=(body.get("name") or old["name"]).strip(),
        metric_type=body.get("metric_type", old["metric_type"]),
        metric=body.get("metric", old["metric"]),
        compare=body.get("compare", old["compare"]),
        threshold=float(body.get("threshold", old["threshold"])),
        duration_seconds=int(body.get("duration_seconds",
                                      old["duration_seconds"])),
        scope=body.get("scope", old["scope"]),
        severity=body.get("severity", old["severity"]),
        channels=body.get("channels", old["channels"]),
        escalation_interval=int(body.get("escalation_interval",
                                         old["escalation_interval"])),
        enabled=1 if body.get("enabled", old["enabled"]) else 0,
        remark=body.get("remark", old["remark"]),
        updated_at=int(time.time())))
    audit("update_alert_rule", "alert_rule", rule_id,
          {"name": body.get("name", old["name"])})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/rules/<int:rule_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_rule(rule_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "alert_rules", rule_id):
        return jsonify({"error": "规则不存在"}), 404
    dbm.delete_record(db, "alert_rules", rule_id)
    audit("delete_alert_rule", "alert_rule", rule_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/rules/<int:rule_id>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_rule(rule_id):
    db = current_app.config["DB_PATH"]
    r = dbm.get_record(db, "alert_rules", rule_id)
    if not r:
        return jsonify({"error": "规则不存在"}), 404
    new_enabled = 0 if r["enabled"] else 1
    dbm.update_record(db, "alert_rules", rule_id, {"enabled": new_enabled})
    audit("toggle_alert_rule", "alert_rule", rule_id, {"enabled": new_enabled})
    return jsonify({"ok": True, "enabled": new_enabled})


# ---------- 静默窗口 CRUD ----------

@alert_bp.route("/alerts/silences")
@require_auth
def list_silences():
    return jsonify(dbm.list_records(current_app.config["DB_PATH"], "silence_windows",
                                    "", (), "id"))


@alert_bp.route("/alerts/silences", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_silence():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "窗口名称必填"}), 400
    wtype = body.get("type", "one_time")
    if wtype not in ("one_time", "recurring"):
        return jsonify({"error": "type 非法"}), 400
    start_at = body.get("start_at", "")
    end_at = body.get("end_at", "")
    if wtype == "one_time":
        try:
            float(start_at); float(end_at)
        except (TypeError, ValueError):
            return jsonify({"error": "一次性窗口需 epoch 秒 start_at/end_at"}), 400
    elif wtype == "recurring":
        if not (_HHMM.match(str(start_at)) and _HHMM.match(str(end_at))):
            return jsonify({"error": "周期窗口需 HH:MM 格式 start_at/end_at"}), 400
    scope = body.get("scope") or {"rules": [], "envs": [], "servers": []}
    if not _valid_scope(scope):
        return jsonify({"error": "scope 需为 {rules,envs,servers} 列表结构"}), 400
    now = int(time.time())
    wid = dbm.create_record(current_app.config["DB_PATH"], "silence_windows", dict(
        name=name, type=wtype, start_at=str(start_at), end_at=str(end_at),
        weekdays=body.get("weekdays") or [],
        scope=scope,
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), created_at=now))
    audit("create_silence", "silence", wid, {"name": name})
    return jsonify({"id": wid})


@alert_bp.route("/alerts/silences/<int:silence_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_silence(silence_id):
    db = current_app.config["DB_PATH"]
    old = dbm.get_record(db, "silence_windows", silence_id)
    if not old:
        return jsonify({"error": "窗口不存在"}), 404
    body = request.get_json(silent=True) or {}
    wtype = body.get("type", old["type"])
    if wtype not in ("one_time", "recurring"):
        return jsonify({"error": "type 非法"}), 400
    start_at = str(body.get("start_at", old["start_at"]))
    end_at = str(body.get("end_at", old["end_at"]))
    if wtype == "one_time":
        try:
            float(start_at); float(end_at)
        except (TypeError, ValueError):
            return jsonify({"error": "一次性窗口需 epoch 秒 start_at/end_at"}), 400
    elif wtype == "recurring":
        if not (_HHMM.match(str(start_at)) and _HHMM.match(str(end_at))):
            return jsonify({"error": "周期窗口需 HH:MM 格式 start_at/end_at"}), 400
    if "scope" in body and not _valid_scope(body["scope"]):
        return jsonify({"error": "scope 需为 {rules,envs,servers} 列表结构"}), 400
    # 局部更新：未提供的字段保持原值，避免 PUT 部分提交静默重置配置
    dbm.update_record(db, "silence_windows", silence_id, dict(
        name=(body.get("name") or old["name"]).strip(),
        type=wtype, start_at=start_at, end_at=end_at,
        weekdays=body.get("weekdays", old["weekdays"]),
        scope=body.get("scope", old["scope"]),
        enabled=1 if body.get("enabled", old["enabled"]) else 0,
        remark=body.get("remark", old["remark"])))
    audit("update_silence", "silence", silence_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/silences/<int:silence_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_silence(silence_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "silence_windows", silence_id):
        return jsonify({"error": "窗口不存在"}), 404
    dbm.delete_record(db, "silence_windows", silence_id)
    audit("delete_silence", "silence", silence_id)
    return jsonify({"ok": True})


# ---------- 渠道配置 ----------

_MASK_KEYS = ("password", "secret", "access_key")


@alert_bp.route("/alerts/channels", methods=["GET"])
@require_auth
@require_role("admin")
def get_channels():
    db = current_app.config["DB_PATH"]
    out = {}
    for c in CHANNELS:
        rows = dbm.list_records(db, "platform_settings", "key=?", (f"channel.{c}",))
        cfg = {}
        if rows and rows[0]["value"]:
            try:
                cfg = json.loads(rows[0]["value"]) or {}
            except ValueError:
                cfg = {}
        masked = dict(cfg)
        for k in _MASK_KEYS:
            if masked.get(k):
                masked[k] = "******"
        out[c] = masked
    return jsonify(out)


@alert_bp.route("/alerts/channels", methods=["PUT"])
@require_auth
@require_role("admin")
def put_channels():
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    for channel, cfg in body.items():
        if channel not in CHANNELS or not isinstance(cfg, dict):
            continue
        rows = dbm.list_records(db, "platform_settings", "key=?", (f"channel.{channel}",))
        old = {}
        if rows and rows[0]["value"]:
            try:
                old = json.loads(rows[0]["value"]) or {}
            except ValueError:
                old = {}
        # 打码字段未修改时保留原值
        for k in _MASK_KEYS:
            if cfg.get(k) in (None, "", "******") and old.get(k):
                cfg[k] = old[k]
        now = int(time.time())
        value = json.dumps(cfg, ensure_ascii=False)
        if rows:
            dbm.update_record(db, "platform_settings", rows[0]["id"],
                              {"value": value, "updated_at": now})
        else:
            dbm.create_record(db, "platform_settings",
                              {"key": f"channel.{channel}", "value": value,
                               "updated_at": now})
    audit("update_alert_channels", "config", 0, {"channels": list(body.keys())})
    return jsonify({"ok": True})
