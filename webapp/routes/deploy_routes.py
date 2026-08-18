"""发布任务路由：创建/列表/详情/日志/终止"""
from flask import Blueprint, request, jsonify, current_app, g

from .. import db as dbm
from ..auth import require_auth, require_role, audit
from ..task_manager import submit_task, tail_log, kill_task

deploy_bp = Blueprint("deploy", __name__)


def _json_body():
    """解析 JSON 请求体；非对象（数组/字符串/null/解析失败）返回 None"""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _bool_field(body, key, default=False):
    """严格布尔取值：缺省用默认；非布尔返回 None 表示非法"""
    if key not in body:
        return default
    if not isinstance(body[key], bool):
        return None
    return body[key]


def _resolve_ref(db_path, project_id, type_, ref_id):
    """按类型解析引用记录；不存在或不属于该项目返回 None（越权信息隐藏）"""
    table = "services" if type_ == "project" else "pipelines"
    rec = dbm.get_record(db_path, table, ref_id)
    if not rec or rec["project_id"] != project_id:
        return None
    return rec


@deploy_bp.route("/tasks", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_task():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    project_id = body.get("project_id")
    type_ = body.get("type", "project")
    ref_id = body.get("ref_id")
    if not isinstance(project_id, int) or isinstance(project_id, bool) \
            or not isinstance(ref_id, int) or isinstance(ref_id, bool) \
            or type_ not in ("project", "pipeline"):
        return jsonify({"error": "project_id/ref_id 必填（整数），type 为 project/pipeline"}), 400
    rec = _resolve_ref(current_app.config["DB_PATH"], project_id, type_, ref_id)
    if not rec:
        return jsonify({"error": "引用的服务/流水线不存在"}), 404
    for k in ("env", "version", "previous_version"):
        if not isinstance(body.get(k, ""), str):
            return jsonify({"error": f"{k} 必须为字符串"}), 400
    env = body.get("env", "")
    version = body.get("version", "")
    previous_version = body.get("previous_version", "")
    rollback = _bool_field(body, "rollback", False)
    auto_version = _bool_field(body, "auto_version", False)
    skip_build = _bool_field(body, "skip_build", False)
    if rollback is None or auto_version is None or skip_build is None:
        return jsonify({"error": "rollback/auto_version/skip_build 必须为布尔值"}), 400
    if auto_version and (version or previous_version):
        return jsonify({"error": "auto_version 与显式 version/previous_version 互斥"}), 400
    if rollback:
        if not version and not previous_version:
            return jsonify({"error": "回滚需指定版本（version 或 previous_version）"}), 400
        # 回滚：deploy.py 语义 --previous-version 才是回滚目标，双字段冲突时 previous_version 优先；
        # version 兜底为目标版本（deploy.py/流水线均强制 version 非空，空值会使子进程直接失败）
        target = previous_version or version
        previous_version = target
        version = version or target
    extra = []
    if auto_version:
        extra.append("--auto-version")
    if skip_build:
        extra.append("--skip-build")
    if rollback:
        extra.append("--rollback")
    task_id = dbm.create_task(current_app.config["DB_PATH"], project_id, type_,
                              ref_id, rec["name"], env, version,
                              previous_version, g.user["id"])
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    submit_task(current_app.config["DB_PATH"], task, extra)
    detail_version = version or previous_version
    if not detail_version and auto_version:
        detail_version = "auto"
    audit("deploy" if not rollback else "rollback", type_, ref_id,
          {"name": rec["name"], "env": env, "version": detail_version})
    return jsonify({"id": task_id, "status": "pending"}), 201


@deploy_bp.route("/tasks")
@require_auth
def list_tasks():
    raw_pid = request.args.get("project_id")
    if raw_pid is not None:
        try:
            project_id = int(raw_pid)
        except (TypeError, ValueError):
            return jsonify({"error": "project_id 必须为整数"}), 400
    else:
        project_id = None
    limit = request.args.get("limit", 100, type=int)
    # SQLite LIMIT 必须非负；非法值回退默认
    if not isinstance(limit, int) or not (1 <= limit <= 500):
        limit = 100
    return jsonify(dbm.list_tasks(current_app.config["DB_PATH"], project_id, limit))


@deploy_bp.route("/tasks/<int:task_id>")
@require_auth
def task_detail(task_id):
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(task)


@deploy_bp.route("/tasks/<int:task_id>/log")
@require_auth
def task_log(task_id):
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    offset = request.args.get("offset", 0, type=int)
    if not isinstance(offset, int) or offset < 0:
        offset = 0
    # 仅运行中/等待中的任务使用尾部截断（避免打开时从头逐块追赶导致卡顿）；
    # 已结束任务始终返回完整日志
    tail = task["status"] in ("running", "pending")
    text, new_offset, truncated = tail_log(task.get("log_file", ""), offset, tail=tail)
    return jsonify({"log": text, "offset": new_offset, "status": task["status"], "truncated": truncated})


@deploy_bp.route("/tasks/<int:task_id>/kill", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def task_kill(task_id):
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    ok = kill_task(task_id, current_app.config["DB_PATH"])
    if ok:
        audit("kill_task", "task", task_id, {"name": task["name"]})
    return jsonify({"killed": ok})


@deploy_bp.route("/tasks/<int:task_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def task_delete(task_id):
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    ok, msg = dbm.delete_task(current_app.config["DB_PATH"], task_id)
    if not ok:
        return jsonify({"error": msg}), 400
    audit("delete_task", "task", task_id, {"name": task["name"]})
    return jsonify({"ok": True})
