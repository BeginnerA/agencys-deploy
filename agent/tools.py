"""工具注册表：Agent 可调用的平台能力（read / write 两级权限）

设计约定：
    - 每个工具 = ToolSpec(name, description, params, permission, fn)
    - permission: read=全体角色；write=operator/admin（write 型动作全部落审计或
      agent_runs 审批单，绝不绕过既有通道）
    - 工具返回纯数据（list/dict/str），由 core 统一 JSON 序列化并截断
    - 敏感信息（SSH 私钥/密码/Nacos 口令）一律不出现在工具输出中
"""
import json
import os
import re
import time

from webapp import db as dbm
from webapp.deploy_scheduler import check_release_window

from . import memory as memory_mod
from . import advisor as advisor_mod
from . import executor as executor_mod


class ToolError(Exception):
    """工具业务错误（参数非法/资源不存在），message 面向用户"""


# ---------- 通用帮助 ----------

def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, OSError, ValueError):
        return "-"


def _resolve_project(db_path: str, project: str = "") -> dict:
    """project 支持 id/code/name 三种指代；缺省返回 None"""
    if not project:
        return None
    if str(project).isdigit():
        rec = dbm.get_record(db_path, "projects", int(project))
        if rec:
            return rec
        raise ToolError(f"项目 ID 不存在: {project}")
    rows = dbm.list_records(db_path, "projects",
                            "code=? OR name=?", (project, project))
    if not rows:
        raise ToolError(f"未找到项目「{project}」（支持 code/name）")
    return rows[0]


def _find_service(db_path: str, project: str = "", service: str = "") -> dict:
    """服务名（可跨项目按首个匹配）+ 可选项目约束"""
    proj = _resolve_project(db_path, project)
    if service:
        where, params = "name=?", (service,)
        if proj:
            where += " AND project_id=?"
            params += (proj["id"],)
        rows = dbm.list_records(db_path, "services", where, params)
        if not rows:
            raise ToolError(f"未找到服务「{service}」"
                            + (f"（项目 {proj['code']}）" if proj else ""))
        return rows[0]
    raise ToolError("缺少服务名 service")


def _compact_alert(a: dict) -> dict:
    """告警行压缩：target_info 解析出 project/env/service 便于阅读"""
    out = {"id": a["id"], "severity": a.get("severity"), "subject": a.get("subject"),
           "status": a.get("status"),
           "first_seen": _fmt_ts(a.get("first_seen_at")),
           "last_seen": _fmt_ts(a.get("last_seen_at"))}
    try:
        ti = json.loads(a.get("target_info") or "{}")
        out["project"] = ti.get("project", "")
        out["env"] = ti.get("env", "")
        out["service"] = ti.get("service", "")
        out["target"] = ti.get("target", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return out


def _service_status_summary(project_id: int, env: str, service: str = "") -> dict:
    """监控缓存摘要：服务在线/异常与服务器可达性（monitor 未启动时返回空）"""
    try:
        from webapp.monitor import get_monitor
        mon = get_monitor()
        if not mon:
            return {"available": False, "reason": "monitor 未运行（服务未启动监控）"}
        ov = mon.get_overview(project_id, env) or {}
        services = ov.get("services") or {}
        servers = ov.get("servers") or []
        if service:
            return {"available": True, "ts": ov.get("ts", 0),
                    "service": services.get(service, {})}
        return {"available": True, "ts": ov.get("ts", 0),
                "services": services,
                "servers": [{"host": s.get("host"), "ok": s.get("ok"),
                             "error": s.get("error", "")} for s in servers]}
    except Exception:
        return {"available": False, "reason": "monitor 查询失败"}


def _search_log_entries(db_path: str, keyword: str = "", service: str = "",
                        env: str = "", minutes: int = 60, limit: int = 20) -> list:
    """日志检索（与 log_routes 同构；keyword 走 FTS5 content 检索）"""
    from_ts = int(time.time()) - max(1, min(minutes, 24 * 60)) * 60
    conn = dbm.get_conn(db_path)
    try:
        conds, params = [], []
        if keyword:
            fts_where = ["log_fts MATCH ?"]
            fts_params = [f"content:{keyword}"]
            if service:
                fts_where.append("service = ?")
                fts_params.append(service)
            if env:
                fts_where.append("env = ?")
                fts_params.append(env)
            conds.append("le.id IN (SELECT rowid FROM log_fts WHERE "
                         + " AND ".join(fts_where) + ")")
            params.extend(fts_params)
        else:
            if service:
                conds.append("le.service = ?")
                params.append(service)
            if env:
                conds.append("le.env = ?")
                params.append(env)
        conds.append("le.ts >= ?")
        params.append(from_ts)
        rows = conn.execute(
            "SELECT le.id, le.ts, le.env, le.service, le.container, "
            "substr(le.content,1,200) AS content FROM log_entries le "
            "WHERE " + " AND ".join(conds) + " ORDER BY le.ts DESC LIMIT ?",
            params + [max(1, min(limit, 100))]).fetchall()
        return [{"id": r["id"], "ts": _fmt_ts(r["ts"]),
                 "env": r["env"], "service": r["service"],
                 "container": r["container"], "content": r["content"]}
                for r in rows]
    except Exception:
        return [{"error": "检索失败（关键词可能含 FTS5 非法字符）"}]
    finally:
        conn.close()


# ---------- 工具实现（read） ----------

def _list_projects(ctx, **kw) -> dict:
    db = ctx.db_path
    out = []
    for p in dbm.list_records(db, "projects", order="id"):
        svc_n = len(dbm.list_services(db, p["id"]))
        env_n = len(dbm.list_environments(db, p["id"]))
        pl_n = len(dbm.list_pipelines(db, p["id"]))
        out.append({"id": p["id"], "name": p["name"], "code": p["code"],
                    "description": p.get("description", ""),
                    "services": svc_n, "environments": env_n,
                    "pipelines": pl_n})
    return {"projects": out}


def _get_project(ctx, project=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError("缺少项目 project（id/code/name）")
    services = [{"id": s["id"], "name": s["name"], "image_name": s["image_name"],
                 "enabled": s.get("enabled", 1)}
                for s in dbm.list_services(db, proj["id"])]
    envs = []
    for e in dbm.list_environments(db, proj["id"]):
        envs.append({"id": e["id"], "name": e["name"],
                     "is_default": e.get("is_default", 0),
                     "servers": [s["host"] for s in dbm.list_env_servers(db, e["id"])]})
    pipelines = [p["name"] for p in dbm.list_pipelines(db, proj["id"])]
    return {"project": {"id": proj["id"], "name": proj["name"],
                        "code": proj["code"],
                        "description": proj.get("description", "")},
            "services": services, "environments": envs,
            "pipelines": pipelines}


def _list_services(ctx, project=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if proj:
        services = dbm.list_services(db, proj["id"])
    else:
        services = dbm.list_records(db, "services", order="id")
    return {"services": [{"id": s["id"], "name": s["name"],
                          "project_id": s["project_id"],
                          "image_name": s["image_name"],
                          "service_name": s["service_name"],
                          "enabled": s.get("enabled", 1)}
                         for s in services]}


def _list_environments(ctx, project=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError("缺少项目 project")
    return {"environments": [
        {"id": e["id"], "name": e["name"], "is_default": e.get("is_default", 0),
         "monitor_enabled": e.get("monitor_enabled", 1)}
        for e in dbm.list_environments(db, proj["id"])]}


def _env_servers(ctx, project=None, env=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError("缺少项目 project")
    if not env:
        raise ToolError("缺少环境 env")
    env_obj = dbm.get_env_by_name(db, proj["id"], env)
    if not env_obj:
        raise ToolError(f"项目 {proj['code']} 下不存在环境「{env}」")
    return {"servers": [
        {"host": s["host"], "nacos_ip": s.get("nacos_ip", ""),
         "remark": s.get("remark", ""), "order_no": s.get("order_no", 0)}
        for s in dbm.list_env_servers(db, env_obj["id"])]}


def _service_status(ctx, project=None, env="", service="", **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if service and not proj:
        # 服务名反查项目（与发布 CLI 同名语义）
        svc = _find_service(db, "", service)
        proj = dbm.get_record(db, "projects", svc["project_id"])
    if not proj:
        # 无项目：全局聚合（只给摘要，避免上下文爆炸）
        summary = _service_status_summary(0, "", "")
        svc_list = summary.get("services", {})
        bad = [k for k, v in svc_list.items()
               if v.get("status") not in ("online", "not_deployed")]
        return {"summary": f"共 {len(svc_list)} 个服务在探测，异常 {len(bad)} 个",
                "abnormal": bad[:20],
                "servers": summary.get("servers", []),
                "ts": summary.get("ts", 0),
                "hint": "可按 project/env/service 缩小范围查询详情"}
    return _service_status_summary(proj["id"], env, service)


def _recent_tasks(ctx, project=None, status="", limit=20, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    where, params = [], []
    if proj:
        where.append("project_id=?")
        params.append(proj["id"])
    if status in ("pending", "running", "success", "failed"):
        where.append("status=?")
        params.append(status)
    rows = dbm.list_records(db, "tasks", " AND ".join(where), tuple(params),
                            "id DESC")[:max(1, min(limit, 100))]
    return {"tasks": [
        {"id": t["id"], "type": t["type"], "name": t["name"], "env": t["env"],
         "version": t["version"], "previous_version": t["previous_version"],
         "status": t["status"],
         "started_at": _fmt_ts(t.get("started_at")),
         "finished_at": _fmt_ts(t.get("finished_at"))}
        for t in rows]}


def _task_detail(ctx, task_id=None, **kw) -> dict:
    db = ctx.db_path
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 task_id（整数）")
    task = dbm.get_task(db, task_id)
    if not task:
        raise ToolError(f"任务不存在: #{task_id}")
    log_tail = advisor_mod._task_log_tail(task, chars=1500)
    return {"task": {k: task[k] for k in (
        "id", "type", "name", "env", "version", "previous_version",
        "status", "log_file", "started_at", "finished_at")},
        "log_tail": log_tail}


def _list_alerts(ctx, status="firing", severity="", limit=20,
                 project="", env="", service="", **kw) -> dict:
    db = ctx.db_path
    if status not in ("firing", "acked", "resolved", ""):
        status = ""
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    if severity in ("P0", "P1", "P2", "P3"):
        conds.append("severity=?")
        params.append(severity)
    proj = _resolve_project(db, project or "") if project else None
    if proj:
        conds.append("target_info LIKE ?")
        params.append(f'%"{proj["code"]}"%')
    if service:
        conds.append("(target_info LIKE ? OR subject LIKE ?)")
        params += [f"%{service}%", f"%{service}%"]
    if env:
        conds.append("target_info LIKE ?")
        params.append(f'%"env":"{env}"%')
    conn = dbm.get_conn(db)
    try:
        wsql = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = conn.execute(
            "SELECT * FROM alerts" + wsql + " ORDER BY id DESC LIMIT ?",
            params + [max(1, min(limit, 100))]).fetchall()
        alerts = [_compact_alert(dbm._deserialize("alerts", dict(r)))
                  for r in rows]
        total = conn.execute("SELECT COUNT(*) FROM alerts" + wsql,
                             params).fetchone()[0]
        return {"alerts": alerts, "total": total,
                "status": status or "any"}
    finally:
        conn.close()


def _alert_detail(ctx, alert_id=None, **kw) -> dict:
    db = ctx.db_path
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 alert_id（整数）")
    alert = dbm.get_record(db, "alerts", alert_id)
    if not alert:
        raise ToolError(f"告警不存在: #{alert_id}")
    conn = dbm.get_conn(db)
    try:
        events = conn.execute(
            "SELECT channel, target, ok, created_at, substr(detail,1,200) AS detail "
            "FROM alert_events WHERE alert_id=? ORDER BY id DESC LIMIT 5",
            (alert_id,)).fetchall()
    finally:
        conn.close()
    return {"alert": _compact_alert(alert),
            "description": alert.get("description", ""),
            "ack_by": alert.get("ack_by", ""),
            "resolved_by": alert.get("resolved_by", ""),
            "notify_count": alert.get("notify_count", 0),
            "events": [{"channel": e["channel"], "ok": e["ok"],
                        "at": _fmt_ts(e["created_at"]),
                        "detail": e["detail"]} for e in events]}


def _search_logs(ctx, keyword="", service="", env="", minutes=60,
                 limit=20, **kw) -> dict:
    if not keyword and not service and not env:
        raise ToolError("至少提供 keyword/service/env 之一")
    rows = _search_log_entries(ctx.db_path, keyword=keyword, service=service,
                               env=env, minutes=minutes, limit=limit)
    return {"rows": rows, "count": len(rows),
            "hint": "结果按时间倒序；可用 service/env 收窄；"
                    "用 logs_context 传某条的 id 查看前后文"}


def _logs_context(ctx, id=None, lines=5, **kw) -> dict:
    """日志上下文：按条目 id 取目标行前后 N 行（与 Web 日志中心同等语义）

    line_offset 是"逻辑字节偏移"（采集端按 Linux 源行长度累加，无 \r）；
    Windows 文本模式追加会把 \n 写为 \r\n，故按行读取时需补偿行尾 \r，
    保证与 line_offset 同单位定位（跨平台自洽）。
    """
    if not isinstance(id, int) or isinstance(id, bool) or id <= 0:
        raise ToolError("缺少日志条目 id（来自 search_logs 结果的 id 字段）")
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        lines = 5
    lines = min(lines, 50)
    db = ctx.db_path
    entry = dbm.get_record(db, "log_entries", id)
    if not entry:
        raise ToolError(f"日志条目不存在: id={id}")
    file_path = entry.get("file_path") or ""
    offset = entry.get("line_offset") or 0
    # 路径遍历防护：日志文件必须落在 data/logs/collect 目录内（与 Web 端一致）
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_base = os.path.realpath(os.path.join(root, "data", "logs", "collect"))
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(log_base + os.sep):
        raise ToolError("非法日志文件路径")
    if not os.path.isfile(real_path):
        raise ToolError(f"日志文件不存在: {file_path}")
    try:
        target_line = 0
        prev, pos = [], 0
        with open(real_path, "rb") as f:
            while pos < offset:
                line = f.readline()
                if not line:
                    break
                pos += len(line) - (1 if line.endswith(b"\r\n") else 0)
                target_line += 1
                if len(prev) >= lines:
                    prev.pop(0)
                prev.append(line)
            tail = []
            target = f.readline()
            if target:
                tail.append(target)
            for _ in range(lines):
                line = f.readline()
                if not line:
                    break
                tail.append(line)
        context_lines = [l.decode("utf-8", "ignore").rstrip("\r\n")
                         for l in prev[-lines:] + tail]
    except (ValueError, OSError):
        raise ToolError("日志文件读取失败")
    start = max(0, target_line - lines)
    return {"id": id, "file": file_path, "target_line": target_line,
            "start": start, "end": start + len(context_lines),
            "env": entry.get("env", ""), "service": entry.get("service", ""),
            "lines": context_lines,
            "hint": f"行号区间 {start}~{start + len(context_lines)}"
                    f"（0 基，目标行 {target_line}）"}


def _monitor_metrics(ctx, metric="response_times", project=None, env="",
                     service="", hours=24, limit=100, **kw) -> dict:
    """监控指标查询：response_times（服务响应时间历史，含汇总）/ snapshots（服务器最新快照扩展指标）"""
    db = ctx.db_path
    if metric not in ("response_times", "snapshots"):
        raise ToolError("metric 仅支持 response_times / snapshots")
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 1:
        hours = 24
    hours = min(hours, 168 if metric == "response_times" else 72)
    env_id = None
    if env or project:
        # 环境名在项目内解析（跨项目可重名）；查全局时 project/env 全部省略
        if not (env and project):
            raise ToolError("按环境过滤需同时提供 project 与 env；查全局时两者均省略")
        proj = _resolve_project(db, project)
        env_obj = dbm.get_env_by_name(db, proj["id"], env)
        if not env_obj:
            raise ToolError(f"项目 {proj['code']} 下不存在环境「{env}」")
        env_id = env_obj["id"]
    if metric == "response_times":
        rows = dbm.get_response_time_history(db, env_id, service or None, hours)
        total = len(rows)
        ok_n = sum(1 for r in rows if r.get("ok"))
        mss = [r["response_ms"] for r in rows
               if isinstance(r.get("response_ms"), (int, float))
               and r["response_ms"] > 0]
        stats = {"count": total,
                 "ok_rate": round(ok_n / total, 3) if total else None,
                 "avg_ms": round(sum(mss) / len(mss), 1) if mss else None,
                 "max_ms": max(mss) if mss else None,
                 "min_ms": min(mss) if mss else None}
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            limit = 100
        limit = min(limit, 500)
        out_rows = [{"ts": _fmt_ts(r["created_at"]), "env_id": r["env_id"],
                     "service": r["service"], "host": r["host"],
                     "response_ms": r["response_ms"], "ok": bool(r["ok"])}
                    for r in rows[:limit]]
        return {"metric": "response_times", "hours": hours, "stats": stats,
                "rows": out_rows, "returned": len(out_rows),
                "hint": "response_ms<0 表示探测失败；可用 service 或 project+env 收窄；"
                        "stats 汇总覆盖查询窗口内全部样本（上限 500 条）"}
    # snapshots：每台服务器取最新一条快照（扩展指标）
    rows = dbm.get_monitor_latest(db, env_id, hours)
    env_names = {e["id"]: e["name"]
                 for e in dbm.list_records(db, "environments")}
    out = [{"ts": _fmt_ts(r["created_at"]),
            "env": env_names.get(r["env_id"], r["env_id"]), "host": r["host"],
            "swap": r.get("swap"),
            "net_rx": r.get("net_rx"), "net_tx": r.get("net_tx"),
            "disk_read": r.get("disk_read"), "disk_write": r.get("disk_write"),
            "tcp_established": r.get("tcp_established"),
            "tcp_time_wait": r.get("tcp_time_wait"),
            "tcp_close_wait": r.get("tcp_close_wait"),
            "restart_count": r.get("restart_count"),
            "uptime_hours": r.get("uptime_hours"),
            "docker_images_mb": round((r.get("docker_images_bytes") or 0) / 1048576, 1),
            "docker_containers_mb": round((r.get("docker_containers_bytes") or 0) / 1048576, 1),
            "docker_volumes_mb": round((r.get("docker_volumes_bytes") or 0) / 1048576, 1),
            "ssl_days": r.get("ssl_days")}
           for r in rows]
    return {"metric": "snapshots", "hours": hours, "rows": out,
            "count": len(out),
            "hint": "每台服务器最新一条快照；swap 为使用率百分比(%)、"
                    "net_rx/net_tx 为 bytes/s、disk_read/write 为 sectors/s、"
                    "docker_*_mb 为占用 MB、ssl_days 为证书剩余天数"}


def _release_window(ctx, project=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError("缺少项目 project")
    win = check_release_window(db, proj["id"])
    if not win:
        return {"project": proj["code"], "window": None,
                "message": "当前无封网/告警窗口，可正常发布"}
    return {"project": proj["code"],
            "window": {"name": win.get("name"), "level": win.get("block_level"),
                       "start": _fmt_ts(win.get("start_at")),
                       "end": _fmt_ts(win.get("end_at"))},
            "message": ("禁止发布" if win["block_level"] == "block"
                        else "发布需告警确认")}


def _canary_configs(ctx, project=None, **kw) -> dict:
    db = ctx.db_path
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError("缺少项目 project")
    rows = dbm.list_records(db, "canary_configs", "project_id=?",
                            (proj["id"],), "id DESC")
    return {"canary_configs": [
        {"id": c["id"], "name": c["name"], "is_default": c.get("is_default", 0),
         "steps": c.get("steps") or []} for c in rows]}


def _cert_monitors(ctx, **kw) -> dict:
    db = ctx.db_path
    rows = dbm.list_records(db, "custom_monitors", order="id DESC")[:50]
    monitors = []
    for m in rows:
        if not m.get("enabled", 1):
            continue
        monitors.append({"id": m["id"], "type": m["type"], "name": m["name"],
                         "target": m["target"], "port": m.get("port"),
                         "days_left": round(m.get("days_left") or 0, 1)
                         if m.get("days_left") is not None else None,
                         "expiry_at": _fmt_ts(m.get("expiry_at")),
                         "renewal_status": m.get("renewal_status", ""),
                         "error": m.get("error", "")})
    return {"monitors": monitors}


def _audit_logs(ctx, limit=20, **kw) -> dict:
    rows = dbm.list_audit_logs(ctx.db_path, max(1, min(limit, 100)))
    return {"audit_logs": [
        {"id": a["id"], "username": a.get("username", ""),
         "action": a.get("action", ""), "target_type": a.get("target_type", ""),
         "target_id": a.get("target_id", 0),
         "at": _fmt_ts(a.get("created_at")),
         "detail": a.get("detail")} for a in rows]}


def _search_cases(ctx, keyword="", service="", limit=5, **kw) -> dict:
    cases = memory_mod.search_cases(ctx.db_path, keyword=keyword,
                                    service=service or None,
                                    limit=max(1, min(limit, 10)))
    return {"cases": cases, "count": len(cases),
            "hint": "案例含 symptom/root_cause/action/lesson，可参考处置"}


def _case_detail(ctx, case_id=None, **kw) -> dict:
    try:
        case_id = int(case_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 case_id（整数）")
    case = memory_mod.get_case(ctx.db_path, case_id)
    if not case:
        raise ToolError(f"案例不存在: #{case_id}")
    return {"case": case}


def _advice_list(ctx, scope="", ref_id=0, limit=10, status="", **kw) -> dict:
    if scope not in ("", "canary", "task_failed", "alert", "precheck"):
        raise ToolError("scope 仅支持 canary/task_failed/alert/precheck")
    if status and status not in advisor_mod.ADVICE_STATUSES:
        raise ToolError("status 仅支持 pending/adopted/dismissed")
    rows = advisor_mod.list_advice(ctx.db_path, scope=scope,
                                   ref_id=int(ref_id or 0),
                                   limit=max(1, min(limit, 50)),
                                   status=status)
    return {"advice": [{"id": a["id"], "scope": a["scope"],
                        "ref_id": a["ref_id"], "title": a["title"],
                        "body": (a.get("body") or "")[:600],
                        "model": a.get("model", ""),
                        "status": a.get("status", "pending"),
                        "action_ref": a.get("action_ref", ""),
                        "created_at": _fmt_ts(a.get("created_at")),
                        "created_by_name": a.get("created_by_name", "")}
                       for a in rows]}


def _pending_approvals(ctx, limit=10, **kw) -> dict:
    db = ctx.db_path
    rows = dbm.list_records(db, "agent_runs",
                            "status='awaiting_approval'", (), "id DESC")
    return {"pending": [
        {"run_id": r["id"], "kind": r.get("kind", "operation"),
         "name": r.get("name", ""), "env": r.get("env", ""),
         "version": r.get("version", ""),
         "previous_version": r.get("previous_version", ""),
         "created_by_name": r.get("created_by_name", ""),
         "created_at": _fmt_ts(r.get("created_at")),
         "intent": (r.get("intent") or "")[:120]}
        for r in rows[:max(1, min(limit, 50))]]}


def _run_detail(ctx, run_id=None, **kw) -> dict:
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 run_id（整数）")
    run = executor_mod.get_run(ctx.db_path, run_id)
    return {"run": run}


def _service_topology(ctx, metric="topology", project=None, env="",
                      service="", old_id=None, new_id=None, limit=20, **kw) -> dict:
    """服务依赖拓扑与发布编排查询：topology（项目全图）/ impact（服务上下游）/ publish_order（发布顺序建议）/ snapshots（部署快照）/ compare（快照对比）"""
    db = ctx.db_path
    if metric not in ("topology", "impact", "publish_order",
                      "snapshots", "compare"):
        raise ToolError("metric 仅支持 topology/impact/publish_order/snapshots/compare")
    if metric in ("topology", "publish_order", "snapshots") and not project:
        raise ToolError(f"metric={metric} 需要项目 project")
    if metric == "compare" and not (old_id and new_id) and not (project and env):
        raise ToolError("compare 需要 project+env（最近两次快照）或 old_id+new_id")
    if metric == "impact" and not (project and service):
        raise ToolError("metric=impact 需要 project + service（服务名）")

    if metric == "snapshots":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError(f"项目不存在: {project}")
        cond, params = "project_id=?", (proj["id"],)
        if env:
            cond, params = "project_id=? AND env=?", (proj["id"], env)
        rows = dbm.list_records(db, "deployment_snapshots", cond, params,
                                "deployed_at DESC")
        return {"project": proj["code"], "env": env or "全部",
                "snapshots": [{"id": r["id"], "env": r.get("env", ""),
                               "task_id": r.get("task_id", 0),
                               "version": r.get("version", ""),
                               "image_versions": r.get("image_versions") or {},
                               "deployed_at": _fmt_ts(r.get("deployed_at"))}
                              for r in rows[:max(1, min(limit, 50))]],
                "hint": "快照由每次部署成功后自动写入；compare 可对比两次快照的服务版本差异"}

    if metric == "compare":
        if old_id and new_id:
            try:
                old_id, new_id = int(old_id), int(new_id)
            except (TypeError, ValueError):
                raise ToolError("old_id/new_id 必须为整数")
            old = dbm.get_record(db, "deployment_snapshots", old_id)
            new = dbm.get_record(db, "deployment_snapshots", new_id)
            if not old or not new:
                raise ToolError("指定的快照不存在")
        else:
            proj = _resolve_project(db, project or "")
            if not proj:
                raise ToolError(f"项目不存在: {project}")
            rows = dbm.list_records(db, "deployment_snapshots",
                                    "project_id=? AND env=?",
                                    (proj["id"], env), "deployed_at DESC")
            if len(rows) < 2:
                raise ToolError("至少需要 2 个快照才能对比")
            old, new = rows[1], rows[0]
        old_v = old.get("image_versions") or {}
        new_v = new.get("image_versions") or {}
        changes = [{"service": k, "old": old_v.get(k, ""), "new": new_v.get(k, "")}
                   for k in sorted(set(old_v) | set(new_v))
                   if old_v.get(k, "") != new_v.get(k, "")]
        return {"old": {"id": old["id"], "version": old.get("version", ""),
                        "env": old.get("env", ""),
                        "deployed_at": _fmt_ts(old.get("deployed_at"))},
                "new": {"id": new["id"], "version": new.get("version", ""),
                        "env": new.get("env", ""),
                        "deployed_at": _fmt_ts(new.get("deployed_at"))},
                "changes": changes,
                "hint": "changes 为两次快照间服务镜像版本差异（含新增/移除服务）"}

    # topology / impact / publish_order：均基于服务依赖图
    proj = _resolve_project(db, project or "")
    if not proj:
        raise ToolError(f"项目不存在: {project}")
    pid = proj["id"]
    services = dbm.list_services(db, pid, enabled_only=True)
    svc_names = {s["name"] for s in services}
    deps = dbm.list_records(db, "service_dependencies", "project_id=?",
                            (pid,), "id")

    if metric == "topology":
        edges, seen = [], set()
        for d in deps:
            key = (d["source_service"], d["target_service"])
            if key not in seen:
                edges.append({"source": d["source_service"],
                              "target": d["target_service"],
                              "type": d.get("dep_type", "manual")})
                seen.add(key)
        for svc in services:
            dubbo = svc.get("dubbo_services") or []
            if isinstance(dubbo, str):
                try:
                    dubbo = json.loads(dubbo)
                except (ValueError, TypeError):
                    dubbo = []
            for d in dubbo if isinstance(dubbo, list) else []:
                target = d if isinstance(d, str) else d.get("service", "")
                if target and target in svc_names:
                    key = (svc["name"], target)
                    if key not in seen:
                        edges.append({"source": svc["name"], "target": target,
                                      "type": "dubbo"})
                        seen.add(key)
        return {"project": proj["code"],
                "nodes": [{"name": s["name"],
                           "service_name": s.get("service_name", "")}
                          for s in services],
                "edges": edges,
                "hint": "edges 含手动依赖（manage_dependencies 维护）与 dubbo 自动检测；"
                        "impact 查单服务上下游、publish_order 查建议发布顺序"}

    if metric == "impact":
        if service not in svc_names:
            raise ToolError(f"服务不存在或不属于该项目: {service}")
        upstream = [{"name": d["source_service"],
                     "type": d.get("dep_type", "manual")}
                    for d in deps if d["target_service"] == service]
        downstream = [{"name": d["target_service"],
                       "type": d.get("dep_type", "manual")}
                      for d in deps if d["source_service"] == service]
        adj_up, adj_down = {}, {}
        for d in deps:
            adj_up.setdefault(d["target_service"], []).append(d["source_service"])
            adj_down.setdefault(d["source_service"], []).append(d["target_service"])
        indirect_up = {uu for u in upstream for uu in adj_up.get(u["name"], [])
                       if uu != service}
        indirect_down = {dd for d in downstream for dd in adj_down.get(d["name"], [])
                         if dd != service}
        return {"project": proj["code"], "service": service,
                "upstream": upstream, "downstream": downstream,
                "indirect_upstream": sorted(indirect_up),
                "indirect_downstream": sorted(indirect_down),
                "hint": "upstream=谁依赖我（升级影响方）、downstream=我依赖谁；"
                        "间接层为两跳；发布前评估对上下游的影响"}

    # publish_order：Kahn 拓扑排序（被依赖的先发布）
    in_degree = {n: 0 for n in svc_names}
    adj = {n: [] for n in svc_names}
    for d in deps:
        src, tgt = d["source_service"], d["target_service"]
        if src in in_degree and tgt in in_degree:
            adj[tgt].append(src)
            in_degree[src] += 1
    queue = sorted(n for n in svc_names if in_degree[n] == 0)
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adj.get(node, [])):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    remaining = [n for n in svc_names if n not in order]
    has_cycle = bool(remaining)
    if remaining:
        order.extend(sorted(remaining))
    return {"project": proj["code"], "order": order, "has_cycle": has_cycle,
            "hint": "被依赖的服务先发布；has_cycle=true 时环上服务附在末尾"
                    "（建议用 manage_dependencies 拆解循环依赖）"}


# ---------- 工具实现（write：operator/admin，均落库留痕） ----------

def _analyze_alert(ctx, alert_id=None, **kw) -> dict:
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 alert_id（整数）")
    try:
        advice = advisor_mod.analyze_alert(
            ctx.db_path, alert_id,
            actor={"id": ctx.user_id, "username": ctx.username})
    except ValueError as e:
        raise ToolError(str(e))
    return {"advice": {"id": advice["id"], "title": advice["title"],
                       "body": advice["body"]},
            "hint": "建议已保存；可将建议沉淀为案例供后续复用"}


def _precheck(ctx, target=None, env=None, version="", **kw) -> dict:
    if not target or not env:
        raise ToolError("缺少 target（服务/流水线名）或 env")
    try:
        res = advisor_mod.run_precheck(
            ctx.db_path, target, env=env, version=version or "",
            canary=bool(kw.get("canary")),
            actor={"id": ctx.user_id, "username": ctx.username})
    except ValueError as e:
        raise ToolError(str(e))
    return {"advice_id": res["id"], "passed": res["passed"],
            "checks": res.get("checks", []), "body": res["body"]}


def _ack_alert(ctx, alert_id=None, note="", **kw) -> dict:
    db = ctx.db_path
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 alert_id（整数）")
    alert = dbm.get_record(db, "alerts", alert_id)
    if not alert:
        raise ToolError(f"告警不存在: #{alert_id}")
    if alert["status"] == "resolved":
        raise ToolError(f"告警 #{alert_id} 已解决，无需确认")
    fields = {"status": "acked", "ack_by": ctx.username, "ack_at": int(time.time())}
    dbm.update_record(db, "alerts", alert_id, fields)
    dbm.create_audit(db, ctx.user_id, ctx.username, "ack_alert", "alerts",
                     alert_id, {"subject": alert.get("subject"),
                                "via": "agent", "note": note or ""})
    return {"ok": True, "alert_id": alert_id, "acked_by": ctx.username,
            "hint": "告警已确认；建议用 analyze_alert 获取归因建议"}


def _plan_operation(ctx, op=None, target=None, env=None, version="",
                    previous_version="", canary=False, skip_build=False,
                    auto_version=False, no_git_tag=False, intent="", **kw) -> dict:
    """生成发布操作审批单（awaiting_approval），由人工在 Web 端审批后执行"""
    if op not in ("deploy", "rollback", "restart"):
        raise ToolError("op 仅支持 deploy/rollback/restart")
    if not target:
        raise ToolError("缺少 target（服务名或流水线名）")
    if not env:
        raise ToolError("缺少环境 env")
    try:
        run = executor_mod.plan_operation(
            ctx.db_path, {"id": ctx.user_id, "username": ctx.username},
            op=op, target=target, env=env, version=version or "",
            previous_version=previous_version or "", canary=bool(canary),
            auto_version=bool(auto_version), skip_build=bool(skip_build),
            no_git_tag=bool(no_git_tag), intent=intent or "")
    except executor_mod.ExecutorError as e:
        raise ToolError(str(e))
    plan = run.get("plan") or {}
    return {"run_id": run["id"], "status": run["status"],
            "plan": {"op": plan.get("op"), "target": plan.get("target"),
                     "env": plan.get("env"), "version": plan.get("version"),
                     "previous_version": plan.get("previous_version"),
                     "flags": plan.get("flags", {}),
                     "steps": plan.get("steps", []),
                     "cli": plan.get("cli", ""),
                     "window_warning": plan.get("window_warning", False),
                     "window_text": plan.get("window_text", "")},
            "hint": "审批单已生成，等待人工审批（Web 审批单页或对话中明确指示 decide_run）"}


def _decide_run(ctx, run_id=None, action=None, note="", force_window=None,
                **kw) -> dict:
    """审批发布审批单（与 Web 端审批同等效力，决策者身份与审计一致）

    安全语义：仅当用户明确表达「批准/驳回某审批单」时调用；
    run_id 必须先经 pending_approvals / run_detail 核对，不得凭记忆猜测。
    """
    if action not in ("approve", "reject"):
        raise ToolError("action 仅支持 approve/reject")
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 run_id（整数，可用 pending_approvals 查询待审清单）")
    try:
        run = executor_mod.decide_run(
            ctx.db_path, run_id,
            {"id": ctx.user_id, "username": ctx.username},
            action=action, note=(note or "")[:500],
            force_window=bool(force_window))
    except executor_mod.ExecutorError as e:
        raise ToolError(str(e))
    dbm.create_audit(ctx.db_path, ctx.user_id, ctx.username,
                     "agent_" + action, "agent_run", run_id,
                     {"name": run.get("name"), "env": run.get("env"),
                      "version": run.get("version"),
                      "note": (note or "")[:500],
                      "force_window": bool(force_window), "via": "agent"})
    hint = ("已驳回，审批单关闭" if action == "reject" else
            "已批准，任务已提交执行；终态由后台同步，可用 run_detail 查询")
    return {"run_id": run_id, "status": run["status"],
            "task_id": run.get("task_id") or 0,
            "decided_by": run.get("decided_by_name", ""),
            "hint": hint}


def _case_save(ctx, advice_id=None, **kw) -> dict:
    try:
        advice_id = int(advice_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 advice_id（整数）")
    try:
        case_id = advisor_mod.save_case_from_advice(
            ctx.db_path, advice_id,
            actor={"id": ctx.user_id, "username": ctx.username})
    except ValueError as e:
        raise ToolError(str(e))
    return {"case_id": case_id, "hint": "案例已创建（草稿），可继续补全 root_cause/action/lesson"}


def _case_update(ctx, case_id=None, root_cause=None, action=None,
                 result=None, lesson=None, title=None, tags=None,
                 service=None, env=None, version=None, symptom=None,
                 **kw) -> dict:
    try:
        case_id = int(case_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 case_id（整数）")
    fields = {}
    for k, v in (("root_cause", root_cause), ("action", action),
                 ("result", result), ("lesson", lesson), ("title", title),
                 ("service", service), ("env", env), ("version", version),
                 ("symptom", symptom)):
        if v is not None:
            fields[k] = v
    if tags is not None:
        fields["tags"] = tags if isinstance(tags, list) else [tags]
    if not fields:
        raise ToolError("至少提供一个要修改的字段")
    ok = memory_mod.update_case(ctx.db_path, case_id, fields)
    if not ok:
        raise ToolError(f"案例不存在: #{case_id}")
    return {"case_id": case_id, "updated": fields,
            "hint": "案例已更新，后续相似故障可被检索复用"}


# ---------- 工具实现（write：平台配置管理与闭环动作，均落审计） ----------

def _jsonish(v, what: str):
    """LLM 可能传对象或 JSON 字符串，统一还原为对象/列表"""
    if v is None or isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            raise ToolError(f"{what} 必须为对象/数组或其 JSON 字符串")
    raise ToolError(f"{what} 类型非法")


def _validate_cron(cron_expr: str) -> int:
    """校验 cron 表达式并返回下次执行时间戳（与路由/调度器同一校验口径）"""
    try:
        from croniter import croniter
        from datetime import datetime
        return int(croniter(cron_expr, datetime.now()).get_next())
    except ImportError:
        raise ToolError("服务端未安装 croniter，无法校验 cron 表达式")
    except (ValueError, KeyError) as e:
        raise ToolError(f"cron 表达式无效: {e}")


def _validate_window_scope(scope) -> str:
    """校验窗口 scope 结构（与 deploy_enhance_routes._validate_scope 对齐）"""
    if not isinstance(scope, dict):
        return "scope 格式错误"
    if scope.get("type") not in ("all", "project", "env", "server", "service"):
        return "scope type 非法"
    if not isinstance(scope.get("ids"), list):
        return "scope.ids 需为列表"
    if "env_ids" in scope and not isinstance(scope["env_ids"], list):
        return "scope.env_ids 需为列表"
    return ""


def _validate_canary_steps(steps) -> list:
    """steps 结构校验与归一化（与 compute_canary_batches 消费口径对齐）"""
    steps = _jsonish(steps, "steps")
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps 必须为非空数组")
    out = []
    for st in steps:
        if not isinstance(st, dict) or "batch" not in st or "servers" not in st:
            raise ToolError("steps 每项需含 batch 与 servers 字段")
        srv = st["servers"]
        if srv != "all" and not isinstance(srv, int):
            raise ToolError('steps.servers 需为整数或 "all"')
        out.append({"batch": int(st["batch"]), "servers": srv,
                    "observe_minutes": int(st.get("observe_minutes", 0)),
                    "auto_advance": bool(st.get("auto_advance", False))})
    return out


def _schedule_out(s: dict) -> dict:
    return {"id": s["id"], "name": s["name"], "type": s["type"],
            "project_id": s["project_id"],
            "ref_type": s.get("ref_type", "project"),
            "ref_id": s.get("ref_id", 0),
            "env": s.get("env", ""), "version": s.get("version", ""),
            "previous_version": s.get("previous_version", ""),
            "enabled": s.get("enabled", 1),
            "schedule_at": _fmt_ts(s.get("schedule_at")),
            "cron_expr": s.get("cron_expr", ""),
            "next_run_at": _fmt_ts(s.get("next_run_at")),
            "last_run_at": _fmt_ts(s.get("last_run_at"))}


def _manage_schedules(ctx, action=None, schedule_id=None, project=None,
                      name="", type="once", service="", env="", version="",
                      previous_version="", schedule_at=0, cron_expr="",
                      extra=None, **kw) -> dict:
    """定时计划管理：list/create/update/delete/toggle；run 生成审批单"""
    db = ctx.db_path
    action = action or "list"
    if action == "list":
        proj = _resolve_project(db, project or "")
        if proj:
            rows = dbm.list_records(db, "deploy_schedules", "project_id=?",
                                    (proj["id"],), "id DESC")
        else:
            rows = dbm.list_records(db, "deploy_schedules", "", (), "id DESC")
        return {"schedules": [_schedule_out(s) for s in rows[:50]]}
    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        if not name.strip():
            raise ToolError("缺少计划名称 name")
        if type not in ("once", "cron"):
            raise ToolError("type 必须为 once 或 cron")
        ref_type, ref_id = "project", proj["id"]
        if service:
            svc = _find_service(db, proj["code"], service)
            ref_type, ref_id = "service", svc["id"]
        if type == "cron":
            if not cron_expr:
                raise ToolError("cron 类型必须提供 cron_expr")
            next_run_at = _validate_cron(cron_expr)
        else:
            if not schedule_at:
                raise ToolError("once 类型必须提供 schedule_at（Unix 秒时间戳）")
            next_run_at = int(schedule_at)
        sid = dbm.create_record(db, "deploy_schedules", dict(
            project_id=proj["id"], type=type, name=name.strip(),
            ref_type=ref_type, ref_id=ref_id, env=env or "",
            version=version or "", previous_version=previous_version or "",
            extra=json.dumps(extra, ensure_ascii=False) if extra else "[]",
            schedule_at=int(schedule_at or 0), cron_expr=cron_expr or "",
            enabled=1, last_run_at=0, next_run_at=next_run_at,
            created_by=ctx.user_id, created_at=int(time.time())))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_schedule", "deploy_schedules", sid,
                         {"name": name, "type": type, "via": "agent"})
        return {"id": sid, "enabled": True,
                "next_run_at": _fmt_ts(next_run_at),
                "hint": "计划已创建并启用，到点由调度器自动执行"}
    sched = dbm.get_record(db, "deploy_schedules", int(schedule_id or 0)) \
        if schedule_id else None
    if not sched:
        raise ToolError("缺少或不存在 schedule_id")
    if action == "update":
        fields = {}
        if name:
            fields["name"] = name.strip()
        if env != "":
            fields["env"] = env
        if version != "":
            fields["version"] = version
        if previous_version != "":
            fields["previous_version"] = previous_version
        if cron_expr:
            fields["cron_expr"] = cron_expr
            # 同步重算 next_run_at，保证新表达式立即生效（与路由 update 对齐）
            fields["next_run_at"] = _validate_cron(cron_expr)
        if schedule_at:
            fields["schedule_at"] = int(schedule_at)
            fields["next_run_at"] = int(schedule_at)
        if extra is not None:
            fields["extra"] = json.dumps(extra, ensure_ascii=False)
        if service:
            svc = _find_service(db, "", service)
            fields["ref_type"], fields["ref_id"] = "service", svc["id"]
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        dbm.update_record(db, "deploy_schedules", sched["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_schedule", "deploy_schedules",
                         sched["id"], fields)
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        dbm.delete_record(db, "deploy_schedules", sched["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_schedule", "deploy_schedules",
                         sched["id"], {"name": sched["name"], "via": "agent"})
        return {"ok": True, "deleted": sched["name"]}
    if action == "toggle":
        new_enabled = 0 if sched["enabled"] else 1
        dbm.update_record(db, "deploy_schedules", sched["id"],
                          {"enabled": new_enabled})
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_toggle_schedule", "deploy_schedules",
                         sched["id"], {"enabled": new_enabled, "via": "agent"})
        return {"id": sched["id"], "enabled": bool(new_enabled)}
    if action == "run":
        try:
            run = executor_mod.plan_schedule_run(
                db, {"id": ctx.user_id, "username": ctx.username},
                sched["id"], intent="Agent 手动执行定时计划")
        except executor_mod.ExecutorError as e:
            raise ToolError(str(e))
        plan = run.get("plan") or {}
        return {"run_id": run["id"], "status": run["status"],
                "plan": {"op": plan.get("op"), "target": plan.get("target"),
                         "env": plan.get("env"),
                         "window_warning": plan.get("window_warning", False),
                         "window_text": plan.get("window_text", ""),
                         "steps": plan.get("steps", [])},
                "hint": "已生成审批单，人工审批通过后才会执行"}
    raise ToolError("action 仅支持 list/create/update/delete/toggle/run")


def _manage_windows(ctx, action=None, window_id=None, name="",
                    block_level="warn", start_at=0, end_at=0,
                    repeat_yearly=False, scope=None, project=None, **kw) -> dict:
    """发布窗口管理（仅 admin，与 Web 端权限一致）"""
    db = ctx.db_path
    if (ctx.role or "").lower() != "admin":
        raise ToolError("发布窗口管理仅 admin 可操作（与 Web 端权限一致）")
    action = action or "list"
    if action == "list":
        rows = dbm.list_records(db, "release_windows", "", (), "id DESC")
        return {"windows": [
            {"id": w["id"], "name": w["name"],
             "project_id": w.get("project_id", 0),
             "start": _fmt_ts(w.get("start_at")),
             "end": _fmt_ts(w.get("end_at")),
             "block_level": w.get("block_level"),
             "repeat_yearly": w.get("repeat_yearly", 0),
             "scope": w.get("scope") or {"type": "all", "ids": []}}
            for w in rows[:50]]}
    if action == "create":
        if not name.strip():
            raise ToolError("缺少窗口名称 name")
        if block_level not in ("block", "warn"):
            raise ToolError("block_level 必须为 block 或 warn")
        if not start_at or not end_at:
            raise ToolError("缺少 start_at / end_at（Unix 秒时间戳）")
        scope = _jsonish(scope, "scope") or {"type": "all", "ids": []}
        err = _validate_window_scope(scope)
        if err:
            raise ToolError(err)
        proj = _resolve_project(db, project or "")
        wid = dbm.create_record(db, "release_windows", dict(
            project_id=proj["id"] if proj else 0, name=name.strip(),
            start_at=int(start_at), end_at=int(end_at),
            block_level=block_level, scope=scope,
            repeat_yearly=1 if repeat_yearly else 0,
            created_by=ctx.user_id, created_at=int(time.time())))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_window", "release_windows", wid,
                         {"name": name, "block_level": block_level,
                          "via": "agent"})
        return {"id": wid, "hint": "窗口已生效，命中项目的发布计划将受限"}
    win = dbm.get_record(db, "release_windows", int(window_id or 0)) \
        if window_id else None
    if not win:
        raise ToolError("缺少或不存在 window_id")
    if action == "update":
        fields = {}
        if name:
            fields["name"] = name.strip()
        if block_level in ("block", "warn"):
            fields["block_level"] = block_level
        if start_at:
            fields["start_at"] = int(start_at)
        if end_at:
            fields["end_at"] = int(end_at)
        if kw.get("repeat_yearly") is not None:
            fields["repeat_yearly"] = 1 if kw["repeat_yearly"] else 0
        if scope is not None:
            scope = _jsonish(scope, "scope")
            err = _validate_window_scope(scope)
            if err:
                raise ToolError(err)
            fields["scope"] = scope
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        dbm.update_record(db, "release_windows", win["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_window", "release_windows",
                         win["id"], fields)
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        dbm.delete_record(db, "release_windows", win["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_window", "release_windows",
                         win["id"], {"name": win["name"], "via": "agent"})
        return {"ok": True, "deleted": win["name"]}
    raise ToolError("action 仅支持 list/create/update/delete")


def _manage_canary(ctx, action=None, config_id=None, project=None,
                   name="", steps=None, is_default=None, **kw) -> dict:
    """灰度策略管理：list/create/update/delete/set_default"""
    db = ctx.db_path
    action = action or "list"
    if action == "list":
        proj = _resolve_project(db, project or "")
        if proj:
            rows = dbm.list_records(db, "canary_configs", "project_id=?",
                                    (proj["id"],), "id")
        else:
            rows = dbm.list_records(db, "canary_configs", "", (), "id")
        return {"canary_configs": [
            {"id": c["id"], "name": c["name"],
             "project_id": c["project_id"],
             "is_default": c.get("is_default", 0),
             "steps": c.get("steps") or []} for c in rows]}
    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        if not name.strip():
            raise ToolError("缺少策略名称 name")
        norm_steps = _validate_canary_steps(steps)
        conn = dbm.get_conn(db)
        try:
            if is_default:
                conn.execute(
                    "UPDATE canary_configs SET is_default=0 "
                    "WHERE project_id=? AND is_default=1", (proj["id"],))
                conn.commit()
        finally:
            conn.close()
        cid = dbm.create_record(db, "canary_configs", dict(
            project_id=proj["id"], name=name.strip(),
            steps=json.dumps(norm_steps, ensure_ascii=False),
            is_default=1 if is_default else 0, created_at=int(time.time())))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_canary", "canary_configs", cid,
                         {"name": name, "is_default": bool(is_default),
                          "via": "agent"})
        return {"id": cid, "is_default": bool(is_default)}
    cfg = dbm.get_record(db, "canary_configs", int(config_id or 0)) \
        if config_id else None
    if not cfg:
        raise ToolError("缺少或不存在 config_id")
    if action in ("update", "set_default"):
        fields = {}
        if action == "set_default" or is_default:
            fields["is_default"] = 1 if (action == "set_default" or is_default) else 0
        if name:
            fields["name"] = name.strip()
        if steps is not None:
            fields["steps"] = json.dumps(_validate_canary_steps(steps),
                                         ensure_ascii=False)
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        if fields.get("is_default") == 1:
            # 设为默认时先清除同项目其他默认策略，保证唯一（与路由语义对齐）
            conn = dbm.get_conn(db)
            try:
                conn.execute(
                    "UPDATE canary_configs SET is_default=0 "
                    "WHERE project_id=? AND is_default=1 AND id<>?",
                    (cfg["project_id"], cfg["id"]))
                conn.commit()
            finally:
                conn.close()
        dbm.update_record(db, "canary_configs", cfg["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         f"agent_{action}_canary", "canary_configs",
                         cfg["id"], fields)
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        dbm.delete_record(db, "canary_configs", cfg["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_canary", "canary_configs", cfg["id"],
                         {"name": cfg["name"], "via": "agent"})
        return {"ok": True, "deleted": cfg["name"]}
    raise ToolError("action 仅支持 list/create/update/delete/set_default")


def _require_admin(ctx, what: str) -> None:
    """配置 CRUD 类工具与 Web 端权限对齐：仅 admin（复用 _manage_windows 惯例）"""
    if (ctx.role or "").lower() != "admin":
        raise ToolError(f"{what}仅 admin 可操作（与 Web 端权限一致）")


def _service_in_pipeline(db: str, project_id: int, service_id: int) -> bool:
    """服务是否被项目流水线 stages 引用（删除保护，与 Web 端语义一致）"""
    for pl in dbm.list_pipelines(db, project_id):
        for stage in pl.get("stages", []) or []:
            if not isinstance(stage, dict):
                continue
            for svc in stage.get("services", []) or []:
                if isinstance(svc, dict) and svc.get("service_id") == service_id:
                    return True
    return False


def _service_out(s: dict) -> dict:
    return {"id": s["id"], "name": s["name"],
            "project_id": s["project_id"],
            "image_name": s["image_name"],
            "service_name": s["service_name"],
            "build_type": s.get("build_type", "maven"),
            "git_repo": s.get("git_repo", ""),
            "git_branch": s.get("git_branch", ""),
            "git_pull_before_build": bool(s.get("git_pull_before_build")),
            "enabled": bool(s.get("enabled", 1)),
            "health_url": s.get("health_url", ""),
            "health_port": s.get("health_port", 0)}


def _validate_git_repo(repo: str) -> str:
    """git_repo 双形态校验：相对目录名，或 http(s) 远程 URL（末段须合法目录名）"""
    repo = (repo or "").strip()
    if repo.startswith(("http://", "https://")):
        name = repo.rstrip("/").rsplit("/", 1)[-1]
        if name.endswith(".git"):
            name = name[:-4]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ToolError("远程 URL 末段须为合法目录名（字母数字 _ -），"
                            "如 https://git.example.com/order-service.git")
    elif repo.startswith(("..", "/", "\\")) or ":" in repo:
        # 本地形态护栏：禁止越界/绝对路径/盘符（构建时会拼入 project_root）
        raise ToolError("git_repo 本地形态须为相对 deploy.project_root 的"
                        "目录名（不含 .. / 绝对路径 / 盘符）")
    return repo


def _manage_projects(ctx, action=None, project_id=None, name="",
                     code="", description="", git_org="", **kw) -> dict:
    """项目 CRUD（仅 admin，与 Web 端一致）：list/create/update/delete"""
    db = ctx.db_path
    _require_admin(ctx, "项目管理")
    action = action or "list"
    if action == "list":
        rows = dbm.list_records(db, "projects", "", (), "id DESC")
        return {"projects": [
            {"id": p["id"], "name": p["name"], "code": p["code"],
             "description": p.get("description", ""),
             "git_org": p.get("git_org", ""),
             "services": len(dbm.list_services(db, p["id"], enabled_only=True)),
             "environments": len(dbm.list_environments(db, p["id"]))}
            for p in rows[:50]]}
    if action == "create":
        name, code = (name or "").strip(), (code or "").strip()
        if not name or not code:
            raise ToolError("缺少 name / code")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", code):
            raise ToolError("项目编码仅允许字母数字 _ -（导出时用作目录名）")
        for p in dbm.list_records(db, "projects"):
            if p["code"] == code:
                raise ToolError(f"项目编码 {code} 已存在")
        pid = dbm.create_record(db, "projects", dict(
            name=name, code=code,
            description=(description or "").strip(),
            git_org=(git_org or "").strip(),
            created_at=int(time.time())))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_project", "projects", pid,
                         {"name": name, "code": code, "via": "agent"})
        return {"id": pid, "name": name, "code": code,
                "hint": "项目已创建；可继续 manage_services 注册服务，"
                        "再用 plan_operation 生成发布审批单"}
    proj = dbm.get_record(db, "projects", int(project_id or 0)) \
        if project_id else None
    if not proj:
        raise ToolError("缺少或不存在 project_id")
    if action == "update":
        fields = {}
        if name:
            fields["name"] = name.strip()
        if description:
            fields["description"] = description.strip()
        if git_org:
            fields["git_org"] = git_org.strip()
        if not fields:
            raise ToolError("未提供任何要修改的字段（name/description/git_org）")
        dbm.update_record(db, "projects", proj["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_project", "projects", proj["id"],
                         fields)
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        ok, msg = dbm.delete_project(db, proj["id"])
        if not ok:
            raise ToolError(msg)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_project", "projects", proj["id"],
                         {"name": proj["name"], "via": "agent"})
        return {"ok": True, "deleted": proj["name"]}
    raise ToolError("action 仅支持 list/create/update/delete")


def _manage_services(ctx, action=None, project=None, service_id=None,
                     name="", image_name="", service_name="",
                     docker_context="", dockerfile="", build_type="maven",
                     snailjob_group="", health_url="", health_port=0,
                     clear_log_dir="", git_repo="", git_branch="",
                     git_pull_before_build=None, git_tag_enabled=None,
                     enabled=None, nacos_groups=None, dubbo_services=None,
                     log_error_keywords=None, build_order=None, **kw) -> dict:
    """服务注册与管理（仅 admin，与 Web 端一致）：list/create/update/delete"""
    db = ctx.db_path
    _require_admin(ctx, "服务管理")
    action = action or "list"
    if action == "list":
        proj = _resolve_project(db, project or "")
        if proj:
            rows = dbm.list_services(db, proj["id"])
        else:
            rows = dbm.list_records(db, "services", order="id")
        return {"services": [_service_out(s) for s in rows[:50]]}
    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        name = (name or "").strip()
        if not name or not (image_name or "").strip() \
                or not (service_name or "").strip():
            raise ToolError("缺少 name / image_name / service_name")
        for s in dbm.list_services(db, proj["id"]):
            if s["name"] == name:
                raise ToolError(f"项目内服务名 {name} 已存在")
        if build_type not in ("maven", "frontend"):
            raise ToolError("build_type 必须为 maven 或 frontend")
        fields = dict(
            project_id=proj["id"], name=name,
            image_name=image_name.strip(),
            service_name=service_name.strip(),
            docker_context=(docker_context or "").strip(),
            dockerfile=(dockerfile or "").strip(),
            build_type=build_type,
            snailjob_group=(snailjob_group or "").strip(),
            health_url=(health_url or "").strip(),
            health_port=int(health_port or 0),
            clear_log_dir=(clear_log_dir or "").strip(),
            git_repo=_validate_git_repo(git_repo),
            git_branch=(git_branch or "").strip(),
            git_pull_before_build=1 if git_pull_before_build else 0,
            git_tag_enabled=1 if git_tag_enabled is None or git_tag_enabled else 0,
            enabled=1 if enabled is None or enabled else 0,
            build_order=json.dumps(_jsonish(build_order, "build_order") or [],
                                   ensure_ascii=False),
            nacos_groups=json.dumps(
                _jsonish(nacos_groups, "nacos_groups") or [],
                ensure_ascii=False),
            dubbo_services=json.dumps(
                _jsonish(dubbo_services, "dubbo_services") or [],
                ensure_ascii=False),
            log_error_keywords=json.dumps(
                _jsonish(log_error_keywords, "log_error_keywords") or [],
                ensure_ascii=False))
        sid = dbm.create_record(db, "services", fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_service", "services", sid,
                         {"name": name, "pid": proj["id"], "via": "agent"})
        return {"id": sid, "project": proj["code"], "name": name,
                "hint": "服务已注册；git_repo 为远程 URL 时首次构建自动 clone，"
                        "用 plan_operation 生成发布审批单即可部署"}
    svc = dbm.get_record(db, "services", int(service_id or 0)) \
        if service_id else None
    if not svc:
        raise ToolError("缺少或不存在 service_id")
    if action == "update":
        fields = {}
        if name:
            n = name.strip()
            if n != svc["name"]:
                for s in dbm.list_services(db, svc["project_id"]):
                    if s["name"] == n and s["id"] != svc["id"]:
                        raise ToolError(f"项目内服务名 {n} 已存在")
            fields["name"] = n
        for k, v in (("image_name", image_name),
                     ("service_name", service_name),
                     ("docker_context", docker_context),
                     ("dockerfile", dockerfile),
                     ("snailjob_group", snailjob_group),
                     ("health_url", health_url),
                     ("clear_log_dir", clear_log_dir)):
            if v:
                fields[k] = v.strip()
        if build_type:
            if build_type not in ("maven", "frontend"):
                raise ToolError("build_type 必须为 maven 或 frontend")
            fields["build_type"] = build_type
        if git_repo:
            fields["git_repo"] = _validate_git_repo(git_repo)
        if git_branch:
            fields["git_branch"] = git_branch.strip()
        if health_port:
            fields["health_port"] = int(health_port)
        if git_pull_before_build is not None:
            fields["git_pull_before_build"] = 1 if git_pull_before_build else 0
        if git_tag_enabled is not None:
            fields["git_tag_enabled"] = 1 if git_tag_enabled else 0
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        for k, v in (("build_order", build_order),
                     ("nacos_groups", nacos_groups),
                     ("dubbo_services", dubbo_services),
                     ("log_error_keywords", log_error_keywords)):
            if v is not None:
                arr = _jsonish(v, k)
                if not isinstance(arr, list):
                    raise ToolError(f"{k} 必须为数组")
                fields[k] = json.dumps(arr, ensure_ascii=False)
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        dbm.update_record(db, "services", svc["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_service", "services", svc["id"],
                         {"name": svc["name"], "updated": list(fields)})
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        if _service_in_pipeline(db, svc["project_id"], svc["id"]):
            raise ToolError("服务被流水线 stages 引用，请先调整流水线")
        dbm.delete_record(db, "services", svc["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_service", "services", svc["id"],
                         {"name": svc["name"], "via": "agent"})
        return {"ok": True, "deleted": svc["name"]}
    raise ToolError("action 仅支持 list/create/update/delete")


def _mask_ssh(secret: str) -> str:
    """SSH 口令脱敏：非空遮蔽为 ***（与 Web 端 _mask_ssh_password 一致，凭据不回显）"""
    return "***" if secret else ""


def _env_out(e: dict, server_count: int = 0) -> dict:
    ssh = dict(e.get("ssh") or {})
    if ssh.get("password"):
        ssh["password"] = "***"
    return {"id": e["id"], "name": e["name"],
            "project_id": e["project_id"],
            "is_default": bool(e.get("is_default")),
            "monitor_enabled": bool(e.get("monitor_enabled", 1)),
            "servers": server_count,
            "registry": e.get("registry") or {},
            "nacos": e.get("nacos") or {},
            "ssh": ssh,
            "build": e.get("build") or {},
            "deploy": e.get("deploy") or {},
            "docker_compose_path": e.get("docker_compose_path", ""),
            "maven_profile": e.get("maven_profile", "")}


def _resolve_env(db: str, project: str, env: str) -> dict:
    """项目 + 环境名 → 环境记录（发布链同款查法）"""
    proj = _resolve_project(db, project)
    if not proj:
        raise ToolError("缺少项目 project（id/code/name）")
    env_obj = dbm.get_env_by_name(db, proj["id"], env or "")
    if not env_obj:
        raise ToolError(f"项目「{proj['code']}」下不存在环境「{env}」")
    return env_obj


_ENV_JSON_KEYS = ("registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy")


def _manage_environments(ctx, action=None, project=None,
                          environment_id=None, name="", is_default=None,
                          monitor_enabled=None, registry=None, nacos=None,
                          mysql_snailjob=None, ssh=None, build=None,
                          deploy=None, docker_compose_path="",
                          maven_profile="", source_project="",
                          source_env="", **kw) -> dict:
    """环境管理（仅 admin，与 Web 端一致）：list/create/clone/update/delete

    clone 为接入链捷径：从既有项目复制环境（六配置块 + 独立列 + 服务器绑定）
    到目标项目，基础设施配置（registry/nacos/ssh 等）多项目间往往共享。
    """
    db = ctx.db_path
    _require_admin(ctx, "环境管理")
    action = action or "list"
    if action == "list":
        proj = _resolve_project(db, project or "")
        if proj:
            rows = dbm.list_environments(db, proj["id"])
        else:
            rows = dbm.list_records(db, "environments", order="id")
        return {"environments": [
            _env_out(e, len(dbm.list_env_servers(db, e["id"])))
            for e in rows[:50]]}
    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        name = (name or "").strip()
        if not name:
            raise ToolError("缺少环境名 name（如 prod/test，仅字母数字 _ -）")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ToolError("环境名仅允许字母数字 _ -（导出时用作文件名）")
        for e in dbm.list_environments(db, proj["id"]):
            if e["name"] == name:
                raise ToolError(f"项目内环境名 {name} 已存在")
        blocks = {}
        for k, v in (("registry", registry), ("nacos", nacos),
                     ("mysql_snailjob", mysql_snailjob), ("ssh", ssh),
                     ("build", build), ("deploy", deploy)):
            if v is not None:
                blocks[k] = _jsonish(v, k)
                if not isinstance(blocks[k], dict):
                    raise ToolError(f"{k} 必须为对象")
        eid = dbm.create_record(db, "environments", dict(
            project_id=proj["id"], name=name,
            is_default=1 if is_default else 0,
            **{k: json.dumps(v, ensure_ascii=False)
               for k, v in blocks.items()},
            docker_compose_path=(docker_compose_path or "").strip(),
            maven_profile=(maven_profile or "").strip(),
            monitor_enabled=1 if monitor_enabled is None or monitor_enabled else 0))
        if is_default:
            _clear_other_default_envs(db, proj["id"], eid)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_environment", "environments", eid,
                         {"name": name, "pid": proj["id"], "via": "agent"})
        return {"id": eid, "project": proj["code"], "name": name,
                "hint": "环境已创建；可用 manage_servers 绑定服务器，"
                        "或 clone 复制既有项目的环境（含服务器绑定）"}
    if action == "clone":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少目标项目 project（id/code/name）")
        name = (name or "").strip()
        if not name:
            raise ToolError("缺少新环境名 name")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ToolError("环境名仅允许字母数字 _ -（导出时用作文件名）")
        for e in dbm.list_environments(db, proj["id"]):
            if e["name"] == name:
                raise ToolError(f"项目内环境名 {name} 已存在")
        src = _resolve_env(db, source_project or project, source_env)
        fields = {k: json.dumps(src.get(k) or {}, ensure_ascii=False)
                  for k in _ENV_JSON_KEYS}
        fields.update(
            docker_compose_path=src.get("docker_compose_path", ""),
            maven_profile=src.get("maven_profile", ""),
            monitor_enabled=src.get("monitor_enabled", 1))
        eid = dbm.create_record(db, "environments", dict(
            project_id=proj["id"], name=name,
            is_default=1 if is_default else 0, **fields))
        if is_default:
            _clear_other_default_envs(db, proj["id"], eid)
        # 复制源环境的服务器绑定（同一批机器多项目共享是常态；order 保持）
        bound = 0
        for s in dbm.list_env_servers(db, src["id"]):
            dbm.bind_server(db, eid, s["id"], s.get("order_no", 0))
            bound += 1
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_clone_environment", "environments", eid,
                         {"name": name, "from": f"{source_project or project}"
                                               f"/{source_env}",
                          "servers": bound, "via": "agent"})
        return {"id": eid, "project": proj["code"], "name": name,
                "servers_bound": bound,
                "hint": "环境已克隆（含服务器绑定）；服务注册后即可"
                        "plan_operation 生成发布审批单"}
    env_obj = dbm.get_record(db, "environments", int(environment_id or 0)) \
        if environment_id else None
    if not env_obj:
        raise ToolError("缺少或不存在 environment_id")
    if action == "update":
        fields = {}
        if name:
            n = name.strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", n):
                raise ToolError("环境名仅允许字母数字 _ -（导出时用作文件名）")
            for e in dbm.list_environments(db, env_obj["project_id"]):
                if e["name"] == n and e["id"] != env_obj["id"]:
                    raise ToolError(f"项目内环境名 {n} 已存在")
            fields["name"] = n
        for k, v in (("registry", registry), ("nacos", nacos),
                     ("mysql_snailjob", mysql_snailjob), ("ssh", ssh),
                     ("build", build), ("deploy", deploy)):
            if v is not None:
                obj = _jsonish(v, k)
                if not isinstance(obj, dict):
                    raise ToolError(f"{k} 必须为对象")
                fields[k] = json.dumps(obj, ensure_ascii=False)
        if docker_compose_path:
            fields["docker_compose_path"] = docker_compose_path.strip()
        if maven_profile:
            fields["maven_profile"] = maven_profile.strip()
        if is_default is not None:
            fields["is_default"] = 1 if is_default else 0
        if monitor_enabled is not None:
            fields["monitor_enabled"] = 1 if monitor_enabled else 0
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        if fields.get("is_default") == 1:
            _clear_other_default_envs(db, env_obj["project_id"], env_obj["id"])
        dbm.update_record(db, "environments", env_obj["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_environment", "environments",
                         env_obj["id"], {"name": env_obj["name"],
                                          "updated": list(fields)})
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        ok, msg = dbm.delete_environment(db, env_obj["id"])
        if not ok:
            raise ToolError(msg)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_environment", "environments",
                         env_obj["id"],
                         {"name": env_obj["name"], "via": "agent"})
        return {"ok": True, "deleted": env_obj["name"]}
    raise ToolError("action 仅支持 list/create/clone/update/delete")


def _clear_other_default_envs(db: str, project_id: int, keep_id: int) -> None:
    """设默认环境时取消同项目其它默认（与 Web 端 create/update_environment 对齐）"""
    for e in dbm.list_environments(db, project_id):
        if e["is_default"] and e["id"] != keep_id:
            dbm.update_record(db, "environments", e["id"], {"is_default": 0})


def _server_out(s: dict) -> dict:
    return {"id": s["id"], "host": s["host"],
            "ssh_port": s.get("ssh_port"),
            "ssh_user": s.get("ssh_user", "") or "",
            "ssh_password": _mask_ssh(s.get("ssh_password", "") or ""),
            "nacos_ip": s.get("nacos_ip", ""),
            "remark": s.get("remark", ""),
            "enabled": bool(s.get("enabled", 1)),
            "order_no": s.get("order_no")}


def _manage_servers(ctx, action=None, host="", server_id=None,
                     project=None, environment="", ssh_port=None,
                     ssh_user=None, ssh_password=None, ssh_key_file=None,
                     nacos_ip="", remark="", enabled=None, **kw) -> dict:
    """服务器管理（仅 admin，与 Web 端一致）：list/create/bind/unbind/update

    bind 为 upsert 语义：服务器未注册时按传入参数先注册再绑定，已注册直接绑定
    （幂等）；发布目标即环境绑定的服务器清单。
    """
    db = ctx.db_path
    _require_admin(ctx, "服务器管理")
    action = action or "list"
    if action == "list":
        if environment:
            env_obj = _resolve_env(db, project or "", environment)
            rows = dbm.list_env_servers(db, env_obj["id"])
            return {"servers": [_server_out(s) for s in rows],
                    "environment": env_obj["name"],
                    "project_id": env_obj["project_id"]}
        rows = dbm.list_records(db, "servers", order="id")
        return {"servers": [_server_out(s) for s in rows[:100]]}
    if action == "create":
        host = (host or "").strip()
        if not host:
            raise ToolError("缺少服务器地址 host")
        for s in dbm.list_records(db, "servers"):
            if s["host"] == host:
                raise ToolError(f"服务器 {host} 已存在（id={s['id']}）")
        sid = dbm.create_record(db, "servers", dict(
            host=host,
            # ssh 三态：不传 = NULL（继承环境级），传了（含空串）才是覆盖（与 Web 一致）
            ssh_port=int(ssh_port) if ssh_port is not None else None,
            ssh_user=ssh_user, ssh_password=ssh_password,
            ssh_key_file=ssh_key_file,
            nacos_ip=(nacos_ip or "").strip(),
            remark=(remark or "").strip(),
            enabled=1 if enabled is None or enabled else 0))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_server", "servers", sid,
                         {"host": host, "via": "agent"})
        # 与 Web 差异：不自动绑定到所有启用监控的环境（对话场景意图是
        # 绑定到指定环境，用 bind 显式绑定）
        return {"id": sid, "host": host,
                "hint": "服务器已注册（未绑定环境）；"
                        "用 bind 动作绑定到指定环境"}
    if action == "bind":
        env_obj = _resolve_env(db, project or "", environment)
        host = (host or "").strip()
        if not host:
            raise ToolError("缺少服务器地址 host")
        rows = dbm.list_records(db, "servers", "host=?", (host,))
        if rows:
            sid = rows[0]["id"]
            created = False
        else:
            # upsert：未注册则按传入参数注册（ssh 三态同 create）
            sid = dbm.create_record(db, "servers", dict(
                host=host,
                ssh_port=int(ssh_port) if ssh_port is not None else None,
                ssh_user=ssh_user, ssh_password=ssh_password,
                ssh_key_file=ssh_key_file,
                nacos_ip=(nacos_ip or "").strip(),
                remark=(remark or "").strip(),
                enabled=1 if enabled is None or enabled else 0))
            created = True
            dbm.create_audit(db, ctx.user_id, ctx.username,
                             "agent_create_server", "servers", sid,
                             {"host": host, "via": "agent-bind"})
        for s in dbm.list_env_servers(db, env_obj["id"]):
            if s["id"] == sid:
                return {"ok": True, "already_bound": True,
                        "server_id": sid, "host": host,
                        "environment": env_obj["name"]}
        order = len(dbm.list_env_servers(db, env_obj["id"]))
        dbm.bind_server(db, env_obj["id"], sid, order)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_bind_server", "environments",
                         env_obj["id"],
                         {"server_id": sid, "host": host,
                          "created": created, "via": "agent"})
        return {"ok": True, "server_id": sid, "host": host,
                "environment": env_obj["name"], "created": created,
                "hint": "已绑定到环境（发布目标即环境绑定清单，按 order 顺序）"}
    if action == "unbind":
        env_obj = _resolve_env(db, project or "", environment)
        if host:
            rows = dbm.list_records(db, "servers", "host=?", ((host or "").strip(),))
            if not rows:
                raise ToolError(f"服务器 {host} 未注册")
            sid = rows[0]["id"]
        else:
            try:
                sid = int(server_id)
            except (TypeError, ValueError):
                raise ToolError("缺少 host 或 server_id")
        dbm.unbind_server(db, env_obj["id"], sid)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_unbind_server", "environments",
                         env_obj["id"], {"server_id": sid, "via": "agent"})
        return {"ok": True, "server_id": sid,
                "environment": env_obj["name"]}
    if action == "test":
        # SSH 连通测试（与 Web /servers/test 同等效力）：仅测已存配置，
        # 服务器级凭据优先、缺省回退首个绑定环境 ssh 段、再回退 root:22
        if server_id:
            rec = dbm.get_record(db, "servers", int(server_id))
        elif host:
            rows = dbm.list_records(db, "servers", "host=?",
                                    (host.strip(),))
            rec = rows[0] if rows else None
        else:
            raise ToolError("action=test 需要 host 或 server_id")
        if not rec:
            raise ToolError("服务器不存在（可先 list 查询）")
        from webapp.monitor import _ssh_exec
        ssh = {}
        for k_src, k_dst in (("ssh_user", "user"),
                             ("ssh_password", "password"),
                             ("ssh_key_file", "key_file")):
            if rec.get(k_src):
                ssh[k_dst] = rec[k_src]
        port = rec.get("ssh_port") or 0
        if not ssh.get("user"):
            envs = dbm.list_server_envs(db, rec["id"])
            if envs:
                env_ssh = envs[0].get("ssh") or {}
                for k in ("user", "password", "key_file", "port"):
                    if k not in ssh and env_ssh.get(k):
                        ssh[k] = env_ssh[k]
        port = port or ssh.get("port") or 22
        user = ssh.get("user") or "root"
        h = rec["host"]
        t0 = time.time()
        try:
            _ssh_exec(h, int(port), ssh, "echo ok", timeout=8)
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            return {"ok": False,
                    "message": f"连接失败（{ms}ms）：{str(e)[:200]}"}
        ms = int((time.time() - t0) * 1000)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_test_server", "servers", rec["id"],
                         {"host": h, "via": "agent"})
        return {"ok": True,
                "message": f"连接成功（{user}@{h}:{port}，耗时 {ms}ms）"}
    # update：按 server_id 改覆盖字段
    rec = dbm.get_record(db, "servers", int(server_id or 0)) \
        if server_id else None
    if not rec:
        raise ToolError("缺少或不存在 server_id")
    if action == "update":
        fields = {}
        if host:
            h = host.strip()
            for s in dbm.list_records(db, "servers"):
                if s["host"] == h and s["id"] != rec["id"]:
                    raise ToolError(f"服务器 {h} 已存在")
            fields["host"] = h
        if nacos_ip:
            fields["nacos_ip"] = nacos_ip.strip()
        if remark:
            fields["remark"] = remark.strip()
        if enabled is not None:
            fields["enabled"] = 1 if enabled else 0
        for k, v in (("ssh_port", ssh_port), ("ssh_user", ssh_user),
                     ("ssh_password", ssh_password),
                     ("ssh_key_file", ssh_key_file)):
            if v is not None:
                fields[k] = int(v) if k == "ssh_port" else v
        if not fields:
            raise ToolError("未提供任何要修改的字段")
        dbm.update_record(db, "servers", rec["id"], fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_server", "servers", rec["id"],
                         {"host": rec["host"], "updated": list(fields)})
        # 口令不回显：审计与返回均不含敏感值
        return {"ok": True, "updated": [k for k in fields if k != "ssh_password"]}
    raise ToolError("action 仅支持 list/create/bind/unbind/test/update")


def _pipeline_stages_in(db: str, project_id: int, stages):
    """stages 双形态解析 → 原生 Web 形态 [{services: [{service_id}]}]

    - 字符串数组 ["a", "b"] = 单 stage 内多服务并行
    - 嵌套数组 [["a", "b"], ["c"]] = 多 stage 顺序发布
    - 对象数组 [{"services": [{"service_id": 1}]}] = 原生形态（对齐 Web）
    服务名在项目内唯一（UNIQUE(project_id, name)），名字解析无歧义；
    与 Web 端 _check_pipeline_stages 同校验：悬空引用创建侧拒绝。
    """
    if isinstance(stages, str):
        try:
            stages = json.loads(stages)
        except (TypeError, ValueError):
            raise ToolError("stages 必须为数组（服务名数组或 stage 数组）")
    if not isinstance(stages, list) or not stages:
        raise ToolError("stages 必须为非空数组（如 [\"order-svc\"] 或 [[\"a\"], [\"b\"]])")
    if all(isinstance(s, str) for s in stages):
        stages = [stages]  # 扁平服务名数组 → 单 stage 多服务
    svc_by_name = {s["name"]: s for s in dbm.list_services(db, project_id)}
    svc_by_id = {s["id"]: s for s in svc_by_name.values()}
    out = []
    for stage in stages:
        if isinstance(stage, str):
            stage = [stage]  # 混形态容错：单元素字符串
        if isinstance(stage, list):
            items = stage
        elif isinstance(stage, dict):
            items = stage.get("services", [])
            if not isinstance(items, list):
                raise ToolError("stage.services 必须为数组")
        else:
            raise ToolError("stage 必须为服务名数组或对象")
        ids = []
        for it in items:
            if isinstance(it, str):
                rec = svc_by_name.get(it)
                if not rec:
                    raise ToolError(f"stages 引用了不存在的服务: {it}")
                ids.append(rec["id"])
            elif isinstance(it, dict) and isinstance(it.get("service_id"), int) \
                    and not isinstance(it.get("service_id"), bool):
                rec = svc_by_id.get(it["service_id"])
                if not rec:
                    raise ToolError(
                        f"stages 引用了不存在的服务 (service_id={it['service_id']})")
                ids.append(rec["id"])
            else:
                raise ToolError("stage.services 元素必须为服务名或 {service_id}")
        if not ids:
            raise ToolError("每个 stage 至少包含一个服务")
        out.append({"services": [{"service_id": i} for i in ids]})
    return out


def _pipeline_out(db: str, pl: dict, svc_map: dict) -> dict:
    """流水线输出：stages 展开服务名（service_id → name，便于阅读）"""
    stages = pl.get("stages") or []
    if isinstance(stages, str):
        try:
            stages = json.loads(stages)
        except (TypeError, ValueError):
            stages = []
    out_stages = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        names = []
        for svc in stage.get("services", []) or []:
            if isinstance(svc, dict):
                sid = svc.get("service_id")
                rec = svc_map.get(sid)
                names.append(rec["name"] if rec else f"#{sid}")
        if names:
            out_stages.append(names)
    return {"id": pl["id"], "name": pl["name"],
            "build_first": bool(pl.get("build_first")),
            "stages": out_stages,
            "service_count": sum(len(s) for s in out_stages)}


def _manage_pipelines(ctx, action=None, project=None, pipeline_id=None,
                       name="", stages=None, build_first=None, **kw) -> dict:
    """流水线管理（仅 admin，与 Web 端一致）：list/create/update/delete

    流水线是多服务分阶段发布的编排：plan_operation 可直接以流水线名为
    目标（kind 自动识别）；调度与单服务发布不依赖流水线。
    """
    db = ctx.db_path
    _require_admin(ctx, "流水线管理")
    action = action or "list"
    if action == "list":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        svc_map = {s["id"]: s for s in dbm.list_services(db, proj["id"])}
        rows = dbm.list_pipelines(db, proj["id"])
        return {"project": proj["code"],
                "pipelines": [_pipeline_out(db, pl, svc_map) for pl in rows]}
    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError("缺少项目 project（id/code/name）")
        name = (name or "").strip()
        if not name:
            raise ToolError("缺少流水线名 name")
        if stages is None:
            raise ToolError("缺少 stages（服务名数组或 stage 数组）")
        for pl in dbm.list_pipelines(db, proj["id"]):
            if pl["name"] == name:
                raise ToolError(f"项目内流水线名已存在: {name}")
        norm_stages = _pipeline_stages_in(db, proj["id"], stages)
        plid = dbm.create_record(db, "pipelines", dict(
            project_id=proj["id"], name=name,
            build_first=1 if build_first is None or build_first else 0,
            stages=norm_stages))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_pipeline", "pipelines", plid,
                         {"name": name, "via": "agent"})
        svc_n = sum(len(s.get("services", [])) for s in norm_stages)
        return {"id": plid, "name": name,
                "stage_count": len(norm_stages), "service_count": svc_n,
                "build_first": build_first is None or bool(build_first),
                "hint": "流水线已创建；发布走 plan_operation target=流水线名"
                        "（stages 逐阶段发布，build_first 先统一构建）"}
    # update/delete：按 pipeline_id 定位
    try:
        plid = int(pipeline_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 pipeline_id（整数）")
    rec = dbm.get_record(db, "pipelines", plid)
    if not rec:
        raise ToolError(f"流水线不存在: #{plid}")
    if action == "update":
        fields = {}
        if name:
            n = name.strip()
            if not n:
                raise ToolError("name 不能为空")
            for pl in dbm.list_pipelines(db, rec["project_id"]):
                if pl["name"] == n and pl["id"] != rec["id"]:
                    raise ToolError(f"项目内流水线名已存在: {n}")
            fields["name"] = n
        if stages is not None:
            fields["stages"] = _pipeline_stages_in(db, rec["project_id"], stages)
        if build_first is not None:
            fields["build_first"] = 1 if build_first else 0
        if not fields:
            raise ToolError("未提供任何要修改的字段（name/stages/build_first）")
        dbm.update_record(db, "pipelines", plid, fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_pipeline", "pipelines", plid,
                         {"name": rec["name"], "updated": list(fields)})
        return {"ok": True, "updated": list(fields)}
    if action == "delete":
        # 对齐 Web：无额外引用保护（调度仅引用 project/service，不引用流水线）
        dbm.delete_record(db, "pipelines", plid)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_pipeline", "pipelines", plid,
                         {"name": rec["name"], "via": "agent"})
        return {"ok": True, "deleted": rec["name"]}
    raise ToolError("action 仅支持 list/create/update/delete")


def _canary_decision(ctx, task_id=None, action=None, **kw) -> dict:
    """灰度运行中决策（与 Web 端 POST /deploy/canary-decisions 同语义）"""
    if action not in ("continue", "stop", "rollback"):
        raise ToolError("action 必须为 continue/stop/rollback")
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 task_id（整数）")
    from deploy import read_canary_decision, canary_decision_path
    cur = read_canary_decision(task_id)
    if cur.get("state") != "waiting":
        raise ToolError(f"任务 #{task_id} 当前无等待中的灰度决策"
                        f"（state={cur.get('state')}）")
    path = canary_decision_path(task_id)
    cur["state"] = action
    cur["updated_at"] = int(time.time())
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    except OSError:
        raise ToolError("决策文件写入失败（任务可能已结束）")
    dbm.create_audit(ctx.db_path, ctx.user_id, ctx.username,
                     "agent_canary_decision", "tasks", task_id,
                     {"action": action, "via": "agent"})
    return {"task_id": task_id, "action": action,
            "hint": "决策已写入，发布子进程将在下轮轮询时读取"}


def _send_digest(ctx, **kw) -> dict:
    """立即发送一次巡检日报（与 Web 端 POST /agent/digest/send 同语义）"""
    from . import digest as digest_mod
    try:
        res = digest_mod.build_and_send(ctx.db_path)
    except ValueError as e:
        raise ToolError(str(e))
    return {"summary": res["summary"],
            "results": [list(r) for r in res["results"]],
            "text": res["text"],
            "hint": "日报已推送；周期自动推送需管理员在 Web 端开启日报配置"}


def _set_alert_status(ctx, alert_id=None, action=None, note="", **kw) -> dict:
    """告警闭环：resolve 解决 / reopen 重开（确认请用 ack_alert；与路由语义一致）"""
    db = ctx.db_path
    if action not in ("resolve", "reopen"):
        raise ToolError("action 仅支持 resolve/reopen（确认请用 ack_alert）")
    try:
        alert_id = int(alert_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 alert_id（整数）")
    alert = dbm.get_record(db, "alerts", alert_id)
    if not alert:
        raise ToolError(f"告警不存在: #{alert_id}")
    now = int(time.time())
    if action == "resolve":
        if alert["status"] == "resolved":
            raise ToolError(f"告警 #{alert_id} 已是解决状态")
        dbm.update_record(db, "alerts", alert_id, dict(
            status="resolved", resolved_by=ctx.username,
            resolved_at=now, resolved_type="manual"))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_resolve_alert", "alerts", alert_id,
                         {"subject": alert.get("subject"), "via": "agent",
                          "note": note or ""})
        return {"ok": True, "alert_id": alert_id, "status": "resolved",
                "hint": "已标记解决；建议沉淀归因为案例供后续复用"}
    if alert["status"] != "resolved":
        raise ToolError("仅已解决告警可重新打开")
    dbm.update_record(db, "alerts", alert_id, dict(
        status="firing", resolved_by="", resolved_at=None,
        resolved_type="", first_seen_at=now, last_seen_at=now,
        notify_count=0, escalation_level=0))
    dbm.create_audit(db, ctx.user_id, ctx.username,
                     "agent_reopen_alert", "alerts", alert_id,
                     {"subject": alert.get("subject"), "via": "agent",
                      "note": note or ""})
    return {"ok": True, "alert_id": alert_id, "status": "firing"}


# ---------- 告警规则与静默窗口管理 ----------

_RULE_METRICS = {"metric": ("cpu", "mem", "disk", "load"),
                 "status": ("service_down", "task_failed")}
_SEVERITIES = ("P0", "P1", "P2", "P3")
_ALERT_CHANNELS = ("email", "wecom", "webhook", "sms_aliyun", "feishu",
                   "dingtalk")
_SCOPE_TABLES = {"project": "projects", "env": "environments",
                 "server": "servers", "service": "services"}
_SCOPE_TIPS = {"project": "list_projects", "env": "list_environments",
               "server": "manage_servers(action=list)",
               "service": "list_services"}
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _check_scope_ids(db, scope) -> str:
    """scope.ids 悬空校验（对话防幻觉：id 必须真实存在，错误引导查询）"""
    stype = scope.get("type")
    if stype not in _SCOPE_TABLES:
        return ""
    ids = [x for x in scope.get("ids") or []
           if isinstance(x, int) and not isinstance(x, bool)]
    valid = {r["id"] for r in dbm.list_records(db, _SCOPE_TABLES[stype])}
    missing = [x for x in ids if x not in valid]
    if missing:
        return (f"scope.ids 引用了不存在的 {stype} id: {missing}"
                f"（可用 {_SCOPE_TIPS[stype]} 查询真实 id）")
    return ""


def _validate_alert_rule(db, body, partial=False) -> str:
    """规则校验（镜像 alert_routes._validate_rule，含跨字段约束）；空串=通过

    partial=True 仅校验已出现的字段（局部更新前的形态检查）。
    """
    def has(k):
        return True if not partial else k in body

    if has("name") and not (body.get("name") or "").strip():
        return "规则名称必填"
    if has("metric_type") and body.get("metric_type") not in (
            "metric", "status", "log_keyword", "custom_monitor"):
        return "metric_type 非法"
    if has("metric"):
        mt = body.get("metric_type")
        if mt == "metric" and body.get("metric") not in _RULE_METRICS["metric"]:
            return "metric 型规则的 metric 必须为 cpu/mem/disk/load"
        if mt == "status" and body.get("metric") not in _RULE_METRICS["status"]:
            return "status 型规则的 metric 必须为 service_down/task_failed"
        if mt == "log_keyword":
            kws = [k.strip() for k in (body.get("metric") or "").split(",")
                   if k.strip()]
            if not kws:
                return "日志关键词不能为空"
            if any(len(k) > 100 for k in kws):
                return "单个关键词长度不能超过 100"
    if has("exclude_keyword"):
        for k in (body.get("exclude_keyword") or "").split(","):
            if k.strip() and len(k.strip()) > 100:
                return "单个排除关键词长度不能超过 100"
    if has("severity") and body.get("severity") not in _SEVERITIES:
        return "severity 必须为 P0/P1/P2/P3"
    if has("compare") and body.get("compare") not in ("<", "<=", ">", ">="):
        return "compare 必须为 < <= > >="
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
            return "duration_seconds 不能超过 1200（滑动窗口上限 20 分钟）"
    if has("escalation_interval"):
        try:
            int(body.get("escalation_interval", 30))
        except (TypeError, ValueError):
            return "escalation_interval 必须为数字"
    if has("scope"):
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            return "scope 格式错误"
        if scope.get("type") not in ("all", "project", "env", "server", "service"):
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
        err = _check_scope_ids(db, scope)
        if err:
            return err
    if has("channels"):
        channels = body.get("channels") or []
        if not isinstance(channels, list):
            return "channels 需为列表"
        if channels and not set(channels) <= set(_ALERT_CHANNELS):
            return "channels 包含非法渠道"
    return ""


def _rule_out(r) -> dict:
    return {"id": r["id"], "name": r["name"],
            "metric_type": r.get("metric_type"), "metric": r.get("metric", ""),
            "compare": r.get("compare", ">"), "threshold": r.get("threshold", 0),
            "duration_seconds": r.get("duration_seconds", 0),
            "scope": r.get("scope") or {"type": "all", "ids": []},
            "severity": r.get("severity", "P2"),
            "channels": r.get("channels") or [],
            "escalation_interval": r.get("escalation_interval", 30),
            "enabled": bool(r.get("enabled")), "remark": r.get("remark", "")}


def _manage_alert_rules(ctx, action=None, rule_id=None, name="",
                        metric_type="", metric="", compare="",
                        threshold=None, duration_seconds=None,
                        scope=None, severity="", channels=None,
                        escalation_interval=None, exclude_keyword=None,
                        enabled=None, remark=None, **kw) -> dict:
    """告警规则管理（operator/admin，与 Web 端同等效力）：
    list/create/update/delete/toggle

    update 为局部更新（未提供的字段保持原值），合并后整体复校验，
    防止 metric_type/metric 跨字段组合失配被引擎静默忽略。
    """
    db = ctx.db_path
    action = action or "list"
    if action == "list":
        rows = dbm.list_records(db, "alert_rules", "", (), "id")
        return {"rules": [_rule_out(r) for r in rows],
                "hint": "scope.ids 为实体 id（项目/环境/服务器/服务）"}
    if action == "create":
        body = dict(
            name=name, metric_type=metric_type or "metric", metric=metric or "",
            compare=compare or ">",
            threshold=0 if threshold is None else threshold,
            duration_seconds=0 if duration_seconds is None else duration_seconds,
            scope=_jsonish(scope, "scope") or {"type": "all", "ids": []},
            severity=severity or "P2", channels=channels or [],
            escalation_interval=(30 if escalation_interval is None
                                 else escalation_interval),
            exclude_keyword=exclude_keyword or "",
            enabled=True if enabled is None else enabled, remark=remark or "")
        err = _validate_alert_rule(db, body)
        if err:
            raise ToolError(err)
        now = int(time.time())
        rid = dbm.create_record(db, "alert_rules", dict(
            name=body["name"].strip(), metric_type=body["metric_type"],
            metric=body["metric"], compare=body["compare"],
            threshold=float(body["threshold"]),
            duration_seconds=int(body["duration_seconds"]),
            scope=body["scope"], severity=body["severity"],
            channels=body["channels"],
            escalation_interval=int(body["escalation_interval"]),
            exclude_keyword=(body["exclude_keyword"] or "").strip(),
            enabled=1 if body["enabled"] else 0,
            remark=body["remark"], created_at=now, updated_at=now))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_alert_rule", "alert_rule", rid,
                         {"name": body["name"], "via": "agent"})
        return {"id": rid, "hint": "规则已生效，下一轮引擎评估即按其触发"}
    rule = dbm.get_record(db, "alert_rules", int(rule_id or 0)) \
        if rule_id else None
    if not rule:
        raise ToolError("缺少或不存在 rule_id")
    if action == "toggle":
        new_enabled = 0 if rule["enabled"] else 1
        dbm.update_record(db, "alert_rules", rule["id"],
                          {"enabled": new_enabled})
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_toggle_alert_rule", "alert_rule", rule["id"],
                         {"enabled": new_enabled, "via": "agent"})
        return {"id": rule["id"], "enabled": bool(new_enabled)}
    if action == "delete":
        dbm.delete_record(db, "alert_rules", rule["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_alert_rule", "alert_rule", rule["id"],
                         {"name": rule["name"], "via": "agent"})
        return {"ok": True, "id": rule["id"]}
    if action != "update":
        raise ToolError("action 仅支持 list/create/update/delete/toggle")
    # 局部更新：仅采用已提供的字段，其余保持原值；合并后整体复校验
    body = dict(rule)
    if name:
        body["name"] = name
    if metric_type:
        body["metric_type"] = metric_type
    if metric:
        body["metric"] = metric
    if compare:
        body["compare"] = compare
    if threshold is not None:
        body["threshold"] = threshold
    if duration_seconds is not None:
        body["duration_seconds"] = duration_seconds
    if scope is not None:
        body["scope"] = _jsonish(scope, "scope")
    if severity:
        body["severity"] = severity
    if channels is not None:
        body["channels"] = channels
    if escalation_interval is not None:
        body["escalation_interval"] = escalation_interval
    if exclude_keyword is not None:
        body["exclude_keyword"] = exclude_keyword
    if enabled is not None:
        body["enabled"] = bool(enabled)
    if remark is not None:
        body["remark"] = remark
    err = _validate_alert_rule(db, body)
    if err:
        raise ToolError(err)
    dbm.update_record(db, "alert_rules", rule["id"], dict(
        name=str(body["name"]).strip(), metric_type=body["metric_type"],
        metric=body["metric"], compare=body["compare"],
        threshold=float(body["threshold"]),
        duration_seconds=int(body["duration_seconds"]),
        scope=body["scope"], severity=body["severity"],
        channels=body["channels"],
        escalation_interval=int(body["escalation_interval"]),
        exclude_keyword=(body["exclude_keyword"] or "").strip(),
        enabled=1 if body["enabled"] else 0,
        remark=body["remark"], updated_at=int(time.time())))
    dbm.create_audit(db, ctx.user_id, ctx.username,
                     "agent_update_alert_rule", "alert_rule", rule["id"],
                     {"name": body["name"], "via": "agent"})
    return {"ok": True, "id": rule["id"]}


def _check_silence_scope_ids(db, scope) -> str:
    """静默 scope 悬空校验：rules/envs/servers 为对应实体 id"""
    checks = (("rules", "alert_rules", "manage_alert_rules(action=list)"),
              ("envs", "environments", "list_environments"),
              ("servers", "servers", "manage_servers(action=list)"))
    for key, table, tip in checks:
        ids = [x for x in scope.get(key) or []
               if isinstance(x, int) and not isinstance(x, bool)]
        valid = {r["id"] for r in dbm.list_records(db, table)}
        missing = [x for x in ids if x not in valid]
        if missing:
            return f"scope.{key} 引用了不存在的 id: {missing}（可用 {tip} 查询）"
    return ""


def _validate_silence(db, body) -> str:
    """静默窗口校验（镜像 alert_routes 创建/更新校验）；空串=通过"""
    if not (body.get("name") or "").strip():
        return "窗口名称必填"
    wtype = body.get("type")
    if wtype not in ("one_time", "recurring"):
        return "type 必须为 one_time 或 recurring"
    start_at, end_at = str(body.get("start_at", "")), str(body.get("end_at", ""))
    if wtype == "one_time":
        try:
            float(start_at); float(end_at)
        except (TypeError, ValueError):
            return "一次性窗口需 epoch 秒 start_at/end_at"
    elif not (_HHMM_RE.match(start_at) and _HHMM_RE.match(end_at)):
        return "周期窗口需 HH:MM 格式 start_at/end_at"
    scope = body.get("scope")
    if not isinstance(scope, dict) or not all(
            isinstance(scope.get(k), list) for k in ("rules", "envs", "servers")):
        return "scope 需为 {rules,envs,servers} 列表结构"
    err = _check_silence_scope_ids(db, scope)
    if err:
        return err
    if body.get("weekdays") is not None and not isinstance(
            body.get("weekdays"), list):
        return "weekdays 需为列表"
    return ""


def _silence_out(w) -> dict:
    return {"id": w["id"], "name": w["name"], "type": w.get("type"),
            "start_at": w.get("start_at"), "end_at": w.get("end_at"),
            "weekdays": w.get("weekdays") or [],
            "scope": (w.get("scope") or {"rules": [], "envs": [],
                                            "servers": []}),
            "enabled": bool(w.get("enabled")), "remark": w.get("remark", "")}


def _manage_silences(ctx, action=None, silence_id=None, name="",
                     type="", start_at="", end_at="", weekdays=None,
                     scope=None, enabled=None, remark=None, **kw) -> dict:
    """静默窗口管理（operator/admin，与 Web 端同等效力）：
    list/create/update/delete

    命中窗口的告警不发送通知（窗口过期后未通知的 firing 告警会补发）；
    scope 三列表均空 = 全局静默。
    """
    db = ctx.db_path
    action = action or "list"
    if action == "list":
        rows = dbm.list_records(db, "silence_windows", "", (), "id")
        return {"silences": [_silence_out(w) for w in rows],
                "hint": "scope.rules/envs/servers 为对应实体 id；全空=全局静默"}
    if action == "create":
        body = dict(
            name=name, type=type or "one_time", start_at=start_at or "",
            end_at=end_at or "", weekdays=weekdays or [],
            scope=_jsonish(scope, "scope") or {"rules": [], "envs": [],
                                                "servers": []},
            enabled=True if enabled is None else enabled, remark=remark or "")
        err = _validate_silence(db, body)
        if err:
            raise ToolError(err)
        wid = dbm.create_record(db, "silence_windows", dict(
            name=body["name"].strip(), type=body["type"],
            start_at=str(body["start_at"]), end_at=str(body["end_at"]),
            weekdays=body["weekdays"], scope=body["scope"],
            enabled=1 if body["enabled"] else 0,
            remark=body["remark"], created_at=int(time.time())))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_silence", "silence", wid,
                         {"name": body["name"], "type": body["type"],
                          "via": "agent"})
        return {"id": wid, "hint": "静默窗口已生效；窗口内新告警不通知"}
    win = dbm.get_record(db, "silence_windows", int(silence_id or 0)) \
        if silence_id else None
    if not win:
        raise ToolError("缺少或不存在 silence_id")
    if action == "delete":
        dbm.delete_record(db, "silence_windows", win["id"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_delete_silence", "silence", win["id"],
                         {"name": win["name"], "via": "agent"})
        return {"ok": True, "id": win["id"]}
    if action != "update":
        raise ToolError("action 仅支持 list/create/update/delete")
    # 局部更新：仅采用已提供的字段，其余保持原值
    body = dict(win)
    if name:
        body["name"] = name
    if type:
        body["type"] = type
    if start_at:
        body["start_at"] = str(start_at)
    if end_at:
        body["end_at"] = str(end_at)
    if weekdays is not None:
        body["weekdays"] = weekdays
    if scope is not None:
        body["scope"] = _jsonish(scope, "scope")
    if enabled is not None:
        body["enabled"] = bool(enabled)
    if remark is not None:
        body["remark"] = remark
    err = _validate_silence(db, body)
    if err:
        raise ToolError(err)
    dbm.update_record(db, "silence_windows", win["id"], dict(
        name=body["name"].strip(), type=body["type"],
        start_at=str(body["start_at"]), end_at=str(body["end_at"]),
        weekdays=body["weekdays"] or [], scope=body["scope"],
        enabled=1 if body["enabled"] else 0, remark=body["remark"]))
    dbm.create_audit(db, ctx.user_id, ctx.username,
                     "agent_update_silence", "silence", win["id"],
                     {"name": body["name"], "via": "agent"})
    return {"ok": True, "id": win["id"]}


# ---------- 日志存储治理（仅 admin） ----------


def _dir_stats(base: str) -> dict:
    """目录统计：文件数 + 总字节数（与 Web 端 /logs/storage 同口径；不存在返回零值）"""
    files = bytes_total = 0
    if os.path.isdir(base):
        from pathlib import Path
        for p in Path(base).rglob("*"):
            if p.is_file():
                try:
                    files += 1
                    bytes_total += p.stat().st_size
                except OSError:
                    pass
    return {"files": files, "mb": round(bytes_total / 1048576, 1)}


def _manage_log_storage(ctx, action="status", log_retention_days=None,
                        log_archive_days=None, **kw) -> dict:
    """日志存储治理（仅 admin，与 Web 日志中心同等效力）：
    status 存储概况 / settings 保留期调整 / cleanup 手动清理（与每日自动维护同一逻辑）"""
    _require_admin(ctx, "日志存储治理")
    db = ctx.db_path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(root, "data", "logs")

    def _settings_map(conn) -> dict:
        out = {}
        for key, default in (("log_retention_days", 7),
                             ("log_archive_days", 30)):
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            try:
                out[key] = int(json.loads(row["value"])) if row else default
            except (ValueError, TypeError):
                out[key] = default
        return out

    if action == "status":
        conn = dbm.get_conn(db)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM log_entries"
            ).fetchone()
            entries, oldest_ts = row[0], row[1]
            freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            settings = _settings_map(conn)
        finally:
            conn.close()
        try:
            db_size = os.path.getsize(db)
        except OSError:
            db_size = 0
        try:
            from webapp.log_collector import get_collector
            collector_running = get_collector() is not None
        except Exception:
            collector_running = False
        return {"log_entries": entries, "oldest": _fmt_ts(oldest_ts),
                "db_size_mb": round(db_size / 1048576, 1),
                "db_freelist_mb": round(freelist * page_size / 1048576, 1),
                "retention_days": settings["log_retention_days"],
                "archive_days": settings["log_archive_days"],
                "collect_dir": _dir_stats(os.path.join(log_dir, "collect")),
                "archive_dir": _dir_stats(os.path.join(log_dir, "archive")),
                "collector_running": collector_running,
                "hint": "cleanup 立即按保留策略清理过期日志并回收空间"
                        "（与每日自动维护同一逻辑）"}
    if action == "settings":
        labels = {"log_retention_days": "日志保留天数",
                  "log_archive_days": "归档保留天数"}
        fields = {}
        for key, val in (("log_retention_days", log_retention_days),
                         ("log_archive_days", log_archive_days)):
            if val is None:
                continue
            if isinstance(val, bool) or not isinstance(val, int):
                raise ToolError(f"{labels[key]}需为整数")
            if val < 1 or val > 365:
                raise ToolError(f"{labels[key]}需在 1~365 之间")
            fields[key] = val
        if not fields:
            conn = dbm.get_conn(db)
            try:
                current = _settings_map(conn)
            finally:
                conn.close()
            return {"ok": True, "settings": current}
        now = int(time.time())
        conn = dbm.get_conn(db)
        try:
            for key, val in fields.items():
                existing = conn.execute(
                    "SELECT id FROM platform_settings WHERE key=?", (key,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE platform_settings SET value=?, updated_at=? WHERE key=?",
                        (json.dumps(val), now, key))
                else:
                    conn.execute(
                        "INSERT INTO platform_settings (key, value, updated_at) "
                        "VALUES (?, ?, ?)", (key, json.dumps(val), now))
            conn.commit()
        finally:
            conn.close()
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_log_settings", "config", 0,
                         {"settings": fields, "via": "agent"})
        return {"ok": True, "updated": fields}
    if action == "cleanup":
        from webapp.log_collector import run_manual_cleanup
        stats = run_manual_cleanup(db, log_dir)
        if stats.get("busy"):
            raise ToolError("清理正在进行中，请稍后重试")
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_manual_log_cleanup", "config", 0,
                         {"deleted_entries": stats.get("deleted_entries", 0),
                          "archived_files": stats.get("archived_files", 0),
                          "purged_files": stats.get("purged_files", 0),
                          "db_mb_before": round(stats.get("db_size_before", 0) / 1048576, 1),
                          "db_mb_after": round(stats.get("db_size_after", 0) / 1048576, 1),
                          "via": "agent"})
        return {"ok": True, "stats": stats,
                "hint": "清理按当前保留策略执行；可用 action=settings 调整保留期"}
    raise ToolError("action 仅支持 status/settings/cleanup")


# ---------- 服务依赖拓扑管理（operator/admin） ----------

def _manage_dependencies(ctx, action=None, project=None, dependency_id=None,
                          source="", target="", dep_type="manual", **kw) -> dict:
    """服务依赖关系管理（与 Web 端拓扑页同等效力）：source 依赖 target
    （发布顺序上 target 先发布）。create 含环检测；对话侧比 Web 更严：
    服务名悬空直接拒绝（防幻觉死配置）。"""
    db = ctx.db_path
    if action not in ("list", "create", "delete"):
        raise ToolError("action 仅支持 list/create/delete")
    if action in ("list", "create") and not project:
        raise ToolError("action=list/create 需要项目 project")

    if action == "list":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError(f"项目不存在: {project}")
        rows = dbm.list_records(db, "service_dependencies", "project_id=?",
                                (proj["id"],), "id")
        return {"project": proj["code"],
                "dependencies": [{"id": d["id"], "source": d["source_service"],
                                  "target": d["target_service"],
                                  "type": d.get("dep_type", "manual")}
                                 for d in rows],
                "hint": "source 依赖 target（target 先发布）；"
                        "service_topology metric=impact/publish_order 消费本图"}

    if action == "create":
        proj = _resolve_project(db, project or "")
        if not proj:
            raise ToolError(f"项目不存在: {project}")
        source, target = str(source or "").strip(), str(target or "").strip()
        if not source or not target:
            raise ToolError("缺少 source/target（服务名）")
        if dep_type not in ("manual", "dubbo"):
            raise ToolError("dep_type 仅支持 manual/dubbo（默认 manual）")
        svc_names = {s["name"] for s in
                     dbm.list_services(db, proj["id"], enabled_only=True)}
        for name in (source, target):
            if name not in svc_names:
                raise ToolError(f"服务不存在或不属于该项目: {name}（可用 "
                                f"list_services 核对）")
        if source == target:
            raise ToolError("不能依赖自己")
        # 环检测：若 target 已能沿依赖边到达 source，加边 source→target 即成环
        deps = dbm.list_records(db, "service_dependencies", "project_id=?",
                                (proj["id"],), "id")
        adj = {}
        for d in deps:
            adj.setdefault(d["source_service"], []).append(d["target_service"])
        visited, queue = set(), [target]
        while queue:
            curr = queue.pop(0)
            if curr == source:
                raise ToolError("添加此依赖会形成循环依赖"
                                "（可先 delete 拆解既有环）")
            if curr in visited:
                continue
            visited.add(curr)
            queue.extend(n for n in adj.get(curr, []) if n not in visited)
        try:
            did = dbm.create_record(db, "service_dependencies", dict(
                project_id=proj["id"], source_service=source,
                target_service=target, dep_type=dep_type,
                created_at=int(time.time())))
        except Exception as e:
            if "UNIQUE" in str(e):
                raise ToolError("依赖关系已存在")
            raise
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_add_dependency", "service_dependency", did,
                         {"project": proj["code"], "source": source,
                          "target": target, "dep_type": dep_type,
                          "via": "agent"})
        return {"ok": True, "id": did, "source": source, "target": target,
                "hint": "已记录；service_topology metric=publish_order 可查新发布顺序"}

    # delete
    try:
        dependency_id = int(dependency_id)
    except (TypeError, ValueError):
        raise ToolError("action=delete 需要 dependency_id（整数，可先 list 查询）")
    rec = dbm.get_record(db, "service_dependencies", dependency_id)
    if not rec:
        raise ToolError(f"依赖不存在: #{dependency_id}")
    dbm.delete_record(db, "service_dependencies", dependency_id)
    dbm.create_audit(db, ctx.user_id, ctx.username,
                     "agent_delete_dependency", "service_dependency",
                     dependency_id,
                     {"project": rec.get("project_id"),
                      "source": rec.get("source_service"),
                      "target": rec.get("target_service"), "via": "agent"})
    return {"ok": True, "id": dependency_id}


# ---------- 到期监控管理（operator/admin；config 写仅 admin） ----------

_MONITOR_TYPES = ("ssl", "cloud_renewal", "license", "custom", "domain")


def _monitor_out(m, now=None):
    """监控项紧凑输出（days_left 按 expiry_at 实时重算，镜像 Web list_monitors）"""
    now = now or int(time.time())
    days_left = m.get("days_left")
    if m.get("expiry_at"):
        days_left = max(0, (int(m["expiry_at"]) - now) // 86400)
    return {"id": m["id"], "type": m["type"], "name": m["name"],
            "target": m["target"], "port": m.get("port"),
            "expiry_at": _fmt_ts(m.get("expiry_at")),
            "days_left": days_left, "warn_days": m.get("warn_days"),
            "severity": m.get("severity", ""),
            "channels": m.get("channels") or [],
            "enabled": bool(m.get("enabled")), "owner": m.get("owner", ""),
            "renewal_status": m.get("renewal_status", ""),
            "last_checked": _fmt_ts(m.get("last_checked")),
            "error": m.get("error", "")}


def _validate_monitor_args(body, partial=False):
    """监控项字段校验（镜像 Web _validate_monitor）；返回错误信息，空串=通过"""
    if not partial:
        if body.get("type") not in _MONITOR_TYPES:
            return "type 仅支持 ssl/cloud_renewal/license/custom/domain"
        if not (body.get("name") or "").strip():
            return "名称 name 必填"
        if not (body.get("target") or "").strip():
            return "目标 target 必填"
    if "type" in body and body["type"] not in _MONITOR_TYPES:
        return "type 仅支持 ssl/cloud_renewal/license/custom/domain"
    if "port" in body:
        try:
            p = int(body["port"])
            if body.get("type") != "ssl" and p == 0:
                pass
            elif p < 1 or p > 65535:
                return "port 需在 1-65535 之间"
        except (TypeError, ValueError):
            return "port 必须为数字"
    if "warn_days" in body:
        try:
            if int(body["warn_days"]) < 0:
                return "warn_days 需为非负整数"
        except (TypeError, ValueError):
            return "warn_days 必须为数字"
    if body.get("severity") and body["severity"] not in _SEVERITIES:
        return "severity 仅支持 P0/P1/P2/P3"
    if "channels" in body:
        ch = body.get("channels") or []
        if not isinstance(ch, list):
            return "channels 需为列表"
        if ch and not set(ch) <= set(_ALERT_CHANNELS):
            return f"channels 仅支持 {'/'.join(_ALERT_CHANNELS)}"
    if not partial and body.get("type") not in ("ssl", "domain"):
        if not body.get("expiry_at"):
            return "非 ssl/domain 类型需提供 expiry_at（epoch 秒；domain 可自动 WHOIS）"
    return ""


def _manage_monitors(ctx, action=None, monitor_id=None, type="", name="",
                     target="", port=None, expiry_at=None, warn_days=None,
                     severity="", channels=None, owner="",
                     renewal_status="", default_warn_days=None,
                     default_severity="", default_channels=None,
                     ssl_check_interval=None, **kw) -> dict:
    """到期监控管理（operator/admin，与 Web 到期监控页同等效力）：
    证书/域名/云续费/license/自定义到期项的 CRUD 与手动探测。"""
    db = ctx.db_path
    if action not in ("list", "create", "update", "delete", "toggle",
                      "check", "whois", "config"):
        raise ToolError("action 仅支持 list/create/update/delete/toggle/"
                        "check/whois/config")

    if action == "list":
        now = int(time.time())
        rows = dbm.list_records(db, "custom_monitors", "", (), "id")
        return {"monitors": [_monitor_out(m, now) for m in rows],
                "count": len(rows),
                "hint": "days_left 实时重算（0=已过期）；check 手动探测，"
                        "whois 查询域名到期；cert_monitors 工具为只读紧凑视图"}

    if action == "config":
        conn = dbm.get_conn(db)
        try:
            row = conn.execute(
                "SELECT value FROM platform_settings WHERE key=?",
                ("custom_monitor_config",)).fetchone()
            current = json.loads(row["value"]) if row and row["value"] else {}
        finally:
            conn.close()
        if not current:
            current = {"default_warn_days": 30, "default_severity": "P1",
                       "default_channels": ["feishu"],
                       "ssl_check_interval": 3600}
        writes = {}
        if default_warn_days is not None:
            if isinstance(default_warn_days, bool) \
                    or not isinstance(default_warn_days, int) \
                    or default_warn_days < 0:
                raise ToolError("default_warn_days 需为非负整数")
            writes["default_warn_days"] = default_warn_days
        if default_severity:
            if default_severity not in _SEVERITIES:
                raise ToolError("default_severity 仅支持 P0/P1/P2/P3")
            writes["default_severity"] = default_severity
        if default_channels is not None:
            if not isinstance(default_channels, list) \
                    or not set(default_channels) <= set(_ALERT_CHANNELS):
                raise ToolError(f"default_channels 仅支持 {'/'.join(_ALERT_CHANNELS)}")
            writes["default_channels"] = default_channels
        if ssl_check_interval is not None:
            if isinstance(ssl_check_interval, bool) \
                    or not isinstance(ssl_check_interval, int) \
                    or ssl_check_interval < 60:
                raise ToolError("ssl_check_interval 需为不小于 60 的整数（秒）")
            writes["ssl_check_interval"] = ssl_check_interval
        if not writes:
            return {"ok": True, "config": current,
                    "hint": "传 default_warn_days/default_severity/"
                            "default_channels/ssl_check_interval 之一即可更新（仅 admin）"}
        _require_admin(ctx, "到期监控全局配置写入")
        current.update(writes)
        now = int(time.time())
        conn = dbm.get_conn(db)
        try:
            existing = conn.execute(
                "SELECT id FROM platform_settings WHERE key=?",
                ("custom_monitor_config",)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE platform_settings SET value=?, updated_at=? "
                    "WHERE key=?",
                    (json.dumps(current, ensure_ascii=False), now,
                     "custom_monitor_config"))
            else:
                conn.execute(
                    "INSERT INTO platform_settings (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    ("custom_monitor_config",
                     json.dumps(current, ensure_ascii=False), now))
            conn.commit()
        finally:
            conn.close()
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_monitor_config", "config", 0,
                         {"config": current, "via": "agent"})
        return {"ok": True, "config": current}

    if action == "whois":
        # 临时查询域名到期（无需建监控项）
        domain = str(target or "").strip()
        if not domain:
            raise ToolError("action=whois 需要 target（域名）")
        from webapp.routes.alert_routes import _whois_lookup
        result = _whois_lookup(domain)
        if result.get("error"):
            raise ToolError(f"WHOIS 查询失败: {result['error']}")
        return {"domain": domain, "expiry_at": _fmt_ts(result["expiry_at"]),
                "days_left": result["days_left"],
                "hint": "可 action=create type=domain 建立持续监控"}

    if action == "check":
        try:
            monitor_id = int(monitor_id)
        except (TypeError, ValueError):
            raise ToolError("action=check 需要 monitor_id（整数）")
        m = dbm.get_record(db, "custom_monitors", monitor_id)
        if not m:
            raise ToolError(f"监控项不存在: #{monitor_id}")
        now = int(time.time())
        if m["type"] == "ssl":
            from webapp.ssl_monitor import get_ssl_monitor
            mon = get_ssl_monitor()
            if not mon:
                raise ToolError("SSL 监控未启动（探测不可用）")
            result = mon.probe_one_now(monitor_id)
            if isinstance(result, dict) and result.get("error"):
                raise ToolError(f"探测失败: {result['error']}")
            return {"ok": True, "monitor": _monitor_out(result, now),
                    "hint": "已即时探测并更新剩余天数"}
        if m["type"] == "domain":
            from webapp.routes.alert_routes import _whois_lookup
            result = _whois_lookup(m["target"])
            if result.get("error"):
                dbm.update_record(db, "custom_monitors", monitor_id, dict(
                    last_checked=now, error=result["error"], updated_at=now))
                raise ToolError(f"WHOIS 查询失败: {result['error']}")
            dbm.update_record(db, "custom_monitors", monitor_id, dict(
                last_checked=now, expiry_at=result["expiry_at"],
                days_left=result["days_left"], error="", updated_at=now))
            return {"ok": True, "domain": m["target"],
                    "expiry_at": _fmt_ts(result["expiry_at"]),
                    "days_left": result["days_left"],
                    "hint": "已更新该监控项的到期时间"}
        raise ToolError("check 仅支持 ssl（探测）/domain（WHOIS）类型")

    if action == "create":
        body = {k: v for k, v in (("type", type), ("name", name),
                                  ("target", target), ("port", port),
                                  ("expiry_at", expiry_at),
                                  ("warn_days", warn_days),
                                  ("severity", severity),
                                  ("channels", channels), ("owner", owner))
                if v is not None and v != ""}
        err = _validate_monitor_args(body)
        if err:
            raise ToolError(err)
        now = int(time.time())
        expiry = body.get("expiry_at")
        days_left = None
        if expiry:
            if isinstance(expiry, bool) or not isinstance(expiry, int):
                raise ToolError("expiry_at 需为 epoch 秒（整数）")
            days_left = max(0, (expiry - now) // 86400)
        # domain 类型无到期时间时自动 WHOIS（失败不阻断创建，后续可 check 补）
        if body.get("type") == "domain" and not expiry:
            from webapp.routes.alert_routes import _whois_lookup
            whois = _whois_lookup(str(target).strip())
            if whois.get("expiry_at"):
                expiry = whois["expiry_at"]
                days_left = whois["days_left"]
        mid = dbm.create_record(db, "custom_monitors", dict(
            type=body["type"], name=str(name).strip(),
            target=str(target).strip(), port=body.get("port", 443),
            expiry_at=expiry, days_left=days_left,
            warn_days=body.get("warn_days", 0),
            severity=body.get("severity", ""),
            channels=body.get("channels", []), enabled=1,
            owner=body.get("owner", ""), renewal_status="",
            error="", created_at=now, updated_at=now))
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_create_monitor", "custom_monitor", mid,
                         {"type": body["type"], "name": name,
                          "target": target, "via": "agent"})
        return {"ok": True, "id": mid,
                "hint": "已创建；ssl 类型可用 action=check 手动探测一次"}

    if action == "update":
        try:
            monitor_id = int(monitor_id)
        except (TypeError, ValueError):
            raise ToolError("action=update 需要 monitor_id（整数）")
        old = dbm.get_record(db, "custom_monitors", monitor_id)
        if not old:
            raise ToolError(f"监控项不存在: #{monitor_id}")
        body = {k: v for k, v in (("type", type), ("name", name),
                                  ("target", target), ("port", port),
                                  ("expiry_at", expiry_at),
                                  ("warn_days", warn_days),
                                  ("severity", severity),
                                  ("channels", channels),
                                  ("owner", owner),
                                  ("renewal_status", renewal_status))
                if v is not None and v != ""}
        err = _validate_monitor_args(body, partial=True)
        if err:
            raise ToolError(err)
        new_expiry = body.get("expiry_at", old.get("expiry_at"))
        days_left = None
        if new_expiry:
            if isinstance(new_expiry, bool) or not isinstance(new_expiry, int):
                raise ToolError("expiry_at 需为 epoch 秒（整数）")
            days_left = max(0, (new_expiry - int(time.time())) // 86400)
        fields = dict(
            type=body.get("type", old["type"]),
            name=(body.get("name") or old["name"]).strip(),
            target=(body.get("target") or old["target"]).strip(),
            port=body.get("port", old.get("port")),
            expiry_at=new_expiry, days_left=days_left,
            warn_days=body.get("warn_days", old.get("warn_days")),
            severity=body.get("severity", old.get("severity", "")),
            channels=body.get("channels", old.get("channels") or []),
            owner=body.get("owner", old.get("owner", "")),
            renewal_status=body.get("renewal_status",
                                     old.get("renewal_status", "")),
            updated_at=int(time.time()))
        dbm.update_record(db, "custom_monitors", monitor_id, fields)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_update_monitor", "custom_monitor", monitor_id,
                         {"name": fields["name"], "fields": sorted(body),
                          "via": "agent"})
        return {"ok": True, "id": monitor_id}

    if action == "toggle":
        try:
            monitor_id = int(monitor_id)
        except (TypeError, ValueError):
            raise ToolError("action=toggle 需要 monitor_id（整数）")
        m = dbm.get_record(db, "custom_monitors", monitor_id)
        if not m:
            raise ToolError(f"监控项不存在: #{monitor_id}")
        new_enabled = 0 if m.get("enabled") else 1
        dbm.update_record(db, "custom_monitors", monitor_id,
                          {"enabled": new_enabled})
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_toggle_monitor", "custom_monitor", monitor_id,
                         {"enabled": new_enabled, "via": "agent"})
        return {"ok": True, "enabled": bool(new_enabled)}

    # delete
    try:
        monitor_id = int(monitor_id)
    except (TypeError, ValueError):
        raise ToolError("action=delete 需要 monitor_id（整数，可先 list 查询）")
    m = dbm.get_record(db, "custom_monitors", monitor_id)
    if not m:
        raise ToolError(f"监控项不存在: #{monitor_id}")
    dbm.delete_record(db, "custom_monitors", monitor_id)
    dbm.create_audit(db, ctx.user_id, ctx.username,
                     "agent_delete_monitor", "custom_monitor", monitor_id,
                     {"name": m.get("name"), "type": m.get("type"),
                      "via": "agent"})
    return {"ok": True, "id": monitor_id}


# ---------- 监控刷新（operator/admin） ----------

def _refresh_monitor(ctx, **kw) -> dict:
    """立即触发一轮监控采集刷新（与 Web /monitor/refresh 同等效力）：
    数据可能过时时先刷一轮再查 service_status/monitor_metrics。"""
    from webapp.monitor import get_monitor
    mon = get_monitor()
    if not mon:
        raise ToolError("监控采集器未启动（刷新不可用）")
    mon.refresh()
    return {"ok": True,
            "hint": "已触发一轮采集刷新；稍后用 service_status/"
                    "monitor_metrics 查最新数据"}


# ---------- 配置导入导出（仅 admin） ----------

def _manage_config_backup(ctx, action=None, **kw) -> dict:
    """平台配置导入/导出（与 Web /config/export|import 同等效力，仅 admin）：
    export 把 DB 配置全量导出为 YAML；import 从 config/ 目录回灌 DB。"""
    _require_admin(ctx, "配置导入导出")
    db = ctx.db_path
    from pathlib import Path
    root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if action == "export":
        from webapp.exporter import export_all
        target = root / "data" / "export" / "config"
        written = export_all(db, target)
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_export_config", "config", 0,
                         {"files": written, "via": "agent"})
        return {"ok": True, "files": written, "dir": str(target),
                "count": len(written),
                "hint": "已导出到 data/export/config/（单项目时兼容名可直接"
                        "复制为 config/；多项目按 code 加后缀）"}
    if action == "import":
        from webapp.importer import import_from_config
        config_dir = root / "config"
        results = import_from_config(db, config_dir)
        summary = {k: {"ok": ok, "message": msg}
                   for k, (ok, msg) in results.items()}
        ok_n = sum(1 for v in summary.values() if v["ok"])
        dbm.create_audit(db, ctx.user_id, ctx.username,
                         "agent_import_config", "config", 0,
                         {"results": summary, "via": "agent"})
        return {"ok": True, "results": summary,
                "imported": ok_n, "total": len(summary),
                "hint": "从 config/ 目录回灌（逐文件结果见 results；"
                        "文件不存在也计入失败项）"}
    raise ToolError("action 仅支持 export/import")


def _live_status(ctx, **kw) -> dict:
    """实时通道体检：SSE 订阅数 / 事件量 / 慢消费者丢弃 + 生产者线程在否"""
    from webapp import events
    from webapp.monitor import get_monitor
    from webapp.alert_engine import get_engine
    from webapp.routes.stream_routes import KNOWN_TOPICS
    st = events.stats()
    mon = get_monitor()
    return {
        "ok": True,
        "subscribers": st["subscribers"],
        "published": st["published"],
        "dropped": st["dropped"],
        "topics": list(KNOWN_TOPICS),
        "monitor_running": mon is not None,
        "monitor_interval": getattr(mon, "_interval", None),
        "alert_engine_running": get_engine() is not None,
        "hint": ("subscribers=0 且 monitor_running=true 说明页面未建流（前端已退回"
                 "降级轮询，数据仍会刷新只是变慢）；dropped 持续增长说明有客户端"
                 "消费不过来，前端会收到 resync 自行全量重取，无需人工处理；"
                 "本统计为当前服务进程内口径（单进程部署即全局）"),
    }


# ---------- 注册表 ----------

def _spec(name, description, params, permission, fn):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": params,
                           "required": [p for p, s in params.items()
                                        if s.get("required")]},
            "permission": permission, "fn": fn}


def build_tools() -> list:
    """全量工具表（含 read/write）；角色过滤在 build_schemas/execute 层做"""
    num = {"type": "integer", "required": True}
    num_opt = {"type": "integer"}
    str_opt = {"type": "string"}
    str_req = {"type": "string", "required": True}
    bool_opt = {"type": "boolean"}
    return [
        _spec("list_projects", "列出平台全部项目（含服务/环境/流水线数量）",
              {}, "read", _list_projects),
        _spec("get_project", "查看项目详情：服务清单、环境（含绑定服务器）、流水线",
              {"project": str_req}, "read", _get_project),
        _spec("list_services", "列出服务；project 可省略（省略时列出全部）",
              {"project": str_opt}, "read", _list_services),
        _spec("list_environments", "列出项目下的环境",
              {"project": str_req}, "read", _list_environments),
        _spec("env_servers", "查看环境绑定的服务器清单（发布顺序）",
              {"project": str_req, "env": str_req}, "read", _env_servers),
        _spec("service_status", "查询服务监控状态（可空参查全局摘要；monitor 未启动时返回提示）",
              {"project": str_opt, "env": str_opt, "service": str_opt},
              "read", _service_status),
        _spec("monitor_metrics", "监控指标历史查询：metric=response_times（服务"
              "响应时间历史，含成功率/均值汇总）/ snapshots（每台服务器最新"
              "快照扩展指标：swap/网络/磁盘IO/TCP/重启/证书天数）。service_status"
              "看实时状态，本工具看历史与扩展指标",
              {"metric": str_req, "project": str_opt, "env": str_opt,
               "service": str_opt, "hours": num_opt, "limit": num_opt},
              "read", _monitor_metrics),
        _spec("recent_tasks", "查看发布任务记录（可按项目/状态过滤）",
              {"project": str_opt, "status": str_opt, "limit": num_opt},
              "read", _recent_tasks),
        _spec("task_detail", "查看单个发布任务详情与最近日志",
              {"task_id": num}, "read", _task_detail),
        _spec("list_alerts", "查看告警列表（默认未解决 firing；可按级别/项目/服务过滤）",
              {"status": str_opt, "severity": str_opt, "limit": num_opt,
               "project": str_opt, "env": str_opt, "service": str_opt},
              "read", _list_alerts),
        _spec("alert_detail", "查看告警详情（含通知事件）",
              {"alert_id": num}, "read", _alert_detail),
        _spec("search_logs", "检索采集的容器日志（keyword 走全文检索；可按服务/环境/时间窗过滤）",
              {"keyword": str_opt, "service": str_opt, "env": str_opt,
               "minutes": num_opt, "limit": num_opt}, "read", _search_logs),
        _spec("logs_context", "查看某条日志的前后上下文（排障定位报错前后文；"
              "id 来自 search_logs 结果，与 Web 日志中心同等语义）",
              {"id": num, "lines": num_opt}, "read", _logs_context),
        _spec("release_window", "查看项目的发布窗口状态（block 禁发/warn 告警确认/无窗口）",
              {"project": str_req}, "read", _release_window),
        _spec("canary_configs", "查看项目灰度策略（分批步骤）",
              {"project": str_req}, "read", _canary_configs),
        _spec("cert_monitors", "查看证书/自定义监控项（到期天数、续期状态）",
              {}, "read", _cert_monitors),
        _spec("audit_logs", "查看操作审计日志",
              {"limit": num_opt}, "read", _audit_logs),
        _spec("search_cases", "检索历史故障案例（相似场景经验，含评分）",
              {"keyword": str_opt, "service": str_opt, "limit": num_opt},
              "read", _search_cases),
        _spec("case_detail", "查看案例详情",
              {"case_id": num}, "read", _case_detail),
        _spec("advice_list", "查看 AI 建议列表（scope: canary/task_failed/alert/precheck；"
              "status: pending 待处理/adopted 已采纳/dismissed 已忽略，默认全部）",
              {"scope": str_opt, "ref_id": num_opt, "limit": num_opt,
               "status": str_opt},
              "read", _advice_list),
        _spec("pending_approvals", "查看待审批的 Agent 发布计划（人工审批入口）",
              {"limit": num_opt}, "read", _pending_approvals),
        _spec("run_detail", "查看审批单（计划/状态/审批人/任务关联）",
              {"run_id": num}, "read", _run_detail),
        _spec("service_topology",
              "服务依赖拓扑与发布编排查询：metric=topology（项目依赖全图，"
              "含 dubbo 自动检测）/ impact（单服务上下游直接+间接影响，发布前"
              "评估）/ publish_order（建议发布顺序，被依赖的先发布，含环检测）/"
              " snapshots（部署快照列表，部署成功自动写入）/ compare（两次快照"
              "间服务版本差异；project+env 对比最近两次或 old_id+new_id 手动指定）",
              {"metric": str_req, "project": str_opt, "env": str_opt,
               "service": str_opt, "old_id": num_opt, "new_id": num_opt,
               "limit": num_opt},
              "read", _service_topology),
        # ---- write（operator/admin）----
        _spec("analyze_alert", "对告警做归因分析并保存建议（含近期发布/日志/案例关联）",
              {"alert_id": num}, "write", _analyze_alert),
        _spec("precheck", "发布前预检：检查目标/服务器/版本/窗口/近期健康度，输出检查清单",
              {"target": str_req, "env": str_req, "version": str_opt},
              "write", _precheck),
        _spec("ack_alert", "确认（ack）一条告警，操作留审计",
              {"alert_id": num, "note": str_opt}, "write", _ack_alert),
        _spec("plan_operation",
              "生成发布操作审批单：op=deploy/rollback/restart，target=服务或流水线名；"
              "生成后需人工审批（Web 审批单页或用户明确指示 decide_run）才执行",
              {"op": str_req, "target": str_req, "env": str_req,
               "version": str_opt, "previous_version": str_opt,
               "canary": bool_opt, "skip_build": bool_opt,
               "auto_version": bool_opt, "no_git_tag": bool_opt,
               "intent": str_opt}, "write", _plan_operation),
        _spec("decide_run",
              "审批发布审批单（与 Web 端同等效力，决策者身份落审计）。"
              "仅当用户明确表达批准/驳回某一审批单时调用；先用 "
              "pending_approvals/run_detail 核对 run_id 与内容，不得凭记忆猜测；"
              "action=approve/reject；approve 即提交任务执行；"
              "审批单含发布窗口告警时必须用户已确认风险并显式 force_window=true；"
              "note 为决策备注（会落审计）",
              {"run_id": num, "action": str_req, "note": str_opt,
               "force_window": bool_opt}, "write", _decide_run),
        _spec("case_save", "把一条 AI 建议（告警归因/失败归因）沉淀为案例草稿",
              {"advice_id": num}, "write", _case_save),
        _spec("case_update", "补全/修正案例内容（title/symptom/service/env/version/root_cause/action/result/lesson/tags）",
              {"case_id": num, "root_cause": str_opt, "action": str_opt,
               "result": str_opt, "lesson": str_opt, "title": str_opt,
               "symptom": str_opt, "service": str_opt, "env": str_opt,
               "version": str_opt,
               "tags": {"type": "array", "items": {"type": "string"}}},
              "write", _case_update),
        _spec("manage_projects",
              "项目 CRUD（仅 admin）。action=list/create/update/delete；"
              "create 需 name+code（code 仅字母数字_-，导出时用作目录名），"
              "description/git_org 可选；创建后可用 manage_services 注册服务",
              {"action": str_req, "project_id": num_opt, "name": str_opt,
               "code": str_opt, "description": str_opt,
               "git_org": str_opt},
              "write", _manage_projects),
        _spec("manage_services",
              "服务注册与管理（仅 admin）。action=list/create/update/delete；"
              "create 需 project+name+image_name+service_name；"
              "git_repo 支持相对目录名或 http(s) 远程 URL（首次构建自动 clone）；"
              "nacos_groups/dubbo_services/log_error_keywords 为字符串数组，"
              "build_order 为 [{name,path,goal}]（frontend 用 commands）",
              {"action": str_req, "project": str_opt, "service_id": num_opt,
               "name": str_opt, "image_name": str_opt, "service_name": str_opt,
               "docker_context": str_opt, "dockerfile": str_opt,
               "build_type": str_opt, "snailjob_group": str_opt,
               "health_url": str_opt, "health_port": num_opt,
               "clear_log_dir": str_opt, "git_repo": str_opt,
               "git_branch": str_opt, "git_pull_before_build": bool_opt,
               "git_tag_enabled": bool_opt, "enabled": bool_opt,
               "nacos_groups": {"type": "array", "items": {"type": "string"}},
               "dubbo_services": {"type": "array", "items": {"type": "string"}},
               "log_error_keywords": {"type": "array", "items": {"type": "string"}},
               "build_order": {"type": "array", "items": {"type": "object"}}},
              "write", _manage_services),
        _spec("manage_environments",
              "环境管理（仅 admin）。action=list/create/clone/update/delete；"
              "create 需 project+name（仅字母数字_-）；registry/nacos/mysql_snailjob/"
              "ssh/build/deploy 为配置对象；clone 为接入捷径：从既有项目"
              "source_project/source_env 复制环境（含服务器绑定）到 project；"
              "新项目无环境时 plan_operation 会拒单，须先建环境",
              {"action": str_req, "project": str_opt,
               "environment_id": num_opt, "name": str_opt,
               "is_default": bool_opt, "monitor_enabled": bool_opt,
               "registry": {"type": "object"}, "nacos": {"type": "object"},
               "mysql_snailjob": {"type": "object"}, "ssh": {"type": "object"},
               "build": {"type": "object"}, "deploy": {"type": "object"},
               "docker_compose_path": str_opt, "maven_profile": str_opt,
               "source_project": str_opt, "source_env": str_opt},
              "write", _manage_environments),
        _spec("manage_servers",
              "服务器管理（仅 admin）。action=list/create/bind/unbind/"
              "test/update；bind 为 upsert：host 未注册时按参数先注册再绑定到"
              "project+environment（幂等）；发布目标即环境绑定清单；"
              "test 为 SSH 连通测试（按 host 或 server_id 定位已注册服务器，"
              "服务器级凭据优先、缺省回退首个绑定环境 ssh）；"
              "ssh_port/ssh_user/ssh_password/ssh_key_file 不传则继承环境级配置",
              {"action": str_req, "host": str_opt, "server_id": num_opt,
               "project": str_opt, "environment": str_opt,
               "ssh_port": num_opt, "ssh_user": str_opt,
               "ssh_password": str_opt, "ssh_key_file": str_opt,
               "nacos_ip": str_opt, "remark": str_opt, "enabled": bool_opt},
              "write", _manage_servers),
        _spec("manage_pipelines",
              "发布流水线编排（仅 admin）。action=list/create/update/delete；"
              "create 需 project+name+stages；stages 三种形态：服务名数组"
              "[\"a\",\"b\"]（单 stage 并行）、嵌套数组[[\"a\"],[\"b\"]]"
              "（多 stage 顺序）、对象[{\"services\":[{\"service_id\":1}]}]；"
              "多服务分阶段发布用流水线，发布走 plan_operation target=流水线名；"
              "build_first=true 先统一构建全部镜像再逐 stage 发布",
              {"action": str_req, "project": str_opt,
               "pipeline_id": num_opt, "name": str_opt,
               "stages": {"type": "array"}, "build_first": bool_opt},
              "write", _manage_pipelines),
        _spec("manage_schedules",
              "管理定时发布计划。action=list/create/update/delete/toggle/run；"
              "create 需 project+name+type（once 需 schedule_at Unix 秒，"
              "cron 需 cron_expr），service 可选指定服务级计划；"
              "run 生成审批单需人工审批后才执行",
              {"action": str_req, "project": str_opt, "schedule_id": num_opt,
               "name": str_opt, "type": str_opt, "service": str_opt,
               "env": str_opt, "version": str_opt,
               "previous_version": str_opt, "schedule_at": num_opt,
               "cron_expr": str_opt,
               "extra": {"type": "array", "items": {"type": "string"}}},
              "write", _manage_schedules),
        _spec("manage_windows",
              "管理发布窗口（仅 admin）。action=list/create/update/delete；"
              "create 需 name+start_at+end_at（Unix 秒），"
              "block_level=block（禁发）/warn（发布需确认），"
              "scope 可选 {type: all/project/env/server/service, ids: []}，"
              "repeat_yearly 表示每年重复",
              {"action": str_req, "window_id": num_opt, "name": str_opt,
               "block_level": str_opt, "start_at": num_opt,
               "end_at": num_opt, "repeat_yearly": bool_opt,
               "scope": {"type": "object"}, "project": str_opt},
              "write", _manage_windows),
        _spec("manage_canary",
              "管理灰度策略。action=list/create/update/delete/set_default；"
              "steps 形如 [{batch:1, servers:1, observe_minutes:10, "
              "auto_advance:false}]（servers 可为 all）；"
              "set_default 将该策略设为项目默认",
              {"action": str_req, "project": str_opt, "config_id": num_opt,
               "name": str_opt,
               "steps": {"type": "array", "items": {"type": "object"}},
               "is_default": bool_opt},
              "write", _manage_canary),
        _spec("canary_decision",
              "对等待中的灰度任务提交决策：action=continue 继续/stop 停止/"
              "rollback 回滚首台（需提供发布任务 task_id）",
              {"task_id": num, "action": str_req}, "write", _canary_decision),
        _spec("set_alert_status",
              "告警闭环：action=resolve 标记解决/reopen 重开已解决告警"
              "（确认告警请用 ack_alert）",
              {"alert_id": num, "action": str_req, "note": str_opt},
              "write", _set_alert_status),
        _spec("manage_alert_rules",
              "管理告警规则（与 Web 端同等效力）。action=list/create/update/"
              "delete/toggle；metric_type=metric(cpu/mem/disk/load)/status("
              "service_down/task_failed)/log_keyword(逗号分隔关键词)/"
              "custom_monitor(到期监控)；scope={type:all/project/env/server/"
              "service, ids:[实体id]}（指标规则禁 service 作用域、状态/日志"
              "规则禁 server 作用域）；severity=P0-P3；channels 为数组"
              "（email/wecom/webhook/sms_aliyun/feishu/dingtalk）；"
              "update 为局部更新（未提供的字段保持原值）",
              {"action": str_req, "rule_id": num_opt, "name": str_opt,
               "metric_type": str_opt, "metric": str_opt, "compare": str_opt,
               "threshold": {"type": "number"}, "duration_seconds": num_opt,
               "scope": {"type": "object"}, "severity": str_opt,
               "channels": {"type": "array"},
               "escalation_interval": num_opt, "exclude_keyword": str_opt,
               "enabled": bool_opt, "remark": str_opt},
              "write", _manage_alert_rules),
        _spec("manage_silences",
              "管理告警静默窗口（与 Web 端同等效力）：命中窗口的告警不发送"
              "通知，窗口过期后未通知的 firing 告警会补发。action=list/"
              "create/update/delete；type=one_time(epoch 秒 start_at/end_at)/"
              "recurring(HH:MM + weekdays)；scope={rules:[规则id], "
              "envs:[环境id], servers:[服务器id]}，三列表全空=全局静默",
              {"action": str_req, "silence_id": num_opt, "name": str_opt,
               "type": str_opt, "start_at": str_opt, "end_at": str_opt,
               "weekdays": {"type": "array"}, "scope": {"type": "object"},
               "enabled": bool_opt, "remark": str_opt},
              "write", _manage_silences),
        _spec("manage_log_storage",
              "日志存储治理（仅 admin，与 Web 日志中心同等效力）：action=status"
              "（存储概况：日志行数/DB 占用/目录大小）/ settings（调整保留期"
              " log_retention_days/log_archive_days，1~365，不传参时查当前值）/"
              " cleanup（立即按保留策略清理过期日志并回收空间，与每日自动维护"
              "同一逻辑）",
              {"action": str_req, "log_retention_days": num_opt,
               "log_archive_days": num_opt},
              "write", _manage_log_storage),
        _spec("manage_dependencies",
              "管理服务依赖关系（operator/admin，与 Web 拓扑页同等效力）："
              "source 依赖 target（发布顺序上 target 先发布）。action=list/"
              "create/delete；create 含环检测（拒绝成环）且服务名必须在项目内"
              "存在（悬空拒绝）；dep_type=manual/dubbo。配合 service_topology "
              "metric=impact/publish_order 查询影响面与发布顺序",
              {"action": str_req, "project": str_opt,
               "dependency_id": num_opt, "source": str_opt, "target": str_opt,
               "dep_type": str_opt},
              "write", _manage_dependencies),
_spec("manage_monitors",
              "到期监控管理（operator/admin，与 Web 到期监控页同等效力）："
              "证书/域名/云续费/license/自定义到期项。action=list/create/"
              "update/delete/toggle/check/whois/config；create 时 type="
              "ssl/cloud_renewal/license/custom/domain（domain 无到期时间则"
              "自动 WHOIS；非 ssl/domain 需给 expiry_at）；check 手动探测"
              "（ssl 探测证书剩余天数/domain WHOIS 刷新）；whois 临时查询"
              "域名到期；config 全局默认值（写仅 admin）",
              {"action": str_req, "monitor_id": num_opt, "type": str_opt,
               "name": str_opt, "target": str_opt, "port": num_opt,
               "expiry_at": num_opt, "warn_days": num_opt,
               "severity": str_opt, "channels": str_opt, "owner": str_opt,
               "renewal_status": str_opt, "default_warn_days": num_opt,
               "default_severity": str_opt, "default_channels": str_opt,
               "ssl_check_interval": num_opt},
              "write", _manage_monitors),
_spec("refresh_monitor",
              "立即触发一轮监控采集刷新（operator/admin，与 Web /monitor/"
              "refresh 同等效力）：觉得数据过时、刚改完配置想看最新状态时先刷"
              "一轮，稍后配合 service_status/monitor_metrics 查看",
              {}, "write", _refresh_monitor),
_spec("manage_config_backup",
              "平台配置导入/导出（仅 admin，与 Web /config/export|import "
              "同等效力）：action=export 把 DB 配置全量导出为 YAML 到 "
              "data/export/config/（备份/迁移）；action=import 从 config/ 目录"
              "逐文件回灌 DB（逐文件结果见 results）",
              {"action": str_req}, "write", _manage_config_backup),
_spec("send_digest",
              "立即聚合并推送一次巡检日报（服务健康/告警/近24h发布/待审审批单"
              "/定时计划/临近到期监控）到已配置的通知渠道；渠道未配置时提示先配置",
              {}, "write", _send_digest),
_spec("live_status",
              "实时推送通道体检（只读）：当前 SSE 订阅者数、累计发布/丢弃事件数、"
              "已开主题，以及监控采集器/告警引擎这两个生产者是否在跑；用于回答"
              "「界面数据为什么不即时/实时通道是否正常」",
              {}, "read", _live_status),
    ]


def _role_rank(role: str) -> int:
    return {"admin": 3, "operator": 2, "viewer": 1}.get(role, 1)


TOOLS = {t["name"]: t for t in build_tools()}


def visible_tools(role: str) -> list:
    """按角色过滤后的工具（viewer=read；operator/admin=read+write）"""
    return [t for t in TOOLS.values()
            if t["permission"] == "read" or _role_rank(role) >= 2]


def _schema_out(spec: dict) -> dict:
    """深拷贝 schema 并剥除非法标记：required 在 JSON Schema 中只能是数组，
    工具定义里内嵌的布尔 required（必填标记）与顶层 required 数组一并移除，
    必填校验统一由工具侧执行；DeepSeek 等网关按严格 Schema 校验，残留会报 400"""
    out = {}
    for k, v in spec.items():
        if k == "required":
            continue
        if isinstance(v, dict):
            out[k] = _schema_out(v)
        elif isinstance(v, list):
            out[k] = [_schema_out(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def tool_schemas(role: str) -> list:
    """OpenAI function-calling schema（角色过滤后，不含布尔 required 标记）"""
    out = []
    for t in visible_tools(role):
        params = _schema_out(t["parameters"])
        out.append({"type": "function",
                    "function": {"name": t["name"],
                                 "description": t["description"],
                                 "parameters": params}})
    return out


def execute_tool(role: str, name: str, args: dict, ctx) -> str:
    """执行工具并返回文本结果（异常统一转为可读错误串，不抛给编排层）"""
    tool = TOOLS.get(name)
    if not tool:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    if tool["permission"] == "write" and _role_rank(role) < 2:
        return json.dumps({"error": f"工具 {name} 需要 operator/admin 权限"},
                          ensure_ascii=False)
    try:
        result = tool["fn"](ctx, **(args or {}))
        return json.dumps(result, ensure_ascii=False, default=str)
    except ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
    except Exception as e:  # 工具自身缺陷兜底，避免打断编排
        return json.dumps({"error": f"工具执行异常: {e}"}, ensure_ascii=False)
