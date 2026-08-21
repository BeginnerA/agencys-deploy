"""配置管理路由：项目/服务/环境/服务器/流水线 CRUD + YAML 导入导出（admin）"""
import re
import time
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app

from .. import db as dbm
from ..auth import require_auth, require_role, audit

config_bp = Blueprint("config", __name__)

DEPLOY_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = DEPLOY_ROOT / "config"

REQUIRED_SERVICE = ("name", "image_name", "service_name")
REQUIRED_ENV = ("name",)
REQUIRED_PIPELINE = ("name",)

# 环境名/项目 code 会拼入导出文件名（exporter._check_safe_name 同款白名单）
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+$")

_LIST_FIELDS_SERVICE = ("build_order", "nacos_groups", "dubbo_services",
                        "log_error_keywords")


def _ok(data=None):
    return jsonify(data if data is not None else {"ok": True})


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _json_body():
    """解析 JSON 请求体；非对象（数组/字符串/null/解析失败）返回 None"""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _owned_or_404(table, rid, project_id):
    """校验记录存在且属于 URL 中的项目；返回 (rec, err_response)"""
    rec = dbm.get_record(current_app.config["DB_PATH"], table, rid)
    if not rec:
        return None, _err("记录不存在", 404)
    if rec["project_id"] != project_id:
        return None, _err("记录不存在", 404)
    return rec, None


def _bool_field(body, key, default=True):
    """严格布尔取值：缺省用默认；非布尔返回 None 表示非法"""
    if key not in body:
        return default
    if not isinstance(body[key], bool):
        return None
    return body[key]


def _list_field(body, key):
    """列表字段取值：缺省空列表；非 list 返回 None 表示非法"""
    if key not in body:
        return []
    if not isinstance(body[key], list):
        return None
    return body[key]


def _check_unique(table, project_id, name, exclude_id=None):
    for r in dbm.list_records(current_app.config["DB_PATH"], table,
                              "project_id=?", (project_id,)):
        if r["name"] == name and r["id"] != exclude_id:
            return True
    return False


def _service_in_pipeline(project_id, service_id) -> bool:
    """服务是否被本项目流水线 stages 引用（悬空引用删除保护）"""
    for pl in dbm.list_pipelines(current_app.config["DB_PATH"], project_id):
        for stage in pl.get("stages", []) or []:
            if not isinstance(stage, dict):
                continue
            for svc in stage.get("services", []) or []:
                if isinstance(svc, dict) and svc.get("service_id") == service_id:
                    return True
    return False


def _mask_ssh_password(rec: dict) -> dict:
    """只读接口脱敏：ssh_password 非空时遮蔽（保持 NULL/空串语义，凭据不回显）"""
    out = dict(rec)
    if out.get("ssh_password"):
        out["ssh_password"] = "***"
    return out


def _mask_env_ssh(env: dict) -> dict:
    """环境列表脱敏：ssh JSON 字段中的 password 非空时遮蔽"""
    out = dict(env)
    ssh = out.get("ssh") or {}
    if isinstance(ssh, dict) and ssh.get("password"):
        ssh = dict(ssh)
        ssh["password"] = "***"
        out["ssh"] = ssh
    return out


def _check_pipeline_stages(project_id, stages):
    """校验 stages 结构及引用的服务存在（悬空引用会阻断导出，创建侧应拒绝）"""
    for stage in stages:
        if not isinstance(stage, dict):
            return "stage 必须为对象"
        svcs = stage.get("services", [])
        if not isinstance(svcs, list):
            return "stage.services 必须为数组"
        for svc in svcs:
            if not isinstance(svc, dict):
                return "stage.services 元素必须为对象"
            sid = svc.get("service_id")
            if not isinstance(sid, int) or isinstance(sid, bool):
                return "service_id 必须为整数"
            rec = dbm.get_record(current_app.config["DB_PATH"], "services", sid)
            if not rec or rec["project_id"] != project_id:
                return f"stages 引用了不存在的服务 (service_id={sid})"
    return None


# ---------- 项目 ----------

@config_bp.route("/projects", methods=["GET"])
@require_auth
def list_projects():
    rows = dbm.list_records(current_app.config["DB_PATH"], "projects")
    out = []
    for p in rows:
        out.append({"id": p["id"], "name": p["name"], "code": p["code"],
                    "description": p["description"], "git_org": p["git_org"],
                    "services": len(dbm.list_services(current_app.config["DB_PATH"], p["id"], enabled_only=True)),
                    "environments": len(dbm.list_environments(current_app.config["DB_PATH"], p["id"])),
                    "pipelines": len(dbm.list_pipelines(current_app.config["DB_PATH"], p["id"]))})
    return _ok(out)


@config_bp.route("/projects", methods=["POST"])
@require_auth
@require_role("admin")
def create_project():
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    name = body.get("name", "")
    code = body.get("code", "")
    if not isinstance(name, str) or not isinstance(code, str):
        return _err("参数类型错误")
    name, code = name.strip(), code.strip()
    if not name or not code:
        return _err("name/code 必填")
    if not _SAFE_NAME.match(code):
        return _err("项目编码仅允许字母数字 _ -（导出时用作目录名）")
    for p in dbm.list_records(current_app.config["DB_PATH"], "projects"):
        if p["code"] == code:
            return _err(f"项目编码 {code} 已存在")
    desc = body.get("description", "")
    git_org = body.get("git_org", "")
    if not isinstance(desc, str) or not isinstance(git_org, str):
        return _err("参数类型错误")
    pid = dbm.create_record(current_app.config["DB_PATH"], "projects", dict(
        name=name, code=code, description=desc, git_org=git_org,
        created_at=time.time()))
    audit("create_project", "project", pid, {"name": name, "code": code})
    return _ok({"id": pid}), 201


@config_bp.route("/projects/<int:pid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_project(pid):
    if not dbm.get_record(current_app.config["DB_PATH"], "projects", pid):
        return _err("记录不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    fields = {k: v for k, v in body.items() if k in ("name", "description", "git_org")}
    if "name" in fields and (not isinstance(fields["name"], str)
                             or not fields["name"].strip()):
        return _err("name 不能为空")
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "projects", pid, fields)
    audit("update_project", "project", pid, fields)
    return _ok()


@config_bp.route("/projects/<int:pid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_project(pid):
    if not dbm.get_record(current_app.config["DB_PATH"], "projects", pid):
        return _err("记录不存在", 404)
    ok, msg = dbm.delete_project(current_app.config["DB_PATH"], pid)
    if not ok:
        return _err(msg)
    audit("delete_project", "project", pid, {})
    return _ok()


# ---------- 服务 ----------

@config_bp.route("/projects/<int:pid>/services", methods=["GET"])
@require_auth
def list_services(pid):
    return _ok(dbm.list_services(current_app.config["DB_PATH"], pid))


@config_bp.route("/projects/<int:pid>/services", methods=["POST"])
@require_auth
@require_role("admin")
def create_service(pid):
    if not dbm.get_record(current_app.config["DB_PATH"], "projects", pid):
        return _err("项目不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    for k in REQUIRED_SERVICE:
        if not isinstance(body.get(k), str) or not body.get(k).strip():
            return _err(f"{k} 必填")
    name = body["name"].strip()
    if _check_unique("services", pid, name):
        return _err("项目内服务名已存在")
    lists = {}
    for k in _LIST_FIELDS_SERVICE:
        v = _list_field(body, k)
        if v is None:
            return _err(f"{k} 必须为数组")
        lists[k] = v
    hp = body.get("health_port", 0)
    if not isinstance(hp, int) or isinstance(hp, bool):
        return _err("health_port 必须为整数")
    enabled = _bool_field(body, "enabled", True)
    if enabled is None:
        return _err("enabled 必须为布尔值")
    fields = dict(project_id=pid, name=name,
                  image_name=body["image_name"].strip(),
                  service_name=body["service_name"].strip(),
                  docker_context=body.get("docker_context", ""),
                  dockerfile=body.get("dockerfile", ""),
                  build_order=lists["build_order"],
                  nacos_groups=lists["nacos_groups"],
                  dubbo_services=lists["dubbo_services"],
                  snailjob_group=body.get("snailjob_group", ""),
                  health_url=body.get("health_url", ""),
                  health_port=hp,
                  log_error_keywords=lists["log_error_keywords"],
                  clear_log_dir=body.get("clear_log_dir", ""),
                  git_repo=body.get("git_repo", ""),
                  enabled=1 if enabled else 0)
    sid = dbm.create_record(current_app.config["DB_PATH"], "services", fields)
    audit("create_service", "service", sid, {"name": name, "pid": pid})
    return _ok({"id": sid}), 201


@config_bp.route("/projects/<int:pid>/services/<int:sid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_service(pid, sid):
    rec, err = _owned_or_404("services", sid, pid)
    if err:
        return err
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if "name" in body and body["name"] != rec["name"] and \
            _check_unique("services", pid, body["name"], sid):
        return _err("项目内服务名已存在")
    if "name" in body and (not isinstance(body["name"], str)
                            or not body["name"].strip()):
        return _err("name 不能为空")
    allowed = ("name", "image_name", "service_name", "docker_context",
               "dockerfile", "snailjob_group", "health_url",
               "clear_log_dir", "git_repo")
    fields = {k: v for k, v in body.items() if k in allowed}
    for k in _LIST_FIELDS_SERVICE:
        if k in body:
            v = _list_field(body, k)
            if v is None:
                return _err(f"{k} 必须为数组")
            fields[k] = v
    if "health_port" in body:
        if not isinstance(body["health_port"], int) or isinstance(body["health_port"], bool):
            return _err("health_port 必须为整数")
        fields["health_port"] = body["health_port"]
    if "enabled" in body:
        enabled = _bool_field(body, "enabled")
        if enabled is None:
            return _err("enabled 必须为布尔值")
        fields["enabled"] = 1 if enabled else 0
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "services", sid, fields)
    audit("update_service", "service", sid, {"name": rec["name"]})
    return _ok()


@config_bp.route("/projects/<int:pid>/services/<int:sid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_service(pid, sid):
    rec, err = _owned_or_404("services", sid, pid)
    if err:
        return err
    if _service_in_pipeline(pid, sid):
        return _err("服务被流水线 stages 引用，请先调整流水线")
    dbm.delete_record(current_app.config["DB_PATH"], "services", sid)
    audit("delete_service", "service", sid, {"name": rec["name"]})
    return _ok()


# ---------- 环境 ----------

@config_bp.route("/projects/<int:pid>/environments", methods=["GET"])
@require_auth
def list_environments(pid):
    return _ok([_mask_env_ssh(e)
                for e in dbm.list_environments(current_app.config["DB_PATH"], pid)])


@config_bp.route("/projects/<int:pid>/environments", methods=["POST"])
@require_auth
@require_role("admin")
def create_environment(pid):
    if not dbm.get_record(current_app.config["DB_PATH"], "projects", pid):
        return _err("项目不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    name = body.get("name", "")
    if not isinstance(name, str):
        return _err("参数类型错误")
    name = name.strip()
    if not name:
        return _err("name 必填")
    # 环境名同时用于 deploy.{name}.yaml 文件名（导出侧有 _check_safe_name 白名单），
    # 此处早校验：仅允许字母数字 _ -
    if not _SAFE_NAME.match(name):
        return _err("环境名仅允许字母数字 _ -（导出时用作文件名）")
    if _check_unique("environments", pid, name):
        return _err("项目内环境名已存在")
    is_default = _bool_field(body, "is_default", False)
    monitor_enabled = _bool_field(body, "monitor_enabled", True)
    if is_default is None or monitor_enabled is None:
        return _err("is_default/monitor_enabled 必须为布尔值")
    for k in ("registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy"):
        if k in body and not isinstance(body[k], dict):
            return _err(f"{k} 必须为对象")
    fields = dict(project_id=pid, name=name,
                  is_default=1 if is_default else 0,
                  registry=body.get("registry", {}),
                  nacos=body.get("nacos", {}),
                  mysql_snailjob=body.get("mysql_snailjob", {}),
                  ssh=body.get("ssh", {}),
                  build=body.get("build", {}),
                  deploy=body.get("deploy", {}),
                  docker_compose_path=body.get("docker_compose_path", ""),
                  maven_profile=body.get("maven_profile", ""),
                  monitor_enabled=1 if monitor_enabled else 0)
    if is_default:
        # 保持默认环境唯一（与 update_environment 对齐）：新建默认环境时取消其它默认
        for e in dbm.list_environments(current_app.config["DB_PATH"], pid):
            if e["is_default"]:
                dbm.update_record(current_app.config["DB_PATH"], "environments",
                                  e["id"], {"is_default": 0})
    eid = dbm.create_record(current_app.config["DB_PATH"], "environments", fields)
    audit("create_environment", "environment", eid, {"name": name})
    return _ok({"id": eid}), 201


@config_bp.route("/projects/<int:pid>/environments/<int:eid>", methods=["GET"])
@require_auth
def get_environment(pid, eid):
    rec, err = _owned_or_404("environments", eid, pid)
    if err:
        return err
    return _ok(rec)


@config_bp.route("/projects/<int:pid>/environments/<int:eid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_environment(pid, eid):
    rec, err = _owned_or_404("environments", eid, pid)
    if err:
        return err
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if "is_default" in body:
        is_default = _bool_field(body, "is_default")
        if is_default is None:
            return _err("is_default 必须为布尔值")
        if is_default and not rec["is_default"]:
            # 取消其它默认环境
            for e in dbm.list_environments(current_app.config["DB_PATH"], pid):
                if e["is_default"]:
                    dbm.update_record(current_app.config["DB_PATH"], "environments",
                                      e["id"], {"is_default": 0})
    if "monitor_enabled" in body:
        monitor_enabled = _bool_field(body, "monitor_enabled")
        if monitor_enabled is None:
            return _err("monitor_enabled 必须为布尔值")
    for k in ("registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy"):
        if k in body and not isinstance(body[k], dict):
            return _err(f"{k} 必须为对象")
    if "name" in body and (not isinstance(body["name"], str)
                            or not body["name"].strip()):
        return _err("name 不能为空")
    allowed = ("name", "registry", "nacos", "mysql_snailjob",
               "ssh", "build", "deploy", "docker_compose_path",
               "maven_profile")
    fields = {k: v for k, v in body.items() if k in allowed}
    if "is_default" in body:
        fields["is_default"] = 1 if _bool_field(body, "is_default") else 0
    if "monitor_enabled" in body:
        fields["monitor_enabled"] = 1 if _bool_field(body, "monitor_enabled") else 0
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "environments", eid, fields)
    audit("update_environment", "environment", eid, {"name": rec["name"]})
    return _ok()


@config_bp.route("/projects/<int:pid>/environments/<int:eid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_environment(pid, eid):
    rec, err = _owned_or_404("environments", eid, pid)
    if err:
        return err
    ok, msg = dbm.delete_environment(current_app.config["DB_PATH"], eid)
    if not ok:
        return _err(msg)
    audit("delete_environment", "environment", eid, {"name": rec["name"]})
    return _ok()


# ---------- 环境服务器绑定 ----------

@config_bp.route("/environments/<int:eid>/servers", methods=["GET"])
@require_auth
def env_servers(eid):
    if not dbm.get_record(current_app.config["DB_PATH"], "environments", eid):
        return _err("环境不存在", 404)
    return _ok([_mask_ssh_password(s)
                for s in dbm.list_env_servers(current_app.config["DB_PATH"], eid)])


@config_bp.route("/environments/<int:eid>/servers", methods=["POST"])
@require_auth
@require_role("admin")
def bind_env_server(eid):
    if not dbm.get_record(current_app.config["DB_PATH"], "environments", eid):
        return _err("环境不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    sid = body.get("server_id")
    if not isinstance(sid, int) or isinstance(sid, bool):
        return _err("server_id 必填（整数）")
    if not dbm.get_record(current_app.config["DB_PATH"], "servers", sid):
        return _err("服务器不存在", 404)
    order = len(dbm.list_env_servers(current_app.config["DB_PATH"], eid))
    dbm.bind_server(current_app.config["DB_PATH"], eid, sid, order)
    audit("bind_server", "environment", eid, {"server_id": sid})
    return _ok()


@config_bp.route("/environments/<int:eid>/servers/<int:sid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def unbind_env_server(eid, sid):
    if not dbm.get_record(current_app.config["DB_PATH"], "environments", eid):
        return _err("环境不存在", 404)
    if not dbm.get_record(current_app.config["DB_PATH"], "servers", sid):
        return _err("服务器不存在", 404)
    dbm.unbind_server(current_app.config["DB_PATH"], eid, sid)
    audit("unbind_server", "environment", eid, {"server_id": sid})
    return _ok()


@config_bp.route("/environments/<int:eid>/servers/order", methods=["PUT"])
@require_auth
@require_role("admin")
def reorder_env_servers(eid):
    if not dbm.get_record(current_app.config["DB_PATH"], "environments", eid):
        return _err("环境不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    ids = body.get("server_ids", [])
    if not isinstance(ids, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return _err("server_ids 必须为整数数组")
    missing = [sid for sid in ids
               if not dbm.get_record(current_app.config["DB_PATH"], "servers", sid)]
    if missing:
        return _err(f"服务器不存在: {missing}", 404)
    dbm.reorder_env_servers(current_app.config["DB_PATH"], eid, ids)
    audit("reorder_servers", "environment", eid, {"order": ids})
    return _ok()


# ---------- 服务器 ----------

@config_bp.route("/servers", methods=["GET"])
@require_auth
def list_servers():
    # 连接状态来自实时探测缓存（未启用监控/无缓存时为 None → 前端显示未知）
    from ..monitor import get_monitor
    mon = get_monitor()
    conn_status = mon.get_servers_status() if mon else {}
    out = []
    for s in dbm.list_records(current_app.config["DB_PATH"], "servers"):
        s = _mask_ssh_password(s)
        st = conn_status.get(s["host"])
        s["conn"] = {"ok": st["ok"], "ts": st["ts"], "error": st["error"]} if st else None
        out.append(s)
    return _ok(out)


@config_bp.route("/servers", methods=["POST"])
@require_auth
@require_role("admin")
def create_server():
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    host = body.get("host", "")
    if not isinstance(host, str):
        return _err("参数类型错误")
    host = host.strip()
    if not host:
        return _err("host 必填")
    for s in dbm.list_records(current_app.config["DB_PATH"], "servers"):
        if s["host"] == host:
            return _err("服务器已存在")
    enabled = _bool_field(body, "enabled", True)
    if enabled is None:
        return _err("enabled 必须为布尔值")
    ssh_port = body.get("ssh_port")
    if ssh_port is not None and (not isinstance(ssh_port, int)
                                 or isinstance(ssh_port, bool)):
        return _err("ssh_port 必须为整数")
    for k in ("nacos_ip", "remark"):
        if k in body and not isinstance(body[k], str):
            return _err(f"{k} 必须为字符串")
    sid = dbm.create_record(current_app.config["DB_PATH"], "servers", dict(
        host=host,
        # ssh 三态：不传 = NULL（导出时继承环境级），传了（含空串）才是覆盖
        ssh_port=ssh_port,
        ssh_user=body.get("ssh_user"),
        ssh_password=body.get("ssh_password"),
        ssh_key_file=body.get("ssh_key_file"),
        nacos_ip=body.get("nacos_ip", ""),
        remark=body.get("remark", ""),
        enabled=1 if enabled else 0))
    # 自动绑定到所有已启用监控的环境，确保新服务器立即出现在监控中心
    db = current_app.config["DB_PATH"]
    for proj in dbm.list_records(db, "projects"):
        for env in dbm.list_environments(db, proj["id"]):
            if env.get("monitor_enabled"):
                existing = dbm.list_env_servers(db, env["id"])
                order = len(existing)
                dbm.bind_server(db, env["id"], sid, order)
    audit("create_server", "server", sid, {"host": host})
    return _ok({"id": sid}), 201


@config_bp.route("/servers/<int:sid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_server(sid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "servers", sid)
    if not rec:
        return _err("记录不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if "host" in body:
        host = body["host"]
        if not isinstance(host, str) or not host.strip():
            return _err("host 不能为空")
        for s in dbm.list_records(current_app.config["DB_PATH"], "servers"):
            if s["host"] == host and s["id"] != sid:
                return _err("服务器已存在")
    allowed = ("host", "ssh_port", "ssh_user", "ssh_password", "ssh_key_file",
               "nacos_ip", "remark")
    if "ssh_port" in body and body["ssh_port"] is not None and (
            not isinstance(body["ssh_port"], int) or isinstance(body["ssh_port"], bool)):
        return _err("ssh_port 必须为整数")
    for k in ("nacos_ip", "remark"):
        if k in body and not isinstance(body[k], str):
            return _err(f"{k} 必须为字符串")
    fields = {k: v for k, v in body.items() if k in allowed}
    if "enabled" in body:
        enabled = _bool_field(body, "enabled")
        if enabled is None:
            return _err("enabled 必须为布尔值")
        fields["enabled"] = 1 if enabled else 0
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "servers", sid, fields)
    audit("update_server", "server", sid, {"host": rec["host"]})
    return _ok()


@config_bp.route("/servers/<int:sid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_server(sid):
    if not dbm.get_record(current_app.config["DB_PATH"], "servers", sid):
        return _err("记录不存在", 404)
    ok, msg = dbm.delete_server(current_app.config["DB_PATH"], sid)
    if not ok:
        return _err(msg)
    audit("delete_server", "server", sid, {})
    return _ok()


@config_bp.route("/servers/test", methods=["POST"])
@require_auth
@require_role("admin")
def test_server_conn():
    """SSH 连接测试：请求体携带待测配置（保存前后均可测）。
    服务器级字段优先，缺省回退首个绑定环境的 ssh，再回退 user=root/port=22。"""
    from ..monitor import _ssh_exec
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    host = body.get("host", "")
    if not isinstance(host, str) or not host.strip():
        return _err("host 必填")
    host = host.strip()
    ssh = {}
    # 编辑态密码留空时复用已存密码（前端传 use_stored_password）
    if body.get("use_stored_password"):
        recs = dbm.list_records(current_app.config["DB_PATH"], "servers",
                                "host=?", (host,))
        if recs and recs[0].get("ssh_password"):
            ssh["password"] = recs[0]["ssh_password"]
    ssh_cfg = body.get("ssh")
    if ssh_cfg is not None and not isinstance(ssh_cfg, dict):
        return _err("ssh 必须为对象")
    for k in ("user", "password", "key_file"):
        v = (ssh_cfg or {}).get(k)
        if v is not None:
            ssh[k] = v
    # 服务器级未配置 user 时回退首个绑定环境的 ssh 段
    if not ssh.get("user"):
        recs = dbm.list_records(current_app.config["DB_PATH"], "servers",
                                "host=?", (host,))
        if recs:
            for env in dbm.list_server_envs(current_app.config["DB_PATH"],
                                            recs[0]["id"]):
                env_ssh = env.get("ssh") or {}
                for k in ("user", "password", "key_file", "port"):
                    if k not in ssh and env_ssh.get(k):
                        ssh[k] = env_ssh[k]
    port = body.get("ssh_port") or ssh.get("port") or 22
    user = ssh.get("user") or "root"
    t0 = time.time()
    try:
        _ssh_exec(host, port, ssh, "echo ok", timeout=8)
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return _ok({"ok": False,
                    "message": f"连接失败（{ms}ms）：{str(e)[:200]}"})
    ms = int((time.time() - t0) * 1000)
    audit("test_server", "server", 0, {"host": host})
    return _ok({"ok": True,
                "message": f"连接成功（{user}@{host}:{port}，耗时 {ms}ms）"})


# ---------- 流水线 ----------

@config_bp.route("/projects/<int:pid>/pipelines", methods=["GET"])
@require_auth
def list_pipelines(pid):
    return _ok(dbm.list_pipelines(current_app.config["DB_PATH"], pid))


@config_bp.route("/projects/<int:pid>/pipelines", methods=["POST"])
@require_auth
@require_role("admin")
def create_pipeline(pid):
    if not dbm.get_record(current_app.config["DB_PATH"], "projects", pid):
        return _err("项目不存在", 404)
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    name = body.get("name", "")
    if not isinstance(name, str):
        return _err("参数类型错误")
    name = name.strip()
    if not name:
        return _err("name 必填")
    if _check_unique("pipelines", pid, name):
        return _err("项目内流水线名已存在")
    stages = _list_field(body, "stages")
    if stages is None:
        return _err("stages 必须为数组")
    err = _check_pipeline_stages(pid, stages)
    if err:
        return _err(err)
    build_first = _bool_field(body, "build_first", True)
    if build_first is None:
        return _err("build_first 必须为布尔值")
    plid = dbm.create_record(current_app.config["DB_PATH"], "pipelines", dict(
        project_id=pid, name=name,
        build_first=1 if build_first else 0,
        stages=stages))
    audit("create_pipeline", "pipeline", plid, {"name": name})
    return _ok({"id": plid}), 201


@config_bp.route("/projects/<int:pid>/pipelines/<int:plid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_pipeline(pid, plid):
    rec, err = _owned_or_404("pipelines", plid, pid)
    if err:
        return err
    body = _json_body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    fields = {}
    if "name" in body:
        name = body["name"]
        if not isinstance(name, str) or not name.strip():
            return _err("name 不能为空")
        fields["name"] = name.strip()
    if "stages" in body:
        stages = _list_field(body, "stages")
        if stages is None:
            return _err("stages 必须为数组")
        err = _check_pipeline_stages(pid, stages)
        if err:
            return _err(err)
        fields["stages"] = stages
    if "build_first" in body:
        build_first = _bool_field(body, "build_first")
        if build_first is None:
            return _err("build_first 必须为布尔值")
        fields["build_first"] = 1 if build_first else 0
    if fields:
        dbm.update_record(current_app.config["DB_PATH"], "pipelines", plid, fields)
    audit("update_pipeline", "pipeline", plid, {"name": rec["name"]})
    return _ok()


@config_bp.route("/projects/<int:pid>/pipelines/<int:plid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_pipeline(pid, plid):
    rec, err = _owned_or_404("pipelines", plid, pid)
    if err:
        return err
    dbm.delete_record(current_app.config["DB_PATH"], "pipelines", plid)
    audit("delete_pipeline", "pipeline", plid, {"name": rec["name"]})
    return _ok()


# ---------- YAML 导入导出 ----------

@config_bp.route("/config/import", methods=["POST"])
@require_auth
@require_role("admin")
def config_import():
    from ..importer import import_from_config
    results = import_from_config(current_app.config["DB_PATH"], CONFIG_DIR)
    # results 值为 (ok, message) 元组，转可序列化结构
    summary = {k: {"ok": ok, "message": msg}
               for k, (ok, msg) in results.items()}
    audit("import_config", "config", 0, summary)
    return _ok({"results": summary})


@config_bp.route("/config/export", methods=["GET"])
@require_auth
@require_role("admin")
def config_export():
    from ..exporter import export_all
    target = DEPLOY_ROOT / "data" / "export" / "config"
    written = export_all(current_app.config["DB_PATH"], target)
    audit("export_config", "config", 0, {"files": written})
    return _ok({"files": written, "dir": str(target)})
