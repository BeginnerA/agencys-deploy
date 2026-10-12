"""Phase 2 决策点 AI 化：灰度决策 / 失败归因与回滚建议 / 告警归因 / 发布预检

设计原则：
    1. 规则引擎先行——健康状态、错误日志、活跃告警、历史发布都是可判定的硬证据，
       规则给出结论与置信度，LLM 只做语言组织与补充推理；
    2. LLM 不可用时自动降级为纯规则输出，功能不中断；
    3. 每条建议写入 agent_advice 表（scope+ref_id 定位），并回填证据（evidence），
       供 Web 决策弹窗/告警卡片/智能助手引用。
"""
import json
import os
import re
import time
from pathlib import Path

from webapp import db as dbm
from webapp import events

from . import llm as llm_mod
from . import memory as memory_mod

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 错误关键字 → 根因提示（按序匹配，日志文本已小写化）
_ERROR_HINTS = [
    (["classnotfound", "nosuchmethod", "noclassdeffound"],
     "代码/依赖缺失（ClassNotFoundException 类错误），检查构建产物与运行依赖"),
    (["outofmemory", "oomkilled", "heap space"],
     "内存溢出（OOM/OOMKilled），检查 JVM 堆参数与容器内存限制"),
    (["disk space", "no space left"],
     "磁盘空间不足，检查宿主机与日志目录"),
    (["connection refused", "connect timed out", "connect timeout"],
     "连接被拒绝/超时，检查下游服务、端口与防火墙"),
    (["mysql", "数据库连接", "sqlstate"],
     "数据库连接或 SQL 异常，检查 MySQL 连接池与慢 SQL"),
    (["redis", "jedis", "lettuce"],
     "Redis 连接异常，检查缓存服务可用性"),
    (["nacos", "registration", "deregister"],
     "Nacos 注册/发现异常，检查 Nacos 服务与配置"),
    (["snailjob"], "SnailJob 调度异常"),
    (["permission denied"], "权限不足（文件/端口/系统调用）"),
    (["dubbo"], "Dubbo RPC 异常"),
    (["timeout"], "请求/任务超时"),
]

_ADVICE_TTL = 6 * 3600  # 同 scope+ref 的建议 6 小时内不重复生成（避免调度器轰炸）


# ---------- 建议存取 ----------

# 建议生命周期状态：pending 待处理 / adopted 已采纳（自动标记，不可逆转）/
# dismissed 已忽略（仅 pending 可忽略，可重开回 pending）
ADVICE_STATUSES = ("pending", "adopted", "dismissed")

def save_advice(db_path: str, scope: str, ref_id: int, title: str,
                body: str, evidence: dict = None, model: str = "",
                actor: dict = None) -> int:
    actor = actor or {}
    advice_id = dbm.create_record(db_path, "agent_advice", dict(
        scope=scope, ref_id=int(ref_id or 0), title=title, body=body,
        evidence=evidence or {}, model=model,
        created_at=int(time.time()),
        created_by=int(actor.get("id", 0)),
        created_by_name=actor.get("username", ""),
        status="pending"))
    # 后台 advisor 持续产出新建议，前端角标/列表靠本事件即时跟进（代替 15s 盲轮询）
    events.publish("agent", {"action": "advice_new", "advice_id": advice_id,
                             "scope": scope})
    return advice_id


def latest_advice(db_path: str, scope: str, ref_id: int, ttl: int = _ADVICE_TTL):
    """取最近一条建议；终态（dismissed/adopted）永久阻塞重新生成，
    pending 状态 ttl 秒内有记录则复用（未过期），否则返回 None。"""
    rows = dbm.list_records(db_path, "agent_advice",
                            "scope=? AND ref_id=?", (scope, int(ref_id or 0)),
                            "id DESC")
    if not rows:
        return None
    row = rows[0]
    # 终态建议永久阻塞同 scope+ref 的重新生成（忽略=不想再看到，采纳=已处理）
    if row.get("status") in ("dismissed", "adopted"):
        return row
    now = time.time()
    if row["created_at"] and (now - row["created_at"]) > ttl:
        return None
    return row


def list_advice(db_path: str, scope: str = "", ref_id: int = 0,
                limit: int = 30, status: str = "") -> list:
    conds, params = [], []
    if scope:
        conds.append("scope=?")
        params.append(scope)
    if ref_id:
        conds.append("ref_id=?")
        params.append(int(ref_id))
    if status:
        conds.append("status=?")
        params.append(status)
    where = " AND ".join(conds)
    rows = dbm.list_records(db_path, "agent_advice", where, tuple(params),
                            "id DESC")
    return rows[:max(1, min(limit, 100))]


def set_advice_status(db_path: str, advice_id: int, status: str,
                      actor: dict = None, action_ref: str = "") -> dict:
    """流转建议状态：pending→adopted（采纳自动标记）/ pending→dismissed（人工忽略）
    / dismissed→pending（重开）。adopted 为终态不可再流转。

    返回 {"ok": True, "status": ...}；建议不存在抛 ValueError，
    非法状态或非法流转抛 ValueError（路由层转 4xx）。
    """
    if status not in ADVICE_STATUSES:
        raise ValueError(f"非法建议状态: {status}")
    advice = dbm.get_record(db_path, "agent_advice", advice_id)
    if not advice:
        raise ValueError(f"建议不存在: #{advice_id}")
    cur = advice.get("status") or "pending"
    if cur == "adopted":
        raise ValueError("该建议已采纳，状态不可再变更")
    if status == "adopted" and cur != "pending":
        raise ValueError("仅待处理建议可标记为已采纳")
    if status == "dismissed" and cur != "pending":
        raise ValueError("仅待处理建议可忽略")
    if status == "pending" and cur != "dismissed":
        raise ValueError("仅已忽略建议可重开")
    fields = {"status": status, "status_at": int(time.time()),
              "status_by": (actor or {}).get("username", "")}
    if action_ref:
        fields["action_ref"] = action_ref
    dbm.update_record(db_path, "agent_advice", advice_id, fields)
    return {"ok": True, "status": status}


def delete_advice(db_path: str, advice_id: int) -> bool:
    """删除建议：若为 pending 且有有效触发源（scope+ref_id），转为忽略墓碑记录（永久阻塞重新生成）；
    已终态记录则物理删除（释放存储）。
    """
    rec = dbm.get_record(db_path, "agent_advice", advice_id)
    if not rec:
        return False
    cur_status = rec.get("status") or "pending"
    # 已终态（dismissed/adopted）直接物理删除，不会被重新触发
    if cur_status in ("dismissed", "adopted"):
        return dbm.delete_record(db_path, "agent_advice", advice_id)
    # pending 有触发源 → 软删为 dismissed 墓碑，保留 scope+ref_id 阻塞重新生成
    if rec.get("scope") and rec.get("ref_id"):
        dbm.update_record(db_path, "agent_advice", advice_id, {
            "status": "dismissed", "status_at": _now(),
            "status_by": "delete", "title": f"[已删除] {rec.get('title', '')}",
            "body": "", "evidence": {},
        })
        return True
    # 无触发源（如 precheck ref_id=0）可以安全物理删除
    return dbm.delete_record(db_path, "agent_advice", advice_id)


# ---------- 上下文采集 ----------

def _now() -> int:
    return int(time.time())


def _recent_log_errors(db_path: str, service: str = "", env: str = "",
                       minutes: int = 30, limit: int = 10) -> list:
    """近 N 分钟该服务/环境日志中的错误行（LIKE 匹配 ERROR/Exception 等）"""
    from_ts = _now() - minutes * 60
    conds = ["ts>=?", "content LIKE ?"]
    params = [from_ts, "%ERROR%"]
    conds.append("(content LIKE ? OR content LIKE ? OR content LIKE ?)")
    params += ["%Exception%", "%exception%", "%错误%"]
    if service:
        conds.append("service=?")
        params.append(service)
    if env:
        conds.append("env=?")
        params.append(env)
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, service, container, substr(content,1,300) AS content "
            "FROM log_entries WHERE " + " AND ".join(conds) +
            " ORDER BY ts DESC LIMIT ?", params + [limit]).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _task_log_tail(task: dict, chars: int = 3000) -> str:
    """任务日志尾部 + 关键行（发布失败归因用）"""
    log_file = task.get("log_file") or ""
    if not log_file:
        return ""
    try:
        p = Path(log_file)
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    tail = text[-chars:]
    # 关键行：失败/异常/错误/回滚 相关（最多 12 条）
    keys = ("失败", "异常", "错误", "Exception", "ERROR", "回滚", "拒绝",
            "denied", "refused", "timeout", "超时")
    picks = [ln.strip() for ln in tail.splitlines()
             if ln.strip() and any(k in ln for k in keys)][-12:]
    return "\n".join(picks) if picks else tail[-600:]


def _service_monitor_state(db_path: str, project_id: int, env: str,
                           service: str) -> dict:
    """监控缓存中的服务状态（无缓存/未初始化返回空 dict）"""
    try:
        from webapp.monitor import get_monitor
        mon = get_monitor()
        if not mon:
            return {}
        ov = mon.get_overview(project_id, env) or {}
        svc = (ov.get("services") or {}).get(service)
        return dict(svc) if svc else {}
    except Exception:
        return {}


def _service_alerts(db_path: str, service: str = "", env: str = "",
                    hours: int = 24, limit: int = 10) -> list:
    """该服务/环境近期未解决告警（target_info 内含 service/env 标记）"""
    from_ts = (_now() - hours * 3600) * 1
    conds = ["status != 'resolved'", "first_seen_at>=?"]
    params = [from_ts]
    if service:
        conds.append("(target_info LIKE ? OR subject LIKE ?)")
        params += [f"%{service}%", f"%{service}%"]
    if env:
        conds.append("target_info LIKE ?")
        params.append(f'%"env":"{env}"%')
    conn = dbm.get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, severity, subject, description, status, "
            "first_seen_at, last_seen_at FROM alerts WHERE " +
            " AND ".join(conds) + " ORDER BY id DESC LIMIT ?",
            params + [limit]).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _recent_service_tasks(db_path: str, service: str, hours: int = 24,
                          limit: int = 8) -> list:
    rows = dbm.list_records(db_path, "tasks", "name=?", (service,),
                            "id DESC")
    out = []
    for t in rows:
        if t.get("finished_at") and (time.time() - t["finished_at"]) <= hours * 3600:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _hint_error(text: str) -> str:
    low = (text or "").lower()
    for keys, hint in _ERROR_HINTS:
        if any(k in low for k in keys):
            return hint
    return ""


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, OSError, ValueError):
        return "-"


def _llm_polish(db_path: str, scene: str, facts: str) -> str:
    """LLM 润色（可选）：基于事实输出简洁建议；任何失败返回空串降级规则文本"""
    system = ("你是智能运维平台内置的决策分析助手。基于给定的事实证据，用中文输出"
              "简明结论与建议（150 字以内，分点）。只依据事实，不要臆测。")
    try:
        # 网关未配置时 simple_chat 抛 LlmConfigError，一并降级为纯规则输出
        return llm_mod.simple_chat(
            db_path, system=system,
            user=f"场景: {scene}\n事实证据:\n{facts}")
    except llm_mod.LlmError:
        return ""


def _model_name(db_path: str) -> str:
    from . import config as agent_config
    s = agent_config.get_llm_settings(db_path)
    return s.get("model", "") or "rule-based"


# ---------- 决策点 1/4：发布失败归因 + 回滚建议 ----------

def analyze_failure(db_path: str, task_id: int, actor: dict = None,
                    force: bool = False) -> dict:
    """发布/回滚任务失败后的归因与处置建议（scope=task_failed, ref_id=task_id）

    返回建议记录 dict（含建议动作与理由要点）；同任务短期内已建议则不重复生成。
    """
    if not force:
        existing = latest_advice(db_path, "task_failed", task_id)
        if existing:
            return existing
    task = dbm.get_task(db_path, task_id)
    if not task:
        raise ValueError(f"任务不存在: #{task_id}")
    service = task.get("name") or ""
    env = task.get("env") or ""
    version = task.get("version") or ""
    prev = task.get("previous_version") or ""
    log_tail = _task_log_tail(task)
    # 1) 日志根因提示
    hint = _hint_error(log_tail)
    # 2) 监控状态与活跃告警（回滚决策硬证据）
    svc_state = _service_monitor_state(db_path, task.get("project_id") or 0,
                                       env, service)
    alerts = _service_alerts(db_path, service, env, hours=6, limit=5)
    # 3) 规则决策
    reasons = []
    suggestion = "重试或人工排查"          # 默认建议
    if log_tail:
        reasons.append("任务日志存在错误痕迹：" + (hint or "详见日志尾部"))
    if svc_state and svc_state.get("status") == "online":
        reasons.append(f"监控显示服务当前 online（容器存活，探测正常）")
    elif svc_state and svc_state.get("status") in ("probe_failed", "stopped"):
        reasons.append(f"监控显示服务当前 {svc_state.get('status')}，存在异常")
    for a in alerts[:2]:
        reasons.append(f"活跃告警 {a['severity']}「{a.get('subject')}」")
    has_rollback_target = bool(prev)
    if prev:
        suggestion = (f"若容器/流量已异常：优先回滚到 {prev}（全量回滚通道）。"
                      "若失败发生在滚动期且平台已自动回滚故障服务器，请先核对大屏任务详情。")
    else:
        suggestion = ("本次为首次发布（无回滚版本），失败后容器保持新版本或未修改："
                      "请人工排查日志根因后修复镜像重试。")
    # 4) 相似案例召回（Phase 4 记忆）
    cases = memory_mod.search_cases(
        db_path, keyword=hint or service, service=service or None, limit=3)
    case_lines = "\n".join(memory_mod.summarize_case(c) for c in cases) if cases else "（无相似案例）"
    # 5) 组装
    evidence = {
        "task": {"id": task["id"], "name": service, "env": env,
                 "version": version, "previous_version": prev,
                 "status": task["status"]},
        "monitor": svc_state or None,
        "alerts": alerts[:3],
        "log_tail": log_tail[:1500],
        "error_hint": hint,
        "related_cases": cases[:3],
    }
    facts = (f"任务: {service} env={env} version={version} prev={prev}\n"
             f"日志错误痕迹: {hint or '未命中已知模式'}\n"
             f"监控状态: {svc_state or '无监控数据'}\n"
             f"告警: {[a.get('subject') for a in alerts[:3]] or '无'}\n"
             f"相似案例: {case_lines}")
    polish = _llm_polish(db_path, "发布任务失败归因", facts)
    body = "## 建议动作\n" + suggestion + "\n\n## 判断依据\n" + \
        "\n".join("- " + r for r in reasons)
    if polish:
        body += "\n\n## 分析\n" + polish
    aid = save_advice(db_path, "task_failed", task_id,
                      f"任务失败归因：{service} {env} {version or ''}",
                      body, evidence=evidence,
                      model=_model_name(db_path), actor=actor)
    return dbm.get_record(db_path, "agent_advice", aid)


# ---------- 决策点 2/4：灰度（canary）继续/停止/回滚建议 ----------

def advise_canary(db_path: str, task_id: int, file_data: dict = None,
                  actor: dict = None, force: bool = False) -> dict:
    """灰度首台发布后的决策建议（scope=canary, ref_id=task_id）

    决策文件（canary_decision_<task_id>.json）已 waiting 时由调度器调用；
    规则硬阈值优先，结论为 continue/stop/rollback 之一 + 置信度。
    """
    if not force:
        existing = latest_advice(db_path, "canary", task_id, ttl=300)
        if existing:
            return existing
    task = dbm.get_task(db_path, task_id)
    if not task:
        raise ValueError(f"任务不存在: #{task_id}")
    service = task.get("name") or ""
    env = task.get("env") or ""
    version = task.get("version") or ""
    prev = task.get("previous_version") or ""
    # 采集首台健康/日志/告警证据
    svc_state = _service_monitor_state(db_path, task.get("project_id") or 0,
                                       env, service)
    err_lines = _recent_log_errors(db_path, service, env, minutes=30, limit=8)
    alerts = _service_alerts(db_path, service, env, hours=6, limit=8)
    # 规则决策（顺序即优先级）
    decision = "continue"
    confidence = 0.5
    reasons = []
    if svc_state and svc_state.get("status") == "probe_failed":
        decision = "rollback"
        confidence = 0.9
        reasons.append(f"首台健康探测失败（状态 probe_failed），继续放量风险高")
    elif svc_state and svc_state.get("status") != "online":
        decision = "rollback"
        confidence = 0.8
        reasons.append(f"监控显示首台服务状态为 {svc_state.get('status')}（非 online）")
    elif err_lines:
        decision = "stop"
        confidence = 0.75
        reasons.append(f"近 30 分钟日志出现 {len(err_lines)} 条错误/异常记录")
    elif any(a["severity"] in ("P0", "P1") for a in alerts):
        decision = "stop"
        confidence = 0.7
        reasons.append("存在 P0/P1 级活跃告警，建议暂停观察")
    else:
        if svc_state and svc_state.get("status") == "online":
            confidence = 0.7
            reasons.append("首台服务状态 online 且健康检查通过")
        reasons.append("无错误日志与高级别告警")
        reasons.append(f"业务侧请人工抽验后决定（灰度决策建议仅供参考）")
    if prev and decision == "rollback":
        reasons.append(f"已提供回滚目标 {prev}")
    elif not prev and decision == "rollback":
        # 无回滚版本的失败首台只能 stop，回滚无意义
        decision = "stop"
        confidence = 0.8
        reasons.append("无回滚版本（首次发版），失败时只能停止放量，容器保持新版本待排查")
    # 相似案例
    cases = memory_mod.search_cases(db_path, keyword=service,
                                    service=service or None, limit=2)
    evidence = {
        "task": {"id": task["id"], "name": service, "env": env,
                 "version": version, "previous_version": prev},
        "decision": decision, "confidence": confidence,
        "monitor": svc_state or None,
        "log_errors_30m": [{"ts": e["ts"], "container": e.get("container", ""),
                            "content": e["content"]} for e in err_lines[:3]],
        "alerts": [{"severity": a["severity"], "subject": a.get("subject")}
                   for a in alerts[:5]],
        "related_cases": cases[:2],
    }
    facts = (f"灰度任务: {service} env={env} version={version} prev={prev or '无'}\n"
             f"首台监控: {svc_state or '无监控数据'}\n"
             f"近30分钟错误日志: {len(err_lines)} 条\n"
             f"活跃告警: {[(a['severity'], a.get('subject')) for a in alerts[:5]] or '无'}\n"
             f"规则初判: {decision} (置信度 {confidence})")
    polish = _llm_polish(db_path, "金丝雀灰度决策", facts)
    reason_text = "\n".join("- " + r for r in reasons)
    body = (f"## 建议: {decision}（置信度 {confidence:.0%}）\n\n"
            f"## 理由\n{reason_text}")
    if polish:
        body += "\n\n## 补充分析\n" + polish
    aid = save_advice(db_path, "canary", task_id,
                      f"灰度决策建议：{service} {env} {version} → {decision}",
                      body, evidence=evidence,
                      model=_model_name(db_path), actor=actor)
    return dbm.get_record(db_path, "agent_advice", aid)


# ---------- 决策点 3/4：告警归因 ----------

def analyze_alert(db_path: str, alert_id: int, actor: dict = None,
                  force: bool = False) -> dict:
    """告警归因与处置建议（scope=alert, ref_id=alert_id）"""
    if not force:
        existing = latest_advice(db_path, "alert", alert_id, ttl=600)
        if existing:
            return existing
    alert = dbm.get_record(db_path, "alerts", alert_id)
    if not alert:
        raise ValueError(f"告警不存在: #{alert_id}")
    # db 层对 JSON 字段已反序列化（target_info 为 dict），兼容旧库遗留的字符串
    target = alert.get("target_info") or {}
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except (json.JSONDecodeError, TypeError):
            target = {}
    if not isinstance(target, dict):
        target = {}
    service = target.get("service", "") or ""
    env = target.get("env", "")
    # 背景采集
    hint = _hint_error(f"{alert.get('subject')} {alert.get('description')}")
    log_lines = _recent_log_errors(db_path, service, env,
                                   minutes=60, limit=6)
    tasks = _recent_service_tasks(db_path, service, hours=24, limit=5)
    cases = memory_mod.search_cases(db_path, keyword=hint or alert.get("subject", ""),
                                    service=service or None, limit=3)
    # 规则结论
    bullets = []
    if hint:
        bullets.append(f"告警内容命中已知错误模式：{hint}")
    if tasks:
        recent = [f"{t.get('status')} {t.get('version', '')}@{_fmt_ts(t.get('finished_at'))}"
                  for t in tasks]
        bullets.append(f"近 24h 发布记录：{', '.join(recent) or '无'}")
        failed_near = [t for t in tasks if t.get("status") == "failed"]
        if failed_near:
            bullets.append(f"最近一次发布失败（{_fmt_ts(failed_near[0].get('finished_at'))}），"
                           "告警可能与发布相关，建议先检查该次发布")
    if log_lines:
        bullets.append(f"近 1h 日志有 {len(log_lines)} 条错误/异常记录")
    if cases:
        bullets.append(f"命中 {len(cases)} 条历史案例，可参考处置")
    if not bullets:
        bullets.append("未命中已知模式，请结合告警详情人工研判")
    evidence = {
        "alert": {"id": alert["id"], "severity": alert.get("severity"),
                  "subject": alert.get("subject"),
                  "status": alert.get("status")},
        "target": target,
        "log_errors_1h": [{"ts": e["ts"], "container": e.get("container", ""),
                           "content": e["content"]} for e in log_lines[:3]],
        "tasks_24h": [{k: t.get(k) for k in ("id", "status", "version",
                                             "finished_at")} for t in tasks],
        "related_cases": cases[:3],
    }
    facts = (f"告警: [{alert.get('severity')}] {alert.get('subject')}\n"
             f"目标: service={service or '-'} env={env or '-'}\n"
             f"错误模式命中: {hint or '无'}\n"
             f"日志错误: {len(log_lines)} 条\n"
             f"近期发布: {[t.get('status') for t in tasks] or '无'}\n"
             f"相似案例: {memory_mod.summarize_case(cases[0]) if cases else '无'}")
    polish = _llm_polish(db_path, "告警归因分析", facts)
    body = "## 归因要点\n" + "\n".join("- " + b for b in bullets)
    if polish:
        body += "\n\n## 分析\n" + polish
    body += "\n\n> 建议核对告警规则与发布窗口后再处置；如需沉淀案例可一键保存。"
    aid = save_advice(db_path, "alert", alert_id,
                      f"告警归因：{alert.get('subject')}",
                      body, evidence=evidence,
                      model=_model_name(db_path), actor=actor)
    return dbm.get_record(db_path, "agent_advice", aid)


# ---------- 决策点 4/4：发布前预检 ----------

def run_precheck(db_path: str, target: str, env: str = "",
                 version: str = "", canary: bool = False,
                 actor: dict = None) -> dict:
    """发布前预检（scope=precheck, ref_id=0）：返回检查清单与结论

    target 为服务名或流水线名（先服务后流水线，与 executor 同语义）。
    """
    from .executor import VERSION_RE
    checks = []
    def add(item: str, status: str, detail: str):
        checks.append({"item": item, "status": status, "detail": detail})

    svc = pl = None
    svc = dbm.list_records(db_path, "services", "name=?", (target,))
    if not svc:
        pl = dbm.list_records(db_path, "pipelines", "name=?", (target,))
    if not svc and not pl:
        raise ValueError(f"未找到服务或流水线「{target}」")
    rec = (svc or pl)[0]
    project_id = rec["project_id"]
    proj = dbm.get_record(db_path, "projects", project_id)
    kind = "服务" if svc else "流水线"
    add(f"目标 {kind}「{target}」存在且已启用",
        "ok" if rec.get("enabled", 1) else "block",
        rec.get("enabled", 1) and "ok" or "记录已停用")
    env_obj = dbm.get_env_by_name(db_path, project_id, env) if env else None
    if not env:
        add("目标环境 env", "block", "缺少 env（如 prod/test）")
    elif not env_obj:
        add(f"环境「{env}」", "block", "该项目下不存在该环境")
    else:
        servers = dbm.list_env_servers(db_path, env_obj["id"])
        add(f"环境「{env}」绑定服务器",
            "ok" if servers else "block",
            f"{len(servers)} 台"
            + ("，顺序: " + ", ".join(s["host"] for s in servers[:6])
               + (" 等" if len(servers) > 6 else "") if servers else "未绑定服务器无法发布"))
    if version:
        add("版本号格式 x.y.z", "ok" if VERSION_RE.match(version) else "block",
            version if VERSION_RE.match(version) else f"{version} 格式非法")
        # 同版本最近发布成功过 → 提示重复发布
        dups = [t for t in _recent_service_tasks(db_path, target, hours=24, limit=20)
                if t.get("version") == version and t.get("status") == "success"]
        if dups:
            add("版本重复发布", "warn",
                f"版本 {version} 近 24h 已成功发布 {len(dups)} 次（请确认是否覆盖同名镜像）")
    # 发布窗口
    from webapp.deploy_scheduler import check_release_window
    win = check_release_window(db_path, project_id)
    if win:
        add("发布窗口", "block" if win["block_level"] == "block" else "warn",
            f"{win['name']}（{win['block_level']}）")
    else:
        add("发布窗口", "ok", "当前无封网/告警窗口")
    # 最近失败发布
    failed_recent = [t for t in _recent_service_tasks(db_path, target, hours=12, limit=20)
                     if t.get("status") == "failed"]
    if failed_recent:
        add("近期发布健康度", "warn",
            f"近 12h 有 {len(failed_recent)} 次失败发布（最近: {_fmt_ts(failed_recent[0].get('finished_at'))}）")
    else:
        add("近期发布健康度", "ok", "近 12h 无失败发布")
    # 监控当前状态（带监控数据的服务）
    if svc:
        st = _service_monitor_state(db_path, project_id, env_obj["name"] if env_obj else "", target)
        if st:
            add("服务当前监控状态", "ok" if st.get("status") == "online" else "warn",
                f"{st.get('status')}（最近探测 {_fmt_ts(st.get('ts'))}）")
    # 结论
    blocks = [c for c in checks if c["status"] == "block"]
    warns = [c for c in checks if c["status"] == "warn"]
    passed = not blocks
    conclusion = (f"预检{'通过' if passed else '未通过'}："
                  f"{len(blocks)} 项阻断，{len(warns)} 项警告")
    title = (f"发布预检：{target} {env or ''} {version or ''} → "
             f"{'通过' if passed else '需处理'}")
    evidence = {"checks": checks, "passed": passed}
    facts = "\n".join(f"[{c['status']}] {c['item']}: {c['detail']}" for c in checks)
    polish = _llm_polish(db_path, "发布前预检", facts) if not passed else ""
    body = f"## 结论\n{conclusion}\n\n## 检查清单\n" + "\n".join(
        f"- [{c['status']}] {c['item']}：{c['detail']}" for c in checks)
    if polish:
        body += "\n\n## 分析\n" + polish
    aid = save_advice(db_path, "precheck", 0, title, body,
                      evidence=evidence, model=_model_name(db_path),
                      actor=actor)
    out = dbm.get_record(db_path, "agent_advice", aid)
    return dict(out, checks=checks, passed=passed)


# ---------- 案例沉淀辅助 ----------

def save_case_from_advice(db_path: str, advice_id: int,
                          actor: dict = None) -> int:
    """把一条建议（告警归因/失败归因）沉淀为案例草稿"""
    advice = dbm.get_record(db_path, "agent_advice", advice_id)
    if not advice:
        raise ValueError(f"建议不存在: #{advice_id}")
    ev = advice.get("evidence") or {}
    service = ""
    if advice["scope"] == "alert":
        service = (ev.get("target") or {}).get("service", "")
    elif advice["scope"] == "task_failed":
        service = (ev.get("task") or {}).get("name", "")
    tags = [advice["scope"], advice.get("model") or "rule-based"]
    return memory_mod.create_case(
        db_path,
        title=advice["title"],
        service=service or "",
        symptom=advice.get("body", "")[:800],
        root_cause="", action="", result="", lesson="",
        tags=tags, source="advice", advice_id=advice_id,
        user_id=int((actor or {}).get("id", 0)),
        username=(actor or {}).get("username", ""))


# ---------- 调度器联动 ----------

def canary_waiting_files() -> list:
    """列出 data/ 下处于 waiting 的灰度决策文件 [(task_id, data)]"""
    out = []
    try:
        for p in DATA_DIR.glob("canary_decision_*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("state") == "waiting":
                try:
                    out.append((int(data["task_id"]), data))
                except (KeyError, TypeError, ValueError):
                    continue
    except OSError:
        pass
    return out


def sweep(db_path: str, actor: dict = None) -> dict:
    """调度器单轮扫描：灰度建议 + 失败归因 + 告警归因 + 审批单状态同步

    全部失败静默（后台任务不因单点异常中断整轮）。
    """
    result = {"canary": 0, "failure": 0, "alert": 0, "sync": 0, "errors": []}
    actor = actor or {"id": 0, "username": "advisor"}
    try:
        from .executor import sync_executing_runs
        result["sync"] = sync_executing_runs(db_path)
    except Exception as e:
        result["errors"].append(f"sync: {e}")
    # 1) 灰度决策等待中的任务 → 生成建议
    for task_id, file_data in canary_waiting_files():
        try:
            if latest_advice(db_path, "canary", task_id, ttl=300):
                continue
            advise_canary(db_path, task_id, file_data=file_data, actor=actor)
            result["canary"] += 1
        except Exception as e:
            result["errors"].append(f"canary {task_id}: {e}")
    # 2) 近 10 分钟失败的发布任务 → 归因建议
    from_ts = _now() - 600
    recent = [t for t in dbm.list_records(db_path, "tasks", order="id DESC")[:100]
              if t.get("status") == "failed"
              and t.get("finished_at") and t["finished_at"] >= from_ts
              and t.get("type") in ("project", "pipeline")]
    for t in recent[:5]:
        try:
            # TTL 去重：同任务短期内已生成过归因建议则跳过
            if latest_advice(db_path, "task_failed", t["id"]):
                continue
            analyze_failure(db_path, t["id"], actor=actor)
            result["failure"] += 1
        except Exception as e:
            result["errors"].append(f"failure {t['id']}: {e}")
    # 3) 未解决的 P0/P1 告警 → 归因建议（节流由 latest_advice TTL 控制）
    try:
        conn = dbm.get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM alerts WHERE status != 'resolved' "
                "AND severity IN ('P0','P1') ORDER BY id DESC LIMIT 10").fetchall()
        finally:
            conn.close()
        for row in rows:
            try:
                if latest_advice(db_path, "alert", row["id"], ttl=900):
                    continue
                analyze_alert(db_path, row["id"], actor=actor)
                result["alert"] += 1
            except Exception as e:
                result["errors"].append(f"alert {row['id']}: {e}")
    except Exception as e:
        result["errors"].append(f"alert list: {e}")
    return result
