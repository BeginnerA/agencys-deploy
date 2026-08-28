"""发布增强路由：定时计划 / 发布窗口 / 灰度策略 / 依赖拓扑 / 快照对比 / Webhook"""
import json
import time
from pathlib import Path

import yaml
from flask import Blueprint, request, jsonify, current_app, g

from .. import db as dbm
from ..auth import require_auth, require_role, audit
from ..deploy_scheduler import check_release_window

deploy_enhance_bp = Blueprint("deploy_enhance", __name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _json_body():
    """解析 JSON 请求体；非对象返回 None"""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _db_path():
    return current_app.config["DB_PATH"]


def _clear_default_canary(conn, project_id: int, exclude_id: int = None):
    """将同项目其余灰度策略的 is_default 置 0，保证默认策略唯一（幂等）"""
    params = [project_id]
    sql = "UPDATE canary_configs SET is_default=0 WHERE project_id=? AND is_default=1"
    if exclude_id is not None:
        sql += " AND id<>?"
        params.append(exclude_id)
    conn.execute(sql, tuple(params))


# ========== 定时发布计划 ==========

@deploy_enhance_bp.route("/deploy/schedules", methods=["GET"])
@require_auth
def list_schedules():
    raw_pid = request.args.get("project_id")
    if raw_pid is not None:
        try:
            project_id = int(raw_pid)
        except (TypeError, ValueError):
            return jsonify({"error": "project_id 必须为整数"}), 400
        rows = dbm.list_records(_db_path(), "deploy_schedules",
                                "project_id=?", (project_id,), "id DESC")
    else:
        rows = dbm.list_records(_db_path(), "deploy_schedules", "", (), "id DESC")
    return jsonify(rows)


@deploy_enhance_bp.route("/deploy/schedules", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_schedule():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id")
    name = body.get("name", "")
    type_ = body.get("type", "once")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return jsonify({"error": "project_id 必须为整数"}), 400
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name 必填"}), 400
    if type_ not in ("once", "cron"):
        return jsonify({"error": "type 必须为 once 或 cron"}), 400

    ref_type = body.get("ref_type", "project")
    ref_id = body.get("ref_id", 0)
    service_id = body.get("service_id")  # 可选：指定服务级别定时发布
    env = body.get("env", "")
    version = body.get("version", "")
    previous_version = body.get("previous_version", "")
    extra = body.get("extra", [])
    if isinstance(extra, list):
        extra = json.dumps(extra, ensure_ascii=False)
    schedule_at = body.get("schedule_at", 0)
    cron_expr = body.get("cron_expr", "")

    # 指定服务时，覆盖 ref_type/ref_id
    if service_id and isinstance(service_id, int) and service_id > 0:
        ref_type = "service"
        ref_id = service_id

    if type_ == "cron":
        if not cron_expr:
            return jsonify({"error": "cron 类型必须提供 cron_expr"}), 400
        try:
            from croniter import croniter
            from datetime import datetime
            cron = croniter(cron_expr, datetime.now())
            next_run_at = int(cron.get_next())
        except (ImportError, ValueError, KeyError) as e:
            return jsonify({"error": f"cron 表达式无效: {e}"}), 400
    else:
        if not schedule_at:
            return jsonify({"error": "once 类型必须提供 schedule_at"}), 400
        next_run_at = int(schedule_at)

    sid = dbm.create_record(_db_path(), "deploy_schedules", dict(
        project_id=project_id, type=type_, name=name.strip(),
        ref_type=ref_type, ref_id=ref_id, env=env,
        version=version, previous_version=previous_version,
        extra=extra if isinstance(extra, str) else json.dumps(extra),
        schedule_at=int(schedule_at), cron_expr=cron_expr,
        enabled=1, last_run_at=0, next_run_at=next_run_at,
        created_by=g.user["id"], created_at=int(time.time())))
    audit("create_schedule", "deploy_schedule", sid,
          {"name": name, "type": type_, "project_id": project_id})
    return jsonify({"id": sid}), 201


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_schedule(sid):
    rec = dbm.get_record(_db_path(), "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    fields = {}
    for k in ("name", "env", "version", "previous_version", "cron_expr"):
        if k in body:
            fields[k] = body[k]
    if "ref_id" in body:
        fields["ref_id"] = body["ref_id"]
    if "service_id" in body:
        svc_id = body["service_id"]
        if isinstance(svc_id, int) and svc_id > 0:
            fields["ref_type"] = "service"
            fields["ref_id"] = svc_id
        else:
            fields["ref_type"] = "project"
            fields["ref_id"] = rec["project_id"]
    if "schedule_at" in body:
        fields["schedule_at"] = int(body["schedule_at"])
        fields["next_run_at"] = int(body["schedule_at"])
    if "extra" in body:
        extra = body["extra"]
        fields["extra"] = json.dumps(extra, ensure_ascii=False) if isinstance(extra, list) else extra
    if fields:
        dbm.update_record(_db_path(), "deploy_schedules", sid, fields)
    audit("update_schedule", "deploy_schedule", sid, fields)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_schedule(sid):
    rec = dbm.get_record(_db_path(), "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    dbm.delete_record(_db_path(), "deploy_schedules", sid)
    audit("delete_schedule", "deploy_schedule", sid, {"name": rec["name"]})
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_schedule(sid):
    rec = dbm.get_record(_db_path(), "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    new_enabled = 0 if rec["enabled"] else 1
    dbm.update_record(_db_path(), "deploy_schedules", sid, {"enabled": new_enabled})
    audit("toggle_schedule", "deploy_schedule", sid, {"enabled": new_enabled})
    return jsonify({"enabled": bool(new_enabled)})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>/run", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def run_schedule(sid):
    rec = dbm.get_record(_db_path(), "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    body = request.get_json(silent=True)
    force_window = isinstance(body, dict) and body.get("force_window") is True
    # 窗口检查
    window = check_release_window(_db_path(), rec["project_id"])
    if window and window["block_level"] == "block":
        return jsonify({"error": f"当前处于封网窗口 '{window['name']}'，禁止发布"}), 403
    # 创建任务
    extra = rec.get("extra", [])
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = []
    if window and window["block_level"] == "warn":
        if not force_window:
            return jsonify({"error": "window_warn",
                            "window": {"name": window["name"], "id": window["id"]}}), 409
        extra.append("--force-window")
    # ref_type=service 时，解析服务名作为任务名
    task_name = rec["name"]
    if rec.get("ref_type") == "service" and rec.get("ref_id"):
        svc = dbm.get_record(_db_path(), "services", rec["ref_id"])
        if svc:
            task_name = svc["name"]
    task_id = dbm.create_task(
        _db_path(), rec["project_id"],
        rec["ref_type"], rec["ref_id"],
        task_name, rec["env"],
        rec["version"], rec["previous_version"],
        g.user["id"], extra=extra)
    from .. import task_manager
    task = dbm.get_task(_db_path(), task_id)
    task_manager.submit_task(_db_path(), task, extra)
    audit("run_schedule", "deploy_schedule", sid, {"task_id": task_id})
    return jsonify({"task_id": task_id, "status": "pending"}), 201


# ========== 发布窗口 ==========

_VALID_SCOPE_TYPES = ("all", "project", "env", "server", "service")


def _validate_scope(scope) -> str:
    """校验 scope 结构，返回错误信息或空字符串"""
    if not isinstance(scope, dict):
        return "scope 格式错误"
    if scope.get("type") not in _VALID_SCOPE_TYPES:
        return "scope type 非法"
    if not isinstance(scope.get("ids"), list):
        return "scope.ids 需为列表"
    if "env_ids" in scope and not isinstance(scope["env_ids"], list):
        return "scope.env_ids 需为列表"
    return ""


@deploy_enhance_bp.route("/deploy/windows", methods=["GET"])
@require_auth
def list_windows():
    rows = dbm.list_records(_db_path(), "release_windows", "", (), "id DESC")
    return jsonify(rows)


@deploy_enhance_bp.route("/deploy/windows", methods=["POST"])
@require_auth
@require_role("admin")
def create_window():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id", 0)
    name = body.get("name", "")
    start_at = body.get("start_at", 0)
    end_at = body.get("end_at", 0)
    block_level = body.get("block_level", "warn")
    repeat_yearly = 1 if body.get("repeat_yearly") else 0
    scope = body.get("scope") or {"type": "all", "ids": []}
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name 必填"}), 400
    if block_level not in ("block", "warn"):
        return jsonify({"error": "block_level 必须为 block 或 warn"}), 400
    err = _validate_scope(scope)
    if err:
        return jsonify({"error": err}), 400
    wid = dbm.create_record(_db_path(), "release_windows", dict(
        project_id=project_id, name=name.strip(),
        start_at=int(start_at), end_at=int(end_at),
        block_level=block_level, scope=scope,
        repeat_yearly=repeat_yearly,
        created_by=g.user["id"], created_at=int(time.time())))
    audit("create_window", "release_window", wid, {"name": name})
    return jsonify({"id": wid}), 201


@deploy_enhance_bp.route("/deploy/windows/<int:wid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_window(wid):
    rec = dbm.get_record(_db_path(), "release_windows", wid)
    if not rec:
        return jsonify({"error": "窗口不存在"}), 404
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    fields = {}
    for k in ("name", "block_level"):
        if k in body:
            fields[k] = body[k]
    for k in ("start_at", "end_at"):
        if k in body:
            fields[k] = int(body[k])
    if "repeat_yearly" in body:
        fields["repeat_yearly"] = 1 if body["repeat_yearly"] else 0
    if "scope" in body:
        scope = body["scope"] or {}
        err = _validate_scope(scope)
        if err:
            return jsonify({"error": err}), 400
        fields["scope"] = scope
    if fields:
        dbm.update_record(_db_path(), "release_windows", wid, fields)
    audit("update_window", "release_window", wid, fields)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/windows/<int:wid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_window(wid):
    rec = dbm.get_record(_db_path(), "release_windows", wid)
    if not rec:
        return jsonify({"error": "窗口不存在"}), 404
    dbm.delete_record(_db_path(), "release_windows", wid)
    audit("delete_window", "release_window", wid, {"name": rec["name"]})
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/windows/check", methods=["GET"])
@require_auth
def check_window():
    raw_pid = request.args.get("project_id", "0")
    try:
        project_id = int(raw_pid)
    except (TypeError, ValueError):
        return jsonify({"error": "project_id 必须为整数"}), 400
    result = check_release_window(_db_path(), project_id)
    return jsonify(result)


# ========== 灰度策略 ==========

@deploy_enhance_bp.route("/deploy/canary-configs", methods=["GET"])
@require_auth
def list_canary_configs():
    raw_pid = request.args.get("project_id")
    if raw_pid is not None:
        try:
            project_id = int(raw_pid)
        except (TypeError, ValueError):
            return jsonify({"error": "project_id 必须为整数"}), 400
        rows = dbm.list_records(_db_path(), "canary_configs",
                                "project_id=?", (project_id,), "id")
    else:
        rows = dbm.list_records(_db_path(), "canary_configs", "", (), "id")
    return jsonify(rows)


@deploy_enhance_bp.route("/deploy/canary-configs", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_canary_config():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id")
    name = body.get("name", "")
    steps = body.get("steps", [])
    is_default = 1 if body.get("is_default") else 0
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return jsonify({"error": "project_id 必须为整数"}), 400
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name 必填"}), 400
    if not isinstance(steps, list):
        return jsonify({"error": "steps 必须为数组"}), 400
    if is_default:
        # 设为默认时先清除同项目其他默认策略，保证唯一（deploy.py 按 is_default=1 读取）
        conn = dbm.get_conn(_db_path())
        try:
            _clear_default_canary(conn, project_id)
            conn.commit()
        finally:
            conn.close()
    cid = dbm.create_record(_db_path(), "canary_configs", dict(
        project_id=project_id, name=name.strip(),
        steps=json.dumps(steps, ensure_ascii=False),
        is_default=is_default, created_at=int(time.time())))
    audit("create_canary_config", "canary_config", cid, {"name": name})
    return jsonify({"id": cid}), 201


@deploy_enhance_bp.route("/deploy/canary-configs/<int:cid>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_canary_config(cid):
    rec = dbm.get_record(_db_path(), "canary_configs", cid)
    if not rec:
        return jsonify({"error": "配置不存在"}), 404
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    fields = {}
    if "name" in body:
        fields["name"] = body["name"]
    if "steps" in body:
        steps = body["steps"]
        if not isinstance(steps, list):
            return jsonify({"error": "steps 必须为数组"}), 400
        fields["steps"] = json.dumps(steps, ensure_ascii=False)
    if "is_default" in body:
        fields["is_default"] = 1 if body["is_default"] else 0
    if fields.get("is_default") == 1:
        # 设为默认时先清除同项目其他默认策略，保证唯一
        conn = dbm.get_conn(_db_path())
        try:
            _clear_default_canary(conn, rec["project_id"], exclude_id=cid)
            conn.commit()
        finally:
            conn.close()
    if fields:
        dbm.update_record(_db_path(), "canary_configs", cid, fields)
    audit("update_canary_config", "canary_config", cid, fields)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/canary-configs/<int:cid>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_canary_config(cid):
    rec = dbm.get_record(_db_path(), "canary_configs", cid)
    if not rec:
        return jsonify({"error": "配置不存在"}), 404
    dbm.delete_record(_db_path(), "canary_configs", cid)
    audit("delete_canary_config", "canary_config", cid, {"name": rec["name"]})
    return jsonify({"ok": True})


# ========== 灰度运行中决策（Web 子进程 ↔ deploy.py 文件协议） ==========

@deploy_enhance_bp.route("/deploy/canary-decisions/<int:task_id>", methods=["GET"])
@require_auth
def get_canary_decision(task_id):
    """读取灰度决策状态：none=无请求 / waiting=等待决策 / continue|stop|rollback=已决策"""
    from deploy import read_canary_decision
    return jsonify(read_canary_decision(task_id))


@deploy_enhance_bp.route("/deploy/canary-decisions/<int:task_id>", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def post_canary_decision(task_id):
    """提交灰度决策：continue 继续 / stop 停止 / rollback 回滚首台"""
    body = _json_body()
    action = body.get("action") if body else None
    if action not in ("continue", "stop", "rollback"):
        return jsonify({"error": "action 必须为 continue/stop/rollback"}), 400
    from deploy import read_canary_decision, canary_decision_path
    cur = read_canary_decision(task_id)
    if cur.get("state") != "waiting":
        return jsonify({"error": "当前无等待中的灰度决策"}), 404
    path = canary_decision_path(task_id)
    cur["state"] = action
    cur["updated_at"] = int(time.time())
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
    except OSError:
        return jsonify({"error": "决策文件写入失败（任务可能已结束）"}), 500
    audit("canary_decision", "task", task_id, {"action": action})
    return jsonify({"ok": True, "action": action})


# ========== 依赖拓扑 & 影响分析 ==========

@deploy_enhance_bp.route("/deploy/topology/<int:project_id>", methods=["GET"])
@require_auth
def get_topology(project_id):
    """获取项目服务依赖拓扑：手动依赖 + dubbo 自动检测"""
    services = dbm.list_services(_db_path(), project_id, enabled_only=True)
    svc_names = {s["name"] for s in services}
    # 手动依赖
    manual_deps = dbm.list_records(_db_path(), "service_dependencies",
                                   "project_id=?", (project_id,), "id")
    edges = []
    edge_set = set()
    for d in manual_deps:
        key = (d["source_service"], d["target_service"])
        if key not in edge_set:
            edges.append({"source": d["source_service"], "target": d["target_service"],
                          "type": d["dep_type"]})
            edge_set.add(key)
    # 自动检测 dubbo 依赖
    for svc in services:
        dubbo = svc.get("dubbo_services", [])
        if isinstance(dubbo, str):
            try:
                dubbo = json.loads(dubbo)
            except (ValueError, TypeError):
                dubbo = []
        if isinstance(dubbo, list):
            for d in dubbo:
                target = d if isinstance(d, str) else d.get("service", "")
                if target and target in svc_names:
                    key = (svc["name"], target)
                    if key not in edge_set:
                        edges.append({"source": svc["name"], "target": target,
                                      "type": "dubbo"})
                        edge_set.add(key)
    nodes = [{"name": s["name"], "service_name": s.get("service_name", "")}
             for s in services]
    return jsonify({"nodes": nodes, "edges": edges})


@deploy_enhance_bp.route("/deploy/topology", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def add_dependency():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id")
    source = body.get("source_service", "")
    target = body.get("target_service", "")
    dep_type = body.get("dep_type", "manual")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return jsonify({"error": "project_id 必须为整数"}), 400
    if not source or not target:
        return jsonify({"error": "source_service 和 target_service 必填"}), 400
    if source == target:
        return jsonify({"error": "不能依赖自己"}), 400
    # 环检测：BFS 从 target 出发，看是否能到达 source
    if _has_path(_db_path(), project_id, target, source):
        return jsonify({"error": "添加此依赖会形成循环依赖"}), 400
    try:
        did = dbm.create_record(_db_path(), "service_dependencies", dict(
            project_id=project_id, source_service=source,
            target_service=target, dep_type=dep_type,
            created_at=int(time.time())))
    except Exception as e:
        if "UNIQUE" in str(e):
            return jsonify({"error": "依赖关系已存在"}), 400
        raise
    audit("add_dependency", "service_dependency", did,
          {"source": source, "target": target})
    return jsonify({"id": did}), 201


def _has_path(db_path, project_id, from_svc, to_svc):
    """BFS 检测从 from_svc 是否能到达 to_svc（沿依赖边 source→target）"""
    deps = dbm.list_records(db_path, "service_dependencies",
                            "project_id=?", (project_id,), "id")
    # 构建邻接表：source -> [target, ...]
    adj = {}
    for d in deps:
        adj.setdefault(d["source_service"], []).append(d["target_service"])
    # BFS
    visited = set()
    queue = [from_svc]
    while queue:
        curr = queue.pop(0)
        if curr == to_svc:
            return True
        if curr in visited:
            continue
        visited.add(curr)
        for nxt in adj.get(curr, []):
            if nxt not in visited:
                queue.append(nxt)
    return False


@deploy_enhance_bp.route("/deploy/topology/<int:dep_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_dependency(dep_id):
    rec = dbm.get_record(_db_path(), "service_dependencies", dep_id)
    if not rec:
        return jsonify({"error": "依赖不存在"}), 404
    dbm.delete_record(_db_path(), "service_dependencies", dep_id)
    audit("delete_dependency", "service_dependency", dep_id,
          {"source": rec["source_service"], "target": rec["target_service"]})
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/impact/<int:project_id>", methods=["GET"])
@require_auth
def impact_analysis(project_id):
    """影响分析：上下游依赖 + 依赖类型"""
    service_name = request.args.get("service", "")
    if not service_name:
        return jsonify({"error": "service 参数必填"}), 400
    deps = dbm.list_records(_db_path(), "service_dependencies",
                            "project_id=?", (project_id,), "id")
    # 上游：谁依赖我（source → me）
    upstream = [{"name": d["source_service"], "type": d.get("dep_type", "manual")}
                for d in deps if d["target_service"] == service_name]
    # 下游：我依赖谁（me → target）
    downstream = [{"name": d["target_service"], "type": d.get("dep_type", "manual")}
                  for d in deps if d["source_service"] == service_name]
    # 间接上游（BFS 两层）
    adj_up = {}
    for d in deps:
        adj_up.setdefault(d["target_service"], []).append(d["source_service"])
    indirect_up = set()
    for u in upstream:
        for uu in adj_up.get(u["name"], []):
            if uu != service_name:
                indirect_up.add(uu)
    # 间接下游（BFS 两层）
    adj_down = {}
    for d in deps:
        adj_down.setdefault(d["source_service"], []).append(d["target_service"])
    indirect_down = set()
    for d in downstream:
        for dd in adj_down.get(d["name"], []):
            if dd != service_name:
                indirect_down.add(dd)
    return jsonify({"service": service_name, "upstream": upstream,
                    "downstream": downstream,
                    "indirect_upstream": sorted(indirect_up),
                    "indirect_downstream": sorted(indirect_down),
                    # 同机部署服务（契约字段）：服务-服务器映射仅运行时
                    # （monitor docker ps 探测）可知，DB 模型（services 仅绑定
                    # 项目）无法精确计算，暂返回空列表，后续可接入探测数据填充
                    "colocated": []})


# ========== 发布顺序建议 ==========

@deploy_enhance_bp.route("/deploy/publish-order/<int:project_id>", methods=["GET"])
@require_auth
def publish_order(project_id):
    """拓扑排序：返回建议的发布顺序（先发布被依赖的）"""
    services = dbm.list_services(_db_path(), project_id, enabled_only=True)
    svc_names = [s["name"] for s in services]
    deps = dbm.list_records(_db_path(), "service_dependencies",
                            "project_id=?", (project_id,), "id")
    # Kahn 算法拓扑排序
    in_degree = {n: 0 for n in svc_names}
    adj = {n: [] for n in svc_names}
    for d in deps:
        src, tgt = d["source_service"], d["target_service"]
        if src in in_degree and tgt in in_degree:
            adj[tgt].append(src)  # 被依赖的先发布
            in_degree[src] += 1
    queue = [n for n in svc_names if in_degree[n] == 0]
    queue.sort()  # 同层级按名称排序，保证稳定
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for nxt in sorted(adj.get(node, [])):
            in_degree[nxt] -= 1
            if in_degree[nxt] == 0:
                queue.append(nxt)
    # 剩余未排序的（有环）
    remaining = [n for n in svc_names if n not in order]
    if remaining:
        order.extend(sorted(remaining))
    return jsonify({"order": order, "has_cycle": bool(remaining)})


# ========== 发布快照 & 对比 ==========

@deploy_enhance_bp.route("/deploy/snapshots/<int:project_id>", methods=["GET"])
@require_auth
def list_snapshots(project_id):
    env = request.args.get("env", "")
    if env:
        rows = dbm.list_records(_db_path(), "deployment_snapshots",
                                "project_id=? AND env=?",
                                (project_id, env), "deployed_at DESC")
    else:
        rows = dbm.list_records(_db_path(), "deployment_snapshots",
                                "project_id=?", (project_id,),
                                "deployed_at DESC")
    return jsonify(rows)


@deploy_enhance_bp.route("/deploy/compare", methods=["GET"])
@require_auth
def compare_snapshots():
    old_id = request.args.get("old_id", type=int)
    new_id = request.args.get("new_id", type=int)

    # 手动模式：old_id + new_id 都存在
    if old_id and new_id:
        old = dbm.get_record(_db_path(), "deployment_snapshots", old_id)
        new = dbm.get_record(_db_path(), "deployment_snapshots", new_id)
        if not old or not new:
            return jsonify({"error": "指定的快照不存在"}), 404
    else:
        # 手动模式参数不完整时，走默认模式
        if old_id or new_id:
            return jsonify({"error": "手动模式需要同时提供 old_id 和 new_id"}), 400
        project_id = request.args.get("project_id", type=int)
        env = request.args.get("env", "")
        if not project_id or not env:
            return jsonify({"error": "project_id 和 env 参数必填"}), 400
        rows = dbm.list_records(_db_path(), "deployment_snapshots",
                                "project_id=? AND env=?",
                                (project_id, env), "deployed_at DESC")
        if len(rows) < 2:
            return jsonify({"error": "至少需要 2 个快照才能对比"}), 400
        old = rows[1]
        new = rows[0]

    old_versions = old.get("image_versions", {})
    new_versions = new.get("image_versions", {})
    if isinstance(old_versions, str):
        try:
            old_versions = json.loads(old_versions)
        except (ValueError, TypeError):
            old_versions = {}
    if isinstance(new_versions, str):
        try:
            new_versions = json.loads(new_versions)
        except (ValueError, TypeError):
            new_versions = {}
    # 计算变更
    changes = []
    all_keys = set(old_versions.keys()) | set(new_versions.keys())
    for k in sorted(all_keys):
        old_v = old_versions.get(k, "")
        new_v = new_versions.get(k, "")
        if old_v != new_v:
            changes.append({"service": k, "old": old_v, "new": new_v})
    return jsonify({"old": {"id": old["id"], "version": old["version"],
                            "env": old.get("env", ""),
                            "deployed_at": old["deployed_at"],
                            "image_versions": old_versions},
                    "new": {"id": new["id"], "version": new["version"],
                            "env": new.get("env", ""),
                            "deployed_at": new["deployed_at"],
                            "image_versions": new_versions},
                    "changes": changes})


# ========== Webhook ==========

@deploy_enhance_bp.route("/deploy/webhook", methods=["POST"])
def webhook():
    """外部 CI/CD 触发发布（token 认证）"""
    token = request.headers.get("X-Webhook-Token", "")
    if not token:
        token = request.args.get("token", "")
    expected = _get_webhook_token()
    if not expected or token != expected:
        return jsonify({"error": "token 无效"}), 403
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id")
    service_name = body.get("service", "")
    version = body.get("version", "")
    env = body.get("env", "")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return jsonify({"error": "project_id 必须为整数"}), 400
    if not service_name or not version:
        return jsonify({"error": "service 和 version 必填"}), 400
    svc = dbm.get_service_by_name(_db_path(), project_id, service_name)
    if not svc:
        return jsonify({"error": "服务不存在"}), 404
    task_id = dbm.create_task(
        _db_path(), project_id, "project", svc["id"],
        service_name, env, version, "", 0, action="deploy")
    from .. import task_manager
    task = dbm.get_task(_db_path(), task_id)
    task_manager.submit_task(_db_path(), task, [])
    return jsonify({"task_id": task_id, "status": "pending"}), 201


def _get_webhook_token() -> str:
    """从 config/deploy.yaml 读取 webhook_token"""
    config_file = CONFIG_DIR / "deploy.yaml"
    if not config_file.exists():
        return ""
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("deploy", {}).get("webhook_token", ""))
    except Exception:
        return ""
