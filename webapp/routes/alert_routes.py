"""告警 API：列表/ack/resolve/统计 + 规则 CRUD + 静默窗口 CRUD + 渠道配置"""
import json
import time
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .. import db as dbm
from ..auth import audit, require_auth, require_role

alert_bp = Blueprint("alert", __name__)

SEVERITIES = ("P0", "P1", "P2", "P3")
STATUSES = ("firing", "acked", "resolved")
CHANNELS = ("email", "wecom", "webhook", "sms_aliyun", "feishu", "dingtalk")


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


# ---------- 规则 CRUD ----------

@alert_bp.route("/alerts/rules")
@require_auth
def list_rules():
    return jsonify(dbm.list_records(current_app.config["DB_PATH"], "alert_rules",
                                    "", (), "id"))


def _validate_rule(body) -> str:
    """规则校验；返回错误信息，空串=通过"""
    if not (body.get("name") or "").strip():
        return "规则名称必填"
    if body.get("metric_type") not in ("metric", "status", "log_keyword"):
        return "metric_type 非法"
    if body.get("metric_type") == "metric" and body.get("metric") not in (
            "cpu", "mem", "disk", "load"):
        return "metric 非法"
    if body.get("metric_type") == "status" and body.get("metric") not in (
            "service_down", "task_failed"):
        return "metric 非法"
    if body.get("severity") not in SEVERITIES:
        return "severity 非法"
    if body.get("metric_type") == "metric":
        try:
            if int(body.get("duration_seconds", 0)) > 1200:
                return "duration_seconds 不能超过 1200 秒（滑动窗口上限 20 分钟）"
        except (TypeError, ValueError):
            return "duration_seconds 非法"
    if not body.get("channels"):
        return "至少选择一个通知渠道"
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
    if not dbm.get_record(db, "alert_rules", rule_id):
        return jsonify({"error": "规则不存在"}), 404
    body = request.get_json(silent=True) or {}
    err = _validate_rule(body)
    if err:
        return jsonify({"error": err}), 400
    dbm.update_record(db, "alert_rules", rule_id, dict(
        name=body["name"].strip(), metric_type=body["metric_type"],
        metric=body.get("metric", ""), compare=body.get("compare", ">"),
        threshold=float(body.get("threshold", 0)),
        duration_seconds=int(body.get("duration_seconds", 0)),
        scope=body.get("scope") or {"type": "all", "ids": []},
        severity=body["severity"], channels=body.get("channels") or [],
        escalation_interval=int(body.get("escalation_interval", 30)),
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), updated_at=int(time.time())))
    audit("update_alert_rule", "alert_rule", rule_id, {"name": body["name"]})
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
    dbm.update_record(db, "alert_rules", rule_id,
                      {"enabled": 0 if r["enabled"] else 1})
    return jsonify({"ok": True, "enabled": 0 if r["enabled"] else 1})


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
    now = int(time.time())
    wid = dbm.create_record(current_app.config["DB_PATH"], "silence_windows", dict(
        name=name, type=wtype, start_at=str(start_at), end_at=str(end_at),
        weekdays=body.get("weekdays") or [],
        scope=body.get("scope") or {"rules": [], "envs": [], "servers": []},
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), created_at=now))
    audit("create_silence", "silence", wid, {"name": name})
    return jsonify({"id": wid})


@alert_bp.route("/alerts/silences/<int:silence_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_silence(silence_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "silence_windows", silence_id):
        return jsonify({"error": "窗口不存在"}), 404
    body = request.get_json(silent=True) or {}
    dbm.update_record(db, "silence_windows", silence_id, dict(
        name=(body.get("name") or "").strip() or "窗口",
        type=body.get("type", "one_time"),
        start_at=str(body.get("start_at", "0")),
        end_at=str(body.get("end_at", "0")),
        weekdays=body.get("weekdays") or [],
        scope=body.get("scope") or {"rules": [], "envs": [], "servers": []},
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", "")))
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
