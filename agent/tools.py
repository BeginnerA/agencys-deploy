"""工具注册表：Agent 可调用的平台能力（read / write 两级权限）

设计约定：
    - 每个工具 = ToolSpec(name, description, params, permission, fn)
    - permission: read=全体角色；write=operator/admin（write 型动作全部落审计或
      agent_runs 审批单，绝不绕过既有通道）
    - 工具返回纯数据（list/dict/str），由 core 统一 JSON 序列化并截断
    - 敏感信息（SSH 私钥/密码/Nacos 口令）一律不出现在工具输出中
"""
import json
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
            "SELECT le.ts, le.env, le.service, le.container, "
            "substr(le.content,1,200) AS content FROM log_entries le "
            "WHERE " + " AND ".join(conds) + " ORDER BY le.ts DESC LIMIT ?",
            params + [max(1, min(limit, 100))]).fetchall()
        return [{"ts": _fmt_ts(r["ts"]), "env": r["env"], "service": r["service"],
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
            "hint": "结果按时间倒序；可用 service/env 收窄"}


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


def _advice_list(ctx, scope="", ref_id=0, limit=10, **kw) -> dict:
    if scope not in ("", "canary", "task_failed", "alert", "precheck"):
        raise ToolError("scope 仅支持 canary/task_failed/alert/precheck")
    rows = advisor_mod.list_advice(ctx.db_path, scope=scope,
                                   ref_id=int(ref_id or 0),
                                   limit=max(1, min(limit, 50)))
    return {"advice": [{"id": a["id"], "scope": a["scope"],
                        "ref_id": a["ref_id"], "title": a["title"],
                        "body": (a.get("body") or "")[:600],
                        "model": a.get("model", ""),
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
            "hint": "审批单已生成，等待人工审批（Web 端操作，Agent 无法自行批准）"}


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
                 result=None, lesson=None, title=None, tags=None, **kw) -> dict:
    try:
        case_id = int(case_id)
    except (TypeError, ValueError):
        raise ToolError("缺少 case_id（整数）")
    fields = {}
    for k, v in (("root_cause", root_cause), ("action", action),
                 ("result", result), ("lesson", lesson), ("title", title)):
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
        _spec("advice_list", "查看 AI 建议列表（scope: canary/task_failed/alert/precheck）",
              {"scope": str_opt, "ref_id": num_opt, "limit": num_opt},
              "read", _advice_list),
        _spec("pending_approvals", "查看待审批的 Agent 发布计划（人工审批入口）",
              {"limit": num_opt}, "read", _pending_approvals),
        _spec("run_detail", "查看审批单（计划/状态/审批人/任务关联）",
              {"run_id": num}, "read", _run_detail),
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
              "生成后必须由人工在 Web 审批才执行",
              {"op": str_req, "target": str_req, "env": str_req,
               "version": str_opt, "previous_version": str_opt,
               "canary": bool_opt, "skip_build": bool_opt,
               "auto_version": bool_opt, "no_git_tag": bool_opt,
               "intent": str_opt}, "write", _plan_operation),
        _spec("case_save", "把一条 AI 建议（告警归因/失败归因）沉淀为案例草稿",
              {"advice_id": num}, "write", _case_save),
        _spec("case_update", "补全/修正案例内容（root_cause/action/result/lesson/title/tags）",
              {"case_id": num, "root_cause": str_opt, "action": str_opt,
               "result": str_opt, "lesson": str_opt, "title": str_opt,
               "tags": {"type": "array", "items": {"type": "string"}}},
              "write", _case_update),
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
