"""WAR/Tomcat 实例托管路由：预览、接入、受控配置更新、人工恢复。"""
import re
import secrets
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request

from deploy import RemoteServer, merge_server_ssh

from deploy_strategies import remote_operation, tomcat_instance
from deploy_strategies.remote_operation import LockBusy, StopFailed
from deploy_strategies.tomcat_config import (TEMPLATE_VERSION,
                                             derive_war_unit,
                                             parse_war_config,
                                             validate_war_config)

from .. import db as dbm
from ..auth import audit, require_auth, require_role

tomcat_bp = Blueprint("tomcat", __name__)
_HEX64 = re.compile(r"[0-9a-f]{64}$")


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _service(pid, sid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "services", sid)
    if not rec or rec["project_id"] != pid:
        return None, _err("服务不存在", 404)
    if (rec.get("deploy_mode") or "") != "war_tomcat":
        return None, _err("该服务不是 WAR/Tomcat 发布方式")
    return rec, None


def _env_servers(pid, env_name):
    db_path = current_app.config["DB_PATH"]
    env = (dbm.get_env_by_name(db_path, pid, env_name) if env_name
           else dbm.get_default_env(db_path, pid))
    if not env:
        return None, None, _err("目标环境不存在", 404)
    servers = dbm.list_env_servers(db_path, env["id"])
    if not servers:
        return None, None, _err("目标环境未绑定服务器")
    return env, servers, None


def _model(rec):
    errors = validate_war_config(rec.get("runtime_config"))
    if errors:
        raise ValueError("；".join(errors))
    cfg = parse_war_config(rec.get("runtime_config"))
    # manifest/证据的服务标识统一取 service_name，单元名沿既有 image_name 派生规则
    service = rec.get("service_name") or rec["name"]
    unit = derive_war_unit(cfg, rec.get("image_name") or rec["name"])
    return cfg, service, unit


def _traffic_context(env, rec):
    from deploy_strategies.tomcat_restart import RestartContext
    return RestartContext(env, rec)


def _owner_digest(server, instance_id):
    from deploy_strategies.remote_operation import lock_paths, shq
    path = lock_paths(instance_id)["owner"]
    out = server.exec(
        f"test -f {shq(path)} && sha256sum {shq(path)} | "
        f"awk '{{print $1}}' || true", timeout=15).strip()
    return out if _HEX64.fullmatch(out) else ""


def _host_requests(body, servers):
    rows = body.get("hosts")
    if not isinstance(rows, list):
        return None, "hosts 必须为列表"
    by_host = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("host"), str):
            return None, "hosts 每项必须包含 host"
        by_host[row["host"]] = row
    if set(by_host) != {s["host"] for s in servers}:
        return None, "hosts 必须与环境绑定服务器完全一致"
    return by_host, None


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/preview",
                 methods=["GET"])
@require_auth
@require_role("admin")
def preview_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    env, servers, err = _env_servers(pid, request.args.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    done, failed = [], []
    for srv in servers:
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            existing = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (existing or {}).get("instance_id") or uuid4().hex
            status = (tomcat_instance.config_status(
                existing, iid, pid, env["name"], service)
                if existing else "unmanaged")
            if status == "owner_mismatch":
                failed.append({"host": srv["host"],
                               "error": "主机已有其它归属的 Tomcat 实例，拒绝接管"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            done.append({
                "host": srv["host"], "instance_id": iid, "unit": unit,
                "digest": plan["digest"], "unit_content": plan["unit_content"],
                "files": plan["files"], "manifest": plan["manifest"],
                "deletes": plan["deletes"], "templates": plan["templates"],
                "onboarded": bool(existing), "config_status": status,
            })
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/onboard",
                 methods=["POST"])
@require_auth
@require_role("admin")
def onboard_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认接入")
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    by_host, msg = _host_requests(body, servers)
    if msg:
        return _err(msg)
    done, failed = [], []
    for srv in servers:
        req = by_host[srv["host"]]
        iid = str(req.get("instance_id") or "")
        digest = str(req.get("digest") or "")
        if not iid or not _HEX64.fullmatch(digest):
            failed.append({"host": srv["host"],
                           "error": "instance_id/digest 缺失或格式错误"})
            continue
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            if tomcat_instance.read_manifest(server, cfg["app_home"]):
                failed.append({"host": srv["host"],
                               "error": "实例已接入；配置变更请走配置更新"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            if plan["digest"] != digest:
                failed.append({"host": srv["host"],
                               "error": "预览摘要已变化，请重新预览"})
                continue
            tomcat_instance.onboard(server, plan, pid, env["name"], iid, digest)
            done.append({"host": srv["host"], "instance_id": iid,
                         "unit": unit, "digest": digest})
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    audit("war_tomcat_onboard", "service", sid,
          {"env": env["name"], "ok": [h["host"] for h in done],
           "failed": failed})
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/config",
                 methods=["POST"])
@require_auth
@require_role("admin")
def update_config(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认更新配置")
    if body.get("action") != "update":
        return _err('action 必须为 "update"')
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    by_host, msg = _host_requests(body, servers)
    if msg:
        return _err(msg)
    done, failed = [], []
    for srv in servers:
        host = srv["host"]
        req = by_host[host]
        server = RemoteServer(host, merge_server_ssh(env.get("ssh") or {}, srv))
        acquired = keep_owner = False
        token = ""
        try:
            manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (manifest or {}).get("instance_id") or ""
            if not iid:
                failed.append({"host": host, "error": "实例未接入"})
                continue
            if iid != str(req.get("instance_id") or ""):
                failed.append({"host": host, "error": "实例身份不一致"})
                continue
            status = tomcat_instance.config_status(
                manifest, iid, pid, env["name"], service)
            if status == "maintenance":
                failed.append({"host": host,
                               "error": "实例处于维护状态，请先结束维护并重启"})
                continue
            if status != "managed":
                failed.append({"host": host,
                               "error": f"实例配置状态为 {status}，拒绝更新"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            if plan["digest"] != str(req.get("digest") or ""):
                failed.append({"host": host, "error": "预览摘要已变化，请重新预览"})
                continue
            token = secrets.token_hex(32)
            remote_operation.acquire(server, iid, token,
                                     "config_update", "preflight")
            acquired = True
            drift = tomcat_instance.verify_instance(
                server, cfg["app_home"], unit, manifest)
            if drift:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": "受管文件漂移"})
                acquired = False
                failed.append({"host": host,
                               "error": "受管文件或 unit 已漂移: " + ", ".join(drift)})
                continue
            ctx = _traffic_context(env, rec)
            snapshot = ctx.capture_traffic(host)
            try:
                ctx.nacos_offline(host)
            except Exception as e:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": f"摘流失败: {e}"})
                acquired = False
                failed.append({"host": host, "error": f"摘流失败，未停止实例: {e}"})
                continue
            try:
                tomcat_instance.maintenance_stop(
                    server, cfg["app_home"], unit, iid, token, snapshot)
            except StopFailed as e:
                keep_owner = True
                failed.append({"host": host,
                               "error": f"停止未确认，锁保留待人工核验: {e}"})
                continue
            except Exception as e:
                keep_owner = True
                failed.append({"host": host,
                               "error": f"维护状态写入/停止异常，锁保留: {e}"})
                continue
            try:
                tomcat_instance.apply_config(
                    server, cfg["app_home"], unit, iid, token,
                    plan["files"], plan["unit_content"], plan["digest"],
                    manifest_changes={
                        "unit": unit, "port": cfg["port"],
                        "context_path": cfg["context_path"],
                        "run_user": cfg["run_user"],
                        "template_version": TEMPLATE_VERSION,
                        "managed_files": sorted(plan["files"]),
                        "status": "maintenance",
                    })
            except Exception as e:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": str(e)})
                acquired = False
                failed.append({"host": host,
                               "error": f"配置更新失败，实例保持维护/停止: {e}"})
                continue
            remote_operation.release(server, iid, token, {
                "status": "maintenance", "operation": "config_update",
                "config_digest": plan["digest"]})
            acquired = False
            done.append({"host": host, "instance_id": iid, "unit": unit,
                         "digest": plan["digest"], "started": False,
                         "status": "maintenance"})
        except LockBusy as e:
            failed.append({"host": host, "error": str(e)})
        except Exception as e:
            failed.append({"host": host, "error": str(e)})
        finally:
            if acquired and not keep_owner:
                try:
                    remote_operation.release(server, iid, token, {
                        "status": "failed", "operation": "config_update",
                        "error": "配置更新异常中断"})
                except Exception:
                    pass
            server.close()
    audit("war_tomcat_config_update", "service", sid,
          {"env": env["name"], "ok": [h["host"] for h in done],
           "failed": failed})
    body = {"env": env["name"], "hosts": done, "failed": failed,
            "note": "配置已更新，实例未启动；请核验后结束维护并重启"}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/recovery",
                 methods=["GET"])
@require_auth
@require_role("admin")
def recovery_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    env, servers, err = _env_servers(pid, request.args.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    done, failed = [], []
    for srv in servers:
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (manifest or {}).get("instance_id") or ""
            state = remote_operation.inspect(server, iid) if iid else {}
            owner = dict(state.get("owner") or {})
            owner.pop("token", None)
            done.append({
                "host": srv["host"], "instance_id": iid, "unit": unit,
                "config_status": (tomcat_instance.config_status(
                    manifest, iid, pid, env["name"], service)
                    if manifest else "unmanaged"),
                "maintenance": (manifest or {}).get("maintenance") or {},
                "owner": owner,
                "owner_digest": _owner_digest(server, iid) if iid else "",
                "last_result": state.get("last_result") or {},
            })
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route(
    "/projects/<int:pid>/services/<int:sid>/tomcat/recovery/revoke",
    methods=["POST"])
@require_auth
@require_role("admin")
def revoke_lock(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认撤销锁")
    host = body.get("host")
    iid = str(body.get("instance_id") or "")
    digest = str(body.get("confirm_digest") or "")
    if not isinstance(host, str) or not iid or not _HEX64.fullmatch(digest):
        return _err("host/instance_id/confirm_digest 缺失或格式错误")
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    if host not in {s["host"] for s in servers}:
        return _err("host 不属于该环境", 404)
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    srv = next(s for s in servers if s["host"] == host)
    server = RemoteServer(host, merge_server_ssh(env.get("ssh") or {}, srv))
    try:
        manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
        if (manifest or {}).get("instance_id") != iid:
            return _err("实例身份不一致，拒绝撤销", 409)
        remote_operation.revoke(server, iid, digest)
    except RuntimeError as e:
        return _err(f"锁撤销失败（摘要不符或已不存在）: {e}", 409)
    finally:
        server.close()
    audit("war_tomcat_lock_revoke", "service", sid,
          {"env": env["name"], "host": host, "instance_id": iid})
    return jsonify({"host": host, "revoked": True})
