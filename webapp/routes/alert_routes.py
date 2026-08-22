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


def _project_alert_filter(project_id=0):
    """按项目 ID 构建告警 WHERE 子句（匹配 target_info 中的 project 字段）
    project_id=0 时匹配所有已配置项目（排除不属于任何项目的孤立告警）
    返回 (sql_fragment, params)"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        if project_id:
            rows = conn.execute(
                "SELECT code FROM projects WHERE id=?", (project_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT code FROM projects").fetchall()
    finally:
        conn.close()
    codes = [r["code"] for r in rows if r["code"]]
    if not codes:
        return "1=0", []
    clauses = []
    params = []
    for code in codes:
        clauses.append("(target_info LIKE ? OR target_info LIKE ?)")
        params += [f'%"project":"{code}"%', f'%"project": "{code}"%']
    # 到期监控告警（target_info 含 monitor_id）始终可见
    clauses.append("(target_info LIKE ?)")
    params.append('%"monitor_id":%')
    return "(" + " OR ".join(clauses) + ")", params


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
    # 项目筛选：始终过滤，仅显示已配置项目的告警（排除孤立告警）
    project_id = request.args.get("project_id", 0, type=int)
    pf, pp = _project_alert_filter(project_id)
    if pf:
        where.append(pf); params.extend(pp)
    # 环境筛选
    env_val = (request.args.get("env") or "").strip()
    if env_val:
        where.append("(target_info LIKE ? OR target_info LIKE ?)")
        params += [f'%"env":"{env_val}"%', f'%"env": "{env_val}"%']
    # 服务筛选
    svc_val = (request.args.get("service") or "").strip()
    if svc_val:
        where.append("(target_info LIKE ? OR target_info LIKE ?)")
        params += [f'%"service":"{svc_val}"%', f'%"service": "{svc_val}"%']
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


@alert_bp.route("/alerts/filter-options")
@require_auth
def alert_filter_options():
    """从告警 target_info 提取环境/服务筛选选项（按项目分组）"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT target_info FROM alerts WHERE target_info IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    # {project_code: {envs: set, services: set}}
    by_proj = {}
    for r in rows:
        try:
            ti = json.loads(r["target_info"]) if r["target_info"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        pc = ti.get("project", "")
        if not pc:
            continue
        bucket = by_proj.setdefault(pc, {"envs": set(), "services": set()})
        if ti.get("env"):
            bucket["envs"].add(ti["env"])
        if ti.get("service"):
            bucket["services"].add(ti["service"])
    return jsonify({
        pc: {"envs": sorted(d["envs"]), "services": sorted(d["services"])}
        for pc, d in by_proj.items()
    })


@alert_bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@require_auth
@require_role("admin", "operator")
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
@require_role("admin", "operator")
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


@alert_bp.route("/alerts/<int:alert_id>/reopen", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def reopen_alert(alert_id):
    """将已解决的告警重新打开为 firing 状态"""
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] != "resolved":
        return jsonify({"error": "仅已解决告警可重新打开"}), 400
    now = int(time.time())
    dbm.update_record(db, "alerts", alert_id, dict(
        status="firing", resolved_by="", resolved_at=None,
        resolved_type="", first_seen_at=now, last_seen_at=now,
        notify_count=0, escalation_level=0))
    audit("reopen_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/<int:alert_id>/delete", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_alert(alert_id):
    """删除已确认或已解决的告警"""
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] not in ("acked", "resolved"):
        return jsonify({"error": "仅可删除已确认或已解决的告警"}), 400
    dbm.delete_record(db, "alerts", alert_id)
    audit("delete_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/badge")
@require_auth
def alert_badge():
    """轻量级活跃告警计数（全局角标轮询用，无筛选/分页）"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        firing = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='firing'"
        ).fetchone()[0]
        acked = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE status='acked'"
        ).fetchone()[0]
    finally:
        conn.close()
    return jsonify({"firing": firing, "acked": acked})


@alert_bp.route("/alerts/stats")
@require_auth
def alert_stats():
    """近 N 天按天/级别聚合（ECharts 趋势图数据源）"""
    db = current_app.config["DB_PATH"]
    days = min(30, max(1, request.args.get("days", 7, type=int)))
    since = int(time.time()) - days * 86400
    # 项目筛选
    project_id = request.args.get("project_id", 0, type=int)
    pf, pp = _project_alert_filter(project_id)
    extra_where = ""
    if pf:
        extra_where = f" AND {pf}"
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT strftime('%m-%d', first_seen_at, 'unixepoch', 'localtime') d, "
            "severity, COUNT(*) n FROM alerts WHERE first_seen_at >= ?" + extra_where +
            " GROUP BY d, severity", (since,) + tuple(pp)).fetchall()
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
@require_role("admin", "operator")
def alert_batch():
    """批量确认/解决/删除告警"""
    body = request.get_json(silent=True) or {}
    action = body.get("action")  # ack / resolve / delete
    ids = body.get("ids") or []
    if action not in ("ack", "resolve", "delete", "reopen"):
        return jsonify({"error": "action 需为 ack、resolve、delete 或 reopen"}), 400
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
        elif action == "delete" and a["status"] in ("acked", "resolved"):
            dbm.delete_record(db, "alerts", int(aid))
            success_count += 1
        elif action == "reopen" and a["status"] == "resolved":
            now_ts = int(time.time())
            dbm.update_record(db, "alerts", int(aid), dict(
                status="firing", resolved_by="", resolved_at=None,
                resolved_type="", first_seen_at=now_ts, last_seen_at=now_ts,
                notify_count=0, escalation_level=0))
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
            f") AND s.enabled = 1 ORDER BY s.name",
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
            "metric", "status", "log_keyword", "custom_monitor"):
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
    if has("exclude_keyword"):
        ex_raw = (body.get("exclude_keyword") or "").strip()
        if ex_raw:
            ex_keywords = [k.strip() for k in ex_raw.split(",") if k.strip()]
            if any(len(k) > 100 for k in ex_keywords):
                return "单个排除关键词长度不能超过 100"
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
        if channels and not set(channels) <= set(CHANNELS):
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
        exclude_keyword=(body.get("exclude_keyword") or "").strip(),
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
        exclude_keyword=body.get("exclude_keyword", old.get("exclude_keyword", "")).strip() if "exclude_keyword" in body else old.get("exclude_keyword", ""),
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


@alert_bp.route("/alerts/channels/<channel>/test", methods=["POST"])
@require_auth
@require_role("admin")
def test_channel_route(channel):
    """测试单个渠道连通性"""
    if channel not in CHANNELS:
        return jsonify({"error": f"未知渠道: {channel}"}), 400
    from ..alert_channels import test_channel
    db = current_app.config["DB_PATH"]
    ok, detail = test_channel(db, channel)
    audit("test_alert_channel", "config", 0, {"channel": channel, "ok": ok})
    return jsonify({"ok": ok, "detail": detail})


# ---------- 到期监控 CRUD ----------

MONITOR_TYPES = ("ssl", "cloud_renewal", "license", "custom", "domain")


@alert_bp.route("/alerts/monitors")
@require_auth
def list_monitors():
    """到期监控列表"""
    db = current_app.config["DB_PATH"]
    rows = dbm.list_records(db, "custom_monitors", "", (), "id")
    return jsonify(rows)


@alert_bp.route("/alerts/monitors", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_monitor():
    """添加监控项"""
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    err = _validate_monitor(body)
    if err:
        return jsonify({"error": err}), 400
    now = int(time.time())
    expiry_at = body.get("expiry_at")
    days_left = None
    if expiry_at:
        days_left = max(0, (int(expiry_at) - now) // 86400)
    # domain 类型自动 WHOIS 查询
    if body.get("type") == "domain" and not expiry_at:
        whois_result = _whois_lookup(body["target"].strip())
        if whois_result.get("expiry_at"):
            expiry_at = whois_result["expiry_at"]
            days_left = max(0, (expiry_at - now) // 86400)
    mid = dbm.create_record(db, "custom_monitors", dict(
        type=body["type"], name=body["name"].strip(),
        target=body["target"].strip(),
        port=body.get("port", 443),
        expiry_at=expiry_at,
        days_left=days_left,
        warn_days=body.get("warn_days", 0),
        severity=body.get("severity", ""),
        channels=body.get("channels", []),
        enabled=1,
        owner=body.get("owner", ""),
        renewal_status="",
        tiered_thresholds=body.get("tiered_thresholds", ""),
        detail=body.get("detail", {}),
        error="",
        created_at=now, updated_at=now))
    audit("create_monitor", "custom_monitor", mid, {"name": body["name"]})
    return jsonify({"id": mid})


@alert_bp.route("/alerts/monitors/<int:monitor_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_monitor(monitor_id):
    """编辑监控项"""
    db = current_app.config["DB_PATH"]
    old = dbm.get_record(db, "custom_monitors", monitor_id)
    if not old:
        return jsonify({"error": "监控项不存在"}), 404
    body = request.get_json(silent=True) or {}
    err = _validate_monitor(body, partial=True)
    if err:
        return jsonify({"error": err}), 400
    dbm.update_record(db, "custom_monitors", monitor_id, dict(
        type=body.get("type", old["type"]),
        name=(body.get("name") or old["name"]).strip(),
        target=(body.get("target") or old["target"]).strip(),
        port=body.get("port", old["port"]),
        expiry_at=body.get("expiry_at", old["expiry_at"]),
        days_left=_calc_days_left(body.get("expiry_at", old["expiry_at"])),
        warn_days=body.get("warn_days", old["warn_days"]),
        severity=body.get("severity", old.get("severity", "")),
        channels=body.get("channels", old.get("channels", [])),
        owner=body.get("owner", old.get("owner", "")),
        renewal_status=body.get("renewal_status", old.get("renewal_status", "")),
        tiered_thresholds=body.get("tiered_thresholds", old.get("tiered_thresholds", "")),
        detail=body.get("detail", old.get("detail", {})),
        updated_at=int(time.time())))
    audit("update_monitor", "custom_monitor", monitor_id,
          {"name": body.get("name", old["name"])})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/monitors/<int:monitor_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_monitor(monitor_id):
    """删除监控项"""
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "custom_monitors", monitor_id):
        return jsonify({"error": "监控项不存在"}), 404
    dbm.delete_record(db, "custom_monitors", monitor_id)
    audit("delete_monitor", "custom_monitor", monitor_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/monitors/<int:monitor_id>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_monitor(monitor_id):
    """启用/禁用监控项"""
    db = current_app.config["DB_PATH"]
    m = dbm.get_record(db, "custom_monitors", monitor_id)
    if not m:
        return jsonify({"error": "监控项不存在"}), 404
    new_enabled = 0 if m["enabled"] else 1
    dbm.update_record(db, "custom_monitors", monitor_id,
                      {"enabled": new_enabled})
    audit("toggle_monitor", "custom_monitor", monitor_id,
          {"enabled": new_enabled})
    return jsonify({"ok": True, "enabled": new_enabled})


@alert_bp.route("/alerts/monitors/<int:monitor_id>/check", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def check_monitor(monitor_id):
    """手动触发 SSL 探测"""
    from ..ssl_monitor import get_ssl_monitor
    mon = get_ssl_monitor()
    if not mon:
        return jsonify({"error": "SSL 监控未启动"}), 503
    result = mon.probe_one_now(monitor_id)
    if "error" in result and len(result) == 1:
        return jsonify(result), 400
    return jsonify(result)


@alert_bp.route("/alerts/monitors/config")
@require_auth
def get_monitor_config():
    """获取全局到期监控配置"""
    db = current_app.config["DB_PATH"]
    rows = dbm.list_records(db, "platform_settings",
                            "key=?", ("custom_monitor_config",))
    if rows and rows[0]["value"]:
        try:
            return jsonify(json.loads(rows[0]["value"]))
        except ValueError:
            pass
    return jsonify({"default_warn_days": 30, "default_severity": "P1",
                    "default_channels": ["feishu"], "ssl_check_interval": 3600})


@alert_bp.route("/alerts/monitors/config", methods=["PUT"])
@require_auth
@require_role("admin")
def put_monitor_config():
    """更新全局到期监控配置"""
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    try:
        warn_days = int(body.get("default_warn_days", 30))
        ssl_interval = int(body.get("ssl_check_interval", 3600))
    except (TypeError, ValueError):
        return jsonify({"error": "default_warn_days 和 ssl_check_interval 必须为数字"}), 400
    cfg = {
        "default_warn_days": warn_days,
        "default_severity": body.get("default_severity", "P1"),
        "default_channels": body.get("default_channels", ["feishu"]),
        "ssl_check_interval": ssl_interval,
    }
    if cfg["default_severity"] not in SEVERITIES:
        return jsonify({"error": "severity 非法"}), 400
    now = int(time.time())
    value = json.dumps(cfg, ensure_ascii=False)
    rows = dbm.list_records(db, "platform_settings",
                            "key=?", ("custom_monitor_config",))
    if rows:
        dbm.update_record(db, "platform_settings", rows[0]["id"],
                          {"value": value, "updated_at": now})
    else:
        dbm.create_record(db, "platform_settings",
                          {"key": "custom_monitor_config", "value": value,
                           "updated_at": now})
    audit("update_monitor_config", "config", 0, {"config": cfg})
    return jsonify({"ok": True})


def _calc_days_left(expiry_at):
    """根据 expiry_at (epoch 秒) 自动计算剩余天数"""
    if not expiry_at:
        return None
    return max(0, (int(expiry_at) - int(time.time())) // 86400)


# ---------- 批量操作 ----------

@alert_bp.route("/alerts/monitors/batch", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def monitor_batch():
    """批量操作监控项：enable/disable/delete/update"""
    body = request.get_json(silent=True) or {}
    action = body.get("action")  # enable / disable / delete / update
    ids = body.get("ids") or []
    if action not in ("enable", "disable", "delete", "update"):
        return jsonify({"error": "action 需为 enable/disable/delete/update"}), 400
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids 不能为空"}), 400
    if len(ids) > 200:
        return jsonify({"error": "单次最多操作 200 条"}), 400
    db = current_app.config["DB_PATH"]
    int_ids = [int(i) for i in ids]
    if action == "enable":
        count = dbm.batch_update_monitors(db, int_ids, {"enabled": 1})
    elif action == "disable":
        count = dbm.batch_update_monitors(db, int_ids, {"enabled": 0})
    elif action == "delete":
        count = 0
        for mid in int_ids:
            if dbm.delete_record(db, "custom_monitors", mid):
                count += 1
    elif action == "update":
        fields = body.get("fields") or {}
        count = dbm.batch_update_monitors(db, int_ids, fields)
    audit(f"batch_{action}_monitors", "custom_monitor", 0,
          {"ids": ids, "count": count})
    return jsonify({"ok": True, "count": count})


# ---------- 导入导出 ----------

@alert_bp.route("/alerts/monitors/export")
@require_auth
def export_monitors():
    """导出监控项为 JSON"""
    db = current_app.config["DB_PATH"]
    fmt = request.args.get("format", "json")
    rows = dbm.list_records(db, "custom_monitors", "", (), "id")
    if fmt == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "type", "name", "target", "port", "expiry_at",
                         "days_left", "warn_days", "severity", "owner",
                         "renewal_status", "enabled", "error"])
        for r in rows:
            writer.writerow([r.get("id"), r.get("type"), r.get("name"),
                             r.get("target"), r.get("port"), r.get("expiry_at"),
                             r.get("days_left"), r.get("warn_days"),
                             r.get("severity"), r.get("owner", ""),
                             r.get("renewal_status", ""), r.get("enabled"),
                             r.get("error", "")])
        from flask import Response
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=monitors.csv"})
    return jsonify(rows)


@alert_bp.route("/alerts/monitors/import", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def import_monitors():
    """批量导入监控项 (JSON 数组)"""
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return jsonify({"error": "需为 JSON 数组"}), 400
    if len(body) > 500:
        return jsonify({"error": "单次最多导入 500 条"}), 400
    now = int(time.time())
    created = 0
    errors = []
    for idx, item in enumerate(body):
        err = _validate_monitor(item)
        if err:
            errors.append(f"第 {idx+1} 条: {err}")
            continue
        expiry_at = item.get("expiry_at")
        days_left = _calc_days_left(expiry_at)
        try:
            dbm.create_record(db, "custom_monitors", dict(
                type=item["type"], name=item["name"].strip(),
                target=item["target"].strip(),
                port=item.get("port", 443),
                expiry_at=expiry_at, days_left=days_left,
                warn_days=item.get("warn_days", 0),
                severity=item.get("severity", ""),
                channels=item.get("channels", []),
                enabled=1 if item.get("enabled", 1) else 0,
                owner=item.get("owner", ""),
                renewal_status="",
                tiered_thresholds=item.get("tiered_thresholds", ""),
                detail=item.get("detail", {}),
                error="",
                created_at=now, updated_at=now))
            created += 1
        except Exception as e:
            errors.append(f"第 {idx+1} 条: {e}")
    audit("import_monitors", "custom_monitor", 0,
          {"created": created, "errors": len(errors)})
    return jsonify({"ok": True, "created": created,
                    "errors": errors[:10]})


# ---------- 监控模板 ----------

MONITOR_TEMPLATES = [
    {"type": "ssl", "name": "SSL 证书监控", "target": "example.com",
     "port": 443, "warn_days": 30, "remark": "自动探测 SSL 证书到期"},
    {"type": "cloud_renewal", "name": "阿里云 ECS 续费", "target": "阿里云 ECS",
     "port": 0, "warn_days": 15, "remark": "云产品续费提醒"},
    {"type": "cloud_renewal", "name": "腾讯云 COS 续费", "target": "腾讯云 COS",
     "port": 0, "warn_days": 15, "remark": "云产品续费提醒"},
    {"type": "license", "name": "软件 License 授权", "target": "License 标识",
     "port": 0, "warn_days": 30, "remark": "License 到期提醒"},
    {"type": "domain", "name": "域名到期监控", "target": "example.com",
     "port": 0, "warn_days": 30, "remark": "WHOIS 查询域名到期"},
    {"type": "custom", "name": "自定义到期监控", "target": "自定义目标",
     "port": 0, "warn_days": 7, "remark": "自定义到期提醒"},
]


@alert_bp.route("/alerts/monitors/templates")
@require_auth
def list_monitor_templates():
    """获取监控项模板列表"""
    return jsonify(MONITOR_TEMPLATES)


# ---------- 续期状态跟踪 ----------

@alert_bp.route("/alerts/monitors/<int:monitor_id>/renewal", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def update_renewal_status(monitor_id):
    """更新续期状态并记录日志"""
    db = current_app.config["DB_PATH"]
    m = dbm.get_record(db, "custom_monitors", monitor_id)
    if not m:
        return jsonify({"error": "监控项不存在"}), 404
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")  # pending / in_progress / completed / failed
    valid_statuses = ("pending", "in_progress", "completed", "failed", "")
    if new_status not in valid_statuses:
        return jsonify({"error": f"status 需为 {valid_statuses}"}), 400
    remark = body.get("remark", "")
    dbm.update_record(db, "custom_monitors", monitor_id, dict(
        renewal_status=new_status, updated_at=int(time.time())))
    # 记录续期日志
    dbm.create_renewal_log(db, monitor_id, action=new_status or "reset",
                           operator=g.user.get("username", ""),
                           detail={"remark": remark})
    audit("update_renewal_status", "custom_monitor", monitor_id,
          {"status": new_status})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/monitors/<int:monitor_id>/renewal-logs")
@require_auth
def get_renewal_logs(monitor_id):
    """获取监控项的续期操作日志"""
    db = current_app.config["DB_PATH"]
    logs = dbm.list_renewal_logs(db, monitor_id)
    return jsonify(logs)


# ---------- WHOIS 域名到期探测 ----------

def _whois_lookup(domain: str) -> dict:
    """WHOIS 查询域名到期时间（简化版，支持主流后缀）"""
    import socket
    import re
    try:
        # 提取顶级域名对应的 WHOIS 服务器
        parts = domain.split(".")
        if len(parts) < 2:
            return {"error": "域名格式不正确"}
        tld = parts[-1].lower()
        whois_servers = {
            "com": "whois.verisign-grs.com",
            "net": "whois.verisign-grs.com",
            "org": "whois.pir.org",
            "cn": "whois.cnnic.cn",
            "io": "whois.nic.io",
            "cc": "ccwhois.verisign-grs.com",
            "info": "whois.afilias.net",
            "asia": "whois.nic.asia",
            "co": "whois.nic.co",
            "me": "whois.nic.me",
            "tv": "whois.nic.tv",
            "biz": "whois.biz",
            "name": "whois.nic.name",
            "mobi": "whois.afilias.net",
            "pro": "whois.registrypro.pro",
            "travel": "whois.nic.travel",
            "jobs": "whois.nic.jobs",
            "cat": "whois.nic.cat",
            "tel": "whois.nic.tel",
            "xxx": "whois.nic.xxx",
            "aero": "whois.aero",
            "coop": "whois.nic.coop",
            "museum": "whois.nic.museum",
            "post": "whois.dotpostregistry.net",
            "geo": "whois.nic.geo",
        }
        whois_server = whois_servers.get(tld)
        if not whois_server:
            return {"error": f"不支持的顶级域名: {tld}"}
        # 连接 WHOIS 服务器
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect((whois_server, 43))
        s.send((domain + "\r\n").encode())
        response = b""
        while True:
            data = s.recv(4096)
            if not data:
                break
            response += data
        s.close()
        text = response.decode("utf-8", errors="ignore")
        # 解析到期时间
        patterns = [
            r"Registry Expiry Date:\s*(\d{4}-\d{2}-\d{2})",
            r"Expiration Date:\s*(\d{4}-\d{2}-\d{2})",
            r"Expiry Date:\s*(\d{4}-\d{2}-\d{2})",
            r"Expiration Time:\s*(\d{4}-\d{2}-\d{2})",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=__import__("datetime").timezone.utc)
                expiry_at = int(dt.timestamp())
                days_left = max(0, (expiry_at - int(time.time())) // 86400)
                return {"expiry_at": expiry_at, "days_left": days_left, "error": None}
        return {"error": "未找到到期时间"}
    except Exception as e:
        return {"error": str(e)}


@alert_bp.route("/alerts/monitors/whois", methods=["POST"])
@require_auth
def whois_check():
    """手动 WHOIS 查询域名到期"""
    body = request.get_json(silent=True) or {}
    domain = (body.get("domain") or "").strip()
    if not domain:
        return jsonify({"error": "域名不能为空"}), 400
    result = _whois_lookup(domain)
    if result.get("error"):
        return jsonify(result), 400
    return jsonify(result)


@alert_bp.route("/alerts/monitors/<int:monitor_id>/whois", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def whois_check_monitor(monitor_id):
    """手动触发域名 WHOIS 查询并更新监控项"""
    db = current_app.config["DB_PATH"]
    m = dbm.get_record(db, "custom_monitors", monitor_id)
    if not m or m.get("type") != "domain":
        return jsonify({"error": "监控项不存在或非域名类型"}), 404
    result = _whois_lookup(m["target"])
    now = int(time.time())
    if result.get("error"):
        dbm.update_record(db, "custom_monitors", monitor_id, dict(
            last_checked=now, error=result["error"], updated_at=now))
        return jsonify(result), 400
    dbm.update_record(db, "custom_monitors", monitor_id, dict(
        last_checked=now,
        expiry_at=result["expiry_at"],
        days_left=result["days_left"],
        error="",
        updated_at=now))
    return jsonify(result)


# ---------- 到期监控统计 ----------

@alert_bp.route("/alerts/monitors/stats")
@require_auth
def monitor_stats():
    """到期监控统计：按类型/状态分组"""
    db = current_app.config["DB_PATH"]
    rows = dbm.list_records(db, "custom_monitors", "", (), "id")
    by_type = {}
    by_status = {"total": len(rows), "expiring": 0, "expired": 0, "healthy": 0}
    for m in rows:
        t = m.get("type", "unknown")
        by_type.setdefault(t, {"total": 0, "expiring": 0, "expired": 0})
        by_type[t]["total"] += 1
        dl = m.get("days_left")
        if dl is not None:
            if dl <= 0:
                by_status["expired"] += 1
                by_type[t]["expired"] += 1
            elif dl <= 30:
                by_status["expiring"] += 1
                by_type[t]["expiring"] += 1
            else:
                by_status["healthy"] += 1
    # 按类型统计
    type_stats = [{"type": t, **v} for t, v in by_type.items()]
    return jsonify({"summary": by_status, "by_type": type_stats})


# ---------- 时间线视图 ----------

@alert_bp.route("/alerts/monitors/timeline")
@require_auth
def monitor_timeline():
    """到期监控时间线：按到期时间排序，返回未来 N 天到期的监控项"""
    db = current_app.config["DB_PATH"]
    days = min(365, max(1, request.args.get("days", 90, type=int)))
    now = int(time.time())
    cutoff = now + days * 86400
    rows = dbm.list_records(db, "custom_monitors",
                            "enabled=1 AND expiry_at IS NOT NULL AND expiry_at <= ?",
                            (cutoff,), "expiry_at")
    items = []
    for m in rows:
        expiry_at = m.get("expiry_at") or 0
        days_left = max(0, (expiry_at - now) // 86400)
        items.append({
            "id": m["id"], "name": m["name"], "type": m["type"],
            "target": m["target"], "expiry_at": expiry_at,
            "days_left": days_left, "owner": m.get("owner", ""),
            "renewal_status": m.get("renewal_status", ""),
        })
    return jsonify(items)


def _validate_monitor(body, partial=False) -> str:
    """监控项校验；返回错误信息，空串=通过"""
    if not partial:
        if body.get("type") not in MONITOR_TYPES:
            return "type 非法"
        if not (body.get("name") or "").strip():
            return "名称必填"
        if not (body.get("target") or "").strip():
            return "目标必填"
    if "type" in body and body["type"] not in MONITOR_TYPES:
        return "type 非法"
    if "port" in body:
        try:
            p = int(body["port"])
            # 非 SSL 类型允许 port=0
            if body.get("type") != "ssl" and p == 0:
                pass
            elif p < 1 or p > 65535:
                return "port 需在 1-65535 之间"
        except (TypeError, ValueError):
            return "port 必须为数字"
    if "warn_days" in body:
        try:
            wd = int(body["warn_days"])
            if wd < 0:
                return "warn_days 需为非负整数"
        except (TypeError, ValueError):
            return "warn_days 必须为数字"
    if "severity" in body and body["severity"] and body["severity"] not in SEVERITIES:
        return "severity 非法"
    if "channels" in body:
        ch = body.get("channels") or []
        if not isinstance(ch, list):
            return "channels 需为列表"
        if ch and not set(ch) <= set(CHANNELS):
            return "channels 包含非法渠道"
    # 非 SSL 类型需要到期时间（domain 类型可自动 WHOIS 查询）
    if not partial and body.get("type") not in ("ssl", "domain"):
        if not body.get("expiry_at"):
            return "非 SSL 类型需提供 expiry_at"
    return ""
