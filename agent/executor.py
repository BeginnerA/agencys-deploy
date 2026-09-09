"""Phase 3 发布执行 Agent：操作计划生成 → 人工审批 → 复用现有任务通道执行 → 状态同步

关键设计：Agent 只负责“读懂意图 + 生成结构化计划”，真正的发布仍走平台既有的
tasks 通道（task_manager → deploy.py / deploy_pipeline.py 子进程），因此灰度文件
协议、回滚兜底、SnailJob 恢复等全部现有机制原样生效，Agent 层零绕过。

状态机：awaiting_approval → executing → success / failed（拒绝为 rejected）
"""
import json
import re
import threading
import time

from webapp import db as dbm
from webapp.deploy_scheduler import check_release_window
from webapp import task_manager

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# 审批后后台等待任务终态的时长上限（任务实际执行通常远小于该值）
_WATCH_TIMEOUT = 6 * 3600
_WATCH_INTERVAL = 5.0

RUN_STATUSES = ("awaiting_approval", "rejected", "executing",
                "success", "failed")


class ExecutorError(Exception):
    """计划/审批业务错误（参数非法、状态冲突等），message 面向用户"""


# ---------- 工具函数 ----------

def _now() -> int:
    return int(time.time())


def _find_project_by_service(db_path: str, service_name: str):
    rows = dbm.list_records(db_path, "services", "name=?", (service_name,))
    return rows[0] if rows else None


def _find_pipeline(db_path: str, pipeline_name: str):
    rows = dbm.list_records(db_path, "pipelines", "name=?", (pipeline_name,))
    return rows[0] if rows else None


def _env_servers(db_path: str, project_id: int, env: str) -> list:
    env_obj = dbm.get_env_by_name(db_path, project_id, env)
    if not env_obj:
        return []
    return dbm.list_env_servers(db_path, env_obj["id"])


def _steps_text(op: str, env: str, svc_names: list, servers: list,
                canary: bool) -> list:
    """计划的执行步骤中文文案（与 deploy.py / deploy_pipeline.py 实际流程对齐）"""
    host_text = f"{len(servers)} 台服务器" if servers else "0 台服务器（环境未绑定服务器!）"
    if op == "restart":
        return ["并发对每台服务器执行 docker restart",
                f"逐台健康检查（health_url，最多约 60s）"]
    base = ["Maven 构建并推送镜像（skip_build 时跳过）",
            "SnailJob 任务组暂停（配置了 snailjob_group 时）",
            "镜像预传输（直传模式并行 save/upload/load）"]
    if op == "deploy":
        deploy_step = ("滚动发布" + ("（金丝雀模式：分批 + 观察/等待人工决策）"
                                     if canary else "（逐台发布，失败即停）"))
    else:  # rollback
        deploy_step = "全量回滚：所有服务器回滚到 previous_version（并行）"
    tail = ["SnailJob 恢复（按备份快照精确恢复）",
            "发布成功打 Git Tag（git_tag_enabled 且未禁用时）"]
    return [f"目标: {host_text}"] + base + [deploy_step] + tail + [
        "完成后清理/汇总并发送通知"]


def _cli_preview(op: str, kind: str, name: str, env: str, version: str,
                 previous_version: str, flags: dict) -> str:
    """人类可读的命令行预览（与 task_manager.build_cli 对齐，仅展示用）"""
    parts = ["python", "deploy_pipeline.py" if kind == "pipeline" else "deploy.py"]
    parts += [f"--{'pipeline' if kind == 'pipeline' else 'project'}", name]
    if env:
        parts += ["--env", env]
    if version:
        parts += ["--version", version]
    if previous_version:
        parts += ["--previous-version", previous_version]
    for flag in ("auto_version", "skip_build", "rollback", "canary",
                 "no_git_tag"):
        if flags.get(flag):
            parts.append("--" + flag.replace("_", "-"))
    return " ".join(parts)


def _normalize_args(op: str, project_code: str = "", target: str = "",
                    env: str = "", version: str = "",
                    previous_version: str = "", flags: dict = None) -> dict:
    """参数规范化与解析（project/target 可多种指代），返回供建单用的字典"""
    flags = dict(flags or {})
    db_path = flags.pop("_db_path")
    kind = flags.get("kind") or "auto"
    # 定位项目与目标
    svc = pl = None
    if kind in ("auto", "service"):
        svc = _find_project_by_service(db_path, target)
    if not svc and kind in ("auto", "pipeline"):
        pl = _find_pipeline(db_path, target)
    if not svc and not pl:
        raise ExecutorError(
            f"未找到服务或流水线「{target}」——请确认名称（服务名或流水线名）")
    if project_code and svc:
        proj = dbm.get_record(db_path, "projects", svc["project_id"])
        if proj and proj["code"] != project_code and proj["name"] != project_code:
            raise ExecutorError(
                f"服务「{target}」不属于项目「{project_code}」（属于 {proj['code']}）")
    if project_code and pl:
        proj = dbm.get_record(db_path, "projects", pl["project_id"])
        if proj and proj["code"] != project_code and proj["name"] != project_code:
            raise ExecutorError(
                f"流水线「{target}」不属于项目「{project_code}」")
    project_id = (svc or pl)["project_id"]
    kind = "service" if svc else "pipeline"
    name = target
    # 环境校验：env 必填（除 rollback/pipeline 亦需 env）
    if not env:
        raise ExecutorError("缺少目标环境 env（如 prod/test）")
    env_obj = dbm.get_env_by_name(db_path, project_id, env)
    if not env_obj:
        raise ExecutorError(f"项目下不存在环境「{env}」")
    # 版本校验
    auto_version = bool(flags.get("auto_version"))
    if op == "restart":
        version = previous_version = ""
    elif op == "rollback":
        if auto_version:
            raise ExecutorError("回滚不支持 auto_version")
        if not (version or previous_version):
            raise ExecutorError("回滚需提供 version（回滚目标版本）或 previous_version")
        for v, tag in ((version, "version"), (previous_version,
                                               "previous_version")):
            if v and not VERSION_RE.match(v):
                raise ExecutorError(f"{tag} 版本号格式非法（应为 x.y.z）: {v}")
        if version and previous_version and version == previous_version:
            raise ExecutorError("version 与 previous_version 不能相同（回滚目标需不同于当前版本）")
    elif op == "deploy":
        if auto_version:
            if version or previous_version:
                raise ExecutorError("auto_version 与显式 version/previous_version 互斥")
        else:
            if not version:
                raise ExecutorError("缺少版本号 version（或使用 auto_version 自动推导）")
            if not VERSION_RE.match(version):
                raise ExecutorError(
                    f"版本号格式非法（应为 x.y.z）: {version}")
            if previous_version and previous_version == version:
                raise ExecutorError("previous_version 不能与 version 相同")
            if previous_version and not VERSION_RE.match(previous_version):
                raise ExecutorError(
                    f"previous_version 格式非法（应为 x.y.z）: {previous_version}")
    else:
        raise ExecutorError(f"不支持的操作类型: {op}")
    if op == "rollback" and flags.get("canary"):
        raise ExecutorError("canary 与 rollback 互斥，回滚不支持灰度")
    if flags.get("canary") and flags.get("rollback"):
        raise ExecutorError("canary 与 rollback 互斥")
    norm_flags = {k: bool(v) for k, v in flags.items()
                  if k in ("canary", "skip_build", "auto_version",
                           "no_git_tag", "rollback")}
    if op == "rollback":
        # 回滚操作必须带 --rollback 走平台回滚通道（对齐 deploy_routes 语义）
        norm_flags["rollback"] = True
    return dict(db_path=db_path, op=op, kind=kind, name=name,
                project_id=project_id, ref_type="services" if svc else "pipelines",
                ref_id=(svc or pl)["id"], env=env, version=version,
                previous_version=previous_version,
                flags=norm_flags)


def _window_warning(db_path: str, project_id: int):
    """返回 (warn: bool, 描述)；block 级窗口直接抛错拒绝"""
    window = check_release_window(db_path, project_id)
    if not window:
        return False, ""
    if window["block_level"] == "block":
        raise ExecutorError(f"当前处于封网窗口「{window['name']}」，禁止生成发布计划")
    return True, (f"当前处于告警窗口「{window['name']}」，"
                  "审批时需勾选“已知悉窗口警告”")


# ---------- 计划 ----------

def plan_operation(db_path: str, user: dict, op: str, target: str = "",
                   project_code: str = "", env: str = "", version: str = "",
                   previous_version: str = "", canary: bool = False,
                   auto_version: bool = False, skip_build: bool = False,
                   no_git_tag: bool = False, intent: str = "") -> dict:
    """生成一次发布/回滚/重启的操作计划，落库 awaiting_approval；返回 run 记录"""
    try:
        args = _normalize_args(
            op, project_code=project_code, target=target, env=env,
            version=version, previous_version=previous_version,
            flags=dict(_db_path=db_path, kind="auto", canary=canary,
                       auto_version=auto_version, skip_build=skip_build,
                       no_git_tag=no_git_tag))
    except ExecutorError as e:
        raise ExecutorError(str(e)) from e
    warn, window_text = _window_warning(db_path, args["project_id"])
    servers = _env_servers(db_path, args["project_id"], args["env"])
    svc_names = [args["name"]]
    if args["kind"] == "service":
        svc = dbm.get_record(db_path, "services", args["ref_id"])
        if not svc:
            raise ExecutorError("服务记录不存在或已删除")
    else:
        pl = dbm.get_record(db_path, "pipelines", args["ref_id"])
        if not pl:
            raise ExecutorError("流水线记录不存在或已删除")
    plan = {
        "op": args["op"], "kind": args["kind"], "target": args["name"],
        "env": args["env"], "version": args["version"],
        "previous_version": args["previous_version"],
        "flags": args["flags"],
        "window_warning": warn,
        "window_text": window_text,
        "steps": _steps_text(args["op"], args["env"], svc_names,
                             servers, bool(args["flags"].get("canary"))),
        "cli": _cli_preview(args["op"], args["kind"], args["name"],
                            args["env"], args["version"],
                            args["previous_version"], args["flags"]),
    }
    run_id = dbm.create_record(db_path, "agent_runs", dict(
        kind="operation", project_id=args["project_id"],
        ref_type=args["ref_type"], ref_id=args["ref_id"],
        name=args["name"], env=args["env"], version=args["version"],
        previous_version=args["previous_version"],
        intent=intent or "", plan=plan,
        status="awaiting_approval",
        created_by=int(user.get("id", 0)),
        created_by_name=user.get("username", ""),
        created_at=_now()))
    return get_run(db_path, run_id)


def get_run(db_path: str, run_id: int) -> dict:
    run = dbm.get_record(db_path, "agent_runs", run_id)
    if not run:
        raise ExecutorError(f"审批单不存在: #{run_id}")
    return run


def list_runs(db_path: str, status: str = "", project_id: int = 0,
              limit: int = 50) -> list:
    if status and status not in RUN_STATUSES:
        status = ""
    conds, params = [], []
    if status:
        conds.append("status=?")
        params.append(status)
    if project_id:
        conds.append("project_id=?")
        params.append(project_id)
    where = " AND ".join(conds)
    rows = dbm.list_records(db_path, "agent_runs", where, tuple(params),
                            "id DESC")
    return rows[:max(1, min(limit, 200))]


def _submit_task(db_path: str, run: dict) -> int:
    """按计划创建平台任务并提交执行（复用 deploy_routes.create_task 的参数语义）"""
    plan = run.get("plan") or {}
    flags = plan.get("flags") or {}
    type_ = "pipeline" if run["ref_type"] == "pipelines" else \
        ("restart" if plan.get("op") == "restart" else "project")
    action = "rollback" if flags.get("rollback") else \
        ("restart" if type_ == "restart" else "deploy")
    extra = []
    if flags.get("auto_version"):
        extra.append("--auto-version")
    if flags.get("skip_build"):
        extra.append("--skip-build")
    if flags.get("no_git_tag"):
        extra.append("--no-git-tag")
    if flags.get("rollback"):
        extra.append("--rollback")
    if flags.get("canary"):
        extra.append("--canary")
    if run.get("force_window") and type_ != "restart":
        extra.append("--force-window")
    task_id = dbm.create_task(
        db_path, run["project_id"], type_, run["ref_id"], run["name"],
        run.get("env") or "", run.get("version") or "",
        run.get("previous_version") or "", run["created_by"],
        extra=extra, action=action)
    task = dbm.get_task(db_path, task_id)
    task_manager.submit_task(db_path, task, extra)
    return task_id


def _watch_run(db_path: str, run_id: int):
    """后台等待平台任务终态并回写审批单（daemon 线程；进程退出即停止，DB 幂等可重查）"""
    deadline = time.time() + _WATCH_TIMEOUT
    while time.time() < deadline:
        try:
            run = dbm.get_record(db_path, "agent_runs", run_id)
            if not run or run["status"] != "executing":
                return
            task = dbm.get_task(db_path, run.get("task_id") or 0) if run.get("task_id") else None
            if not task:
                return
            if task["status"] in ("success", "failed"):
                summary = _summarize_task(task)
                dbm.update_record(db_path, "agent_runs", run_id, dict(
                    status="success" if task["status"] == "success" else "failed",
                    summary=summary,
                    executed_at=_now()))
                return
        except Exception:
            pass
        time.sleep(_WATCH_INTERVAL)
    dbm.update_record(db_path, "agent_runs", run_id, dict(
        status="failed", summary="等待任务终态超时，请到大屏任务列表确认实际结果"))


def _summarize_task(task: dict) -> str:
    """从任务日志尾部提取关键行生成简短摘要"""
    lines = []
    log_file = task.get("log_file") or ""
    if log_file:
        try:
            from pathlib import Path
            p = Path(log_file)
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="ignore")
                lines = [ln.strip() for ln in text.splitlines()
                         if ln.strip()][-30:]
        except OSError:
            pass
    head = (f"任务 #{task['id']} {task['name']} "
            f"{task.get('env') or ''} {task.get('version') or ''} "
            f"结果: {task['status']}")
    if not lines:
        return head
    # 只保留有信息量的行（含 成功/失败/发布/回滚/异常 关键字）
    keys = ("失败", "成功", "完成", "异常", "回滚", "发布", "中断", "汇总")
    picks = [ln for ln in lines if any(k in ln for k in keys)][-8:]
    tail = "\n".join(picks) if picks else lines[-5].strip() if lines else ""
    return head + ("\n" + tail if tail else "")


def decide_run(db_path: str, run_id: int, decider: dict, action: str,
               note: str = "", force_window: bool = False) -> dict:
    """审批：approve → 提交任务并后台同步；reject → 关闭审批单"""
    run = get_run(db_path, run_id)
    if run["status"] != "awaiting_approval":
        raise ExecutorError(f"审批单状态为 {run['status']}，不可重复审批")
    plan = run.get("plan") or {}
    if action == "reject":
        dbm.update_record(db_path, "agent_runs", run_id, dict(
            status="rejected", decided_by=int(decider.get("id", 0)),
            decided_by_name=decider.get("username", ""),
            decided_at=_now(), decision_note=note or ""))
        return get_run(db_path, run_id)
    if action != "approve":
        raise ExecutorError(f"非法审批动作: {action}")
    if plan.get("window_warning") and not force_window:
        raise ExecutorError(
            "当前存在发布窗口告警，审批必须显式确认（force_window=true）")
    # 审批确认的 force_window 先写回 run 快照，供 _submit_task 组装 --force-window
    run["force_window"] = 1 if force_window else run.get("force_window", 0)
    try:
        task_id = _submit_task(db_path, run)
    except Exception as e:
        # 提交失败（如任务队列异常）：审批单保留待处理并给出可读原因
        dbm.update_record(db_path, "agent_runs", run_id, dict(
            decision_note=f"审批通过但提交失败: {e}"))
        raise ExecutorError(f"提交任务失败: {e}") from e
    dbm.update_record(db_path, "agent_runs", run_id, dict(
        status="executing", task_id=task_id,
        force_window=run["force_window"],
        decided_by=int(decider.get("id", 0)),
        decided_by_name=decider.get("username", ""),
        decided_at=_now(), decision_note=note or ""))
    threading.Thread(target=_watch_run, args=(db_path, run_id),
                     daemon=True).start()
    return get_run(db_path, run_id)


def sync_executing_runs(db_path: str) -> int:
    """轮询同步 executing 状态的审批单（进程重启后由 scheduler 收敛状态）"""
    runs = list_runs(db_path, status="executing", limit=50)
    synced = 0
    for run in runs:
        task = dbm.get_task(db_path, run.get("task_id") or 0) if run.get("task_id") else None
        if task and task["status"] in ("success", "failed"):
            dbm.update_record(db_path, "agent_runs", run["id"], dict(
                status="success" if task["status"] == "success" else "failed",
                summary=_summarize_task(task), executed_at=_now()))
            synced += 1
    return synced


def build_case_from_run(db_path: str, run_id: int, user: dict = None) -> int:
    """执行完成后一键沉淀案例（Phase 4）：失败 run + 关联 advice → agent_cases 草稿"""
    run = get_run(db_path, run_id)
    if run["status"] not in ("failed", "success"):
        raise ExecutorError("仅已完结的审批单可沉淀案例")
    from . import memory, advisor
    advice = advisor.latest_advice(db_path, "task_failed", run.get("task_id") or 0)
    symptom = run.get("summary") or ""
    root_cause = ""
    action_text = ""
    if advice:
        body = advice.get("body") or ""
        root_cause = body[:500]
        action_text = f"参考建议 #{advice['id']}: {advice.get('title')}"
    tags = ["自动沉淀"] if run["status"] == "failed" else ["自动沉淀", "成功案例"]
    svc = dbm.get_record(db_path, run["ref_type"], run["ref_id"]) if run["ref_id"] else None
    return memory.create_case(
        db_path,
        title=f"[审批单#{run_id}] {run['name']} {run.get('env') or ''} "
              f"{run.get('version') or ''} {run['status']}",
        service=svc["name"] if svc else run["name"],
        env=run.get("env") or "", version=run.get("version") or "",
        symptom=symptom, root_cause=root_cause, action=action_text,
        result=run["status"], lesson="", tags=tags,
        source="agent_run", advice_id=advice["id"] if advice else 0,
        user_id=int((user or {}).get("id", 0)),
        username=(user or {}).get("username", ""))


def serialize_public(run: dict) -> dict:
    """对外输出审批单（剥离内部字段；plan JSON 已反序列化由 dbm 保证）"""
    return run
