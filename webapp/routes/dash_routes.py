"""大屏聚合路由：/api/dashboard/overview + /api/audit"""
import time
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app

from .. import db as dbm
from ..auth import require_auth, require_role

dash_bp = Blueprint("dash", __name__)


def _day_start(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d")


def _recent_days(n: int) -> list:
    return [(_day_start((datetime.now() - timedelta(days=i)).timestamp()))
            for i in range(n - 1, -1, -1)]


def _trend(tasks: list, days: int) -> dict:
    days_list = _recent_days(days)
    trend = {d: {"total": 0, "success": 0, "failed": 0} for d in days_list}
    for t in tasks:
        if not t.get("started_at"):
            continue
        d = _day_start(t["started_at"])
        if d in trend:
            trend[d]["total"] += 1
            if t["status"] == "success":
                trend[d]["success"] += 1
            elif t["status"] == "failed":
                trend[d]["failed"] += 1
    return trend


@dash_bp.route("/dashboard/overview")
@require_auth
def overview():
    db = current_app.config["DB_PATH"]
    projects = dbm.list_records(db, "projects")
    # 统计窗口为最近 500 条任务（list_tasks 上限约定），高频率发布下 7/30 天窗口早期可能被截断
    tasks = dbm.list_tasks(db, limit=500)
    total = len(tasks)
    success = sum(1 for t in tasks if t["status"] == "success")
    failed = sum(1 for t in tasks if t["status"] == "failed")
    running = sum(1 for t in tasks if t["status"] == "running")
    servers = dbm.list_records(db, "servers")
    # 计算任务平均耗时（用于性能评分）
    completed = [t for t in tasks if t["status"] in ("success", "failed")
                 and t.get("started_at") and t.get("finished_at")]
    avg_duration = sum(t["finished_at"] - t["started_at"] for t in completed) / len(completed) if completed else 0
    out = {
        "projects": len(projects),
        "services": sum(len(dbm.list_services(db, p["id"])) for p in projects),
        "environments": sum(len(dbm.list_environments(db, p["id"])) for p in projects),
        "servers": len(servers),
        "tasks_total": total, "tasks_success": success,
        "tasks_failed": failed, "tasks_running": running,
        "tasks_avg_duration": avg_duration,
        "trend7": _trend(tasks, 7),
        "trend30": _trend(tasks, 30),
        # 零任务时 None（前端显示占位符），避免误导性 100%
        "success_rate": round(success / total * 100, 1) if total else None,
        "recent": [{"id": t["id"], "type": t["type"], "name": t["name"],
                    "env": t["env"], "version": t["version"],
                    "previous_version": t["previous_version"],
                    "status": t["status"], "started_at": t["started_at"]}
                   for t in tasks[:10]],
    }
    return jsonify(out)


@dash_bp.route("/audit")
@require_auth
@require_role("admin")
def audit_logs():
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    if page < 1:
        page = 1
    if not (1 <= page_size <= 200):
        page_size = 50
    rows, total = dbm.list_audit_logs_paged(current_app.config["DB_PATH"], page, page_size)
    return jsonify({"rows": rows, "total": total, "page": page, "page_size": page_size})


@dash_bp.route("/monitor/overview")
@require_auth
def monitor_overview():
    from ..monitor import get_monitor
    project_id = request.args.get("project_id", type=int)
    env = request.args.get("env", "")
    if not project_id:
        return jsonify({"error": "project_id 必填"}), 400
    mon = get_monitor()
    data = mon.get_overview(project_id, env) if mon else {"services": {}, "servers": [], "ts": 0}
    return jsonify(data)


@dash_bp.route("/monitor/refresh", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def monitor_refresh():
    from ..monitor import get_monitor
    mon = get_monitor()
    if mon:
        mon.refresh()
    return jsonify({"ok": True})
