"""实例文件、归属清单、管理员预览与接入/配置事务；不做发布切换与启动。"""
import base64
import hashlib
import json
import secrets
import time

from .remote_operation import (acquire, guarded, release, shq,
                               stop_and_confirm)
from .tomcat_config import (TEMPLATE_VERSION, config_digest,
                            render_instance_files, render_war_unit)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_path(app_home: str) -> str:
    return f"{app_home}/.smartops-instance.json"


def read_templates(server, tomcat_home: str) -> dict:
    result = {}
    for name in ("web.xml", "catalina.properties", "logging.properties"):
        raw = server.exec(
            f"base64 -w0 {tomcat_home}/conf/{name} 2>/dev/null || true", timeout=15).strip()
        if raw:
            result[name] = base64.b64decode(raw).decode("utf-8", "replace")
    return result


def read_manifest(server, app_home: str) -> dict:
    raw = server.exec(
        f"base64 -w0 {manifest_path(app_home)} 2>/dev/null || true", timeout=15).strip()
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def resolve_instance_id(server, app_home: str) -> str:
    """归属身份只从归属文件解析（随机 ID 由接入事务生成），不在本地推导。

    发布、重启、配置、恢复一律经此取同一远端身份；实例未接入或清单缺
    instance_id 时直接失败，绝不生成临时身份（避免绕过管理员接入事务）。
    """
    manifest = read_manifest(server, app_home)
    instance_id = (manifest or {}).get("instance_id") or ""
    if not instance_id:
        raise RuntimeError("实例未接入或归属清单缺少 instance_id")
    return instance_id


def build_manifest(cfg, service, project_id, env_name, unit, instance_id,
                   template_version, digest, files=None, unit_content="") -> dict:
    files = files or {}
    managed = sorted(files) if files else sorted(render_instance_files(
        cfg, service, {"web.xml": "x", "catalina.properties": "x",
                       "logging.properties": "x"}))
    return {
        "schema_version": 1,
        "instance_id": instance_id,
        "project_id": project_id,
        "service": service,
        "env": env_name,
        "unit": unit,
        "app_home": cfg["app_home"],
        "port": cfg["port"],
        "context_path": cfg["context_path"],
        "run_user": cfg["run_user"],
        "template_version": template_version,
        "config_digest": digest,
        "managed_files": managed,
        "file_digests": {k: _sha(v) for k, v in sorted(files.items())},
        "unit_digest": _sha(unit_content),
        "maintenance": {"active": False, "traffic_snapshot": None},
        "status": "ready",
    }


def verify_instance(server, app_home: str, unit: str, manifest: dict) -> list:
    """按归属清单核对实际受管文件与 unit；返回不一致项，空列表表示未漂移。"""
    expected = manifest.get("file_digests") or {}
    if not expected:
        return ["归属清单缺少文件摘要"]
    bad = []
    for rel, digest in sorted(expected.items()):
        try:
            actual = server.exec(
                f"sha256sum {shq(app_home + '/' + rel)} 2>/dev/null | "
                f"awk '{{print $1}}'", timeout=15).strip()
        except Exception:
            actual = ""
        if actual != digest:
            bad.append(rel)
    expected_unit = manifest.get("unit_digest") or ""
    try:
        actual_unit = server.exec(
            f"sha256sum {shq('/etc/systemd/system/' + unit + '.service')} "
            f"2>/dev/null | awk '{{print $1}}'", timeout=15).strip()
    except Exception:
        actual_unit = ""
    if not expected_unit or actual_unit != expected_unit:
        bad.append("unit.service")
    return bad


def preview(server, cfg, service, project_id, env_name, unit, instance_id) -> dict:
    templates = read_templates(server, cfg["tomcat_home"])
    files = render_instance_files(cfg, service, templates)
    unit_content = render_war_unit(cfg, service)
    digest = config_digest(cfg, files, TEMPLATE_VERSION, unit_content)
    manifest = build_manifest(cfg, service, project_id, env_name, unit,
                              instance_id, TEMPLATE_VERSION, digest,
                              files={f"instance/{k}": v for k, v in files.items()},
                              unit_content=unit_content)
    return {
        "digest": digest,
        "unit": unit,
        "unit_content": unit_content,
        "files": {f"instance/{k}": v for k, v in files.items()},
        "manifest": manifest,
        "deletes": ["instance/webapps/*", "instance/work/*", "instance/temp/*"],
        "templates": sorted(templates),
    }


# ---- Task 7：接入与配置事务（受锁、可恢复） ----

class StalePreview(ValueError):
    """预览摘要与当前 plan 不一致；拒绝接入，要求重新预览。"""


def maintenance_path(app_home: str) -> str:
    return f"{app_home}/.smartops-maintenance.json"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _write_file(server, instance_id, token, target, content):
    # 内容经 stdin_rest 随令牌同行进入 stdin：令牌占首行，其余由 cat 消费，
    # 配置文本不进命令行（超长/特殊字符也安全）
    guarded(server, instance_id, token,
            f"mkdir -p {shq(target.rsplit('/', 1)[0])} && "
            f"cat > {shq(target)}.tmp && mv -f {shq(target)}.tmp {shq(target)}",
            timeout=30, stdin_rest=content)


def _read_json_file(server, path):
    raw = server.exec(f"base64 -w0 {shq(path)} 2>/dev/null || true",
                      timeout=15).strip()
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def read_maintenance(server, app_home: str) -> dict:
    """读取维护标志与流量快照；缺失或解析失败返回 {}（只读，不抛错）。"""
    return _read_json_file(server, maintenance_path(app_home))


def update_manifest(server, app_home, instance_id, token, **changes) -> dict:
    """先读后写更新归属清单；缺失或归属不符一律拒绝（fail-closed）。"""
    manifest = read_manifest(server, app_home)
    if not manifest:
        raise RuntimeError("实例归属清单不存在，拒绝更新")
    if manifest.get("instance_id") != instance_id:
        raise RuntimeError("实例归属标识不一致，拒绝更新")
    manifest.update(changes)
    _write_file(server, instance_id, token, manifest_path(app_home),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return manifest


def _assert_instance_rel(rel: str) -> None:
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "instance" or parts[1] != "conf" \
            or ".." in parts or "" in parts:
        raise ValueError(f"配置事务拒绝非 instance/conf/ 相对路径: {rel!r}")


def onboard(server, plan, project_id, env_name, instance_id, preview_digest):
    """接入：写实例配置、unit 与归属清单，只 daemon-reload + enable，绝不启动。

    plan 来自 :func:`preview`：摘要不符抛 :class:`StalePreview`（先于任何远端
    动作）；远端已有其它归属的清单时拒绝覆盖（占锁前先读）。project_id 与
    env_name 已由 plan["manifest"] 携带，保留参数只为签名兼容。令牌在本地
    生成、只走 stdin；任何一步失败都先释放占用（fail 结果落盘）再抛出。
    """
    if plan["digest"] != preview_digest:
        raise StalePreview("预览摘要已变化，请重新预览")
    existing = read_manifest(server, plan["manifest"]["app_home"])
    if existing and existing.get("instance_id") != instance_id:
        raise RuntimeError("实例已有其它归属，拒绝覆盖")
    token = secrets.token_hex(32)
    acquire(server, instance_id, token, "onboard", "preflight")
    try:
        app = plan["manifest"]["app_home"]
        for rel, content in sorted(plan["files"].items()):
            _write_file(server, instance_id, token, f"{app}/{rel}", content)
        unit = plan["unit"]
        _write_file(server, instance_id, token,
                    f"/etc/systemd/system/{unit}.service", plan["unit_content"])
        manifest = dict(plan["manifest"], status="ready")
        _write_file(server, instance_id, token, manifest_path(app),
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        guarded(server, instance_id, token,
                f"systemctl daemon-reload && systemctl enable {shq(unit)}.service",
                timeout=60)
    except Exception as e:
        try:
            release(server, instance_id, token,
                    {"status": "failed", "operation": "onboard", "error": str(e)})
        except Exception:
            pass
        raise
    release(server, instance_id, token, {"status": "ready", "operation": "onboard"})
    return manifest


def maintenance_stop(server, app_home, unit, instance_id, token, traffic_snapshot):
    """维护标志先于停止落盘；停止失败保留标志，交人工核验后再撤销。"""
    snapshot = {"active": True, "traffic_snapshot": traffic_snapshot or {},
                "started_at": int(time.time())}
    _write_file(server, instance_id, token, maintenance_path(app_home),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    update_manifest(server, app_home, instance_id, token,
                    maintenance=snapshot, status="maintenance")
    stop_and_confirm(server, instance_id, token, unit)
    return snapshot


def clear_maintenance(server, app_home, instance_id, token) -> None:
    """重启成功且流量恢复后才调用；文件与清单同时回到非维护态。"""
    guarded(server, instance_id, token,
            f"rm -f {shq(maintenance_path(app_home))}", timeout=15)
    update_manifest(server, app_home, instance_id, token,
                    maintenance={"active": False, "traffic_snapshot": None},
                    status="ready")


def apply_config(server, app_home, unit, instance_id, token, files,
                 unit_content, digest, manifest_changes=None):
    """配置事务：备份 → 写入 → daemon-reload → 更新清单摘要；失败整体还原。

    ``files`` 的键必须是 ``instance/conf/...`` 相对路径（值来自 preview 的
    files），不接受任意绝对路径；全程不启动实例（维护标志仍由
    maintenance_stop 持有），任一步失败恢复旧配置、旧清单与旧 unit。
    备份步骤自身失败时不存在可还原的备份：直接重抛原始异常，远端一字不动
    （还原链的 rm -rf 会先毁掉现网唯一 conf）。
    """
    revision = digest[:12]
    backup = f"{app_home}/.config-backup/{revision}"
    manifest_backup = f"{backup}/instance.json"
    backup_ready = False
    try:
        guarded(server, instance_id, token,
                f"mkdir -p {shq(backup)} && "
                f"cp -a {shq(app_home + '/instance/conf')} {shq(backup + '/conf')} && "
                f"cp -a {shq(manifest_path(app_home))} {shq(manifest_backup)} && "
                f"cp -a {shq('/etc/systemd/system/' + unit + '.service')} "
                f"{shq(backup + '/unit.service')}", timeout=60)
        backup_ready = True
        for rel, content in sorted(files.items()):
            _assert_instance_rel(rel)
            _write_file(server, instance_id, token, f"{app_home}/{rel}", content)
        _write_file(server, instance_id, token,
                    f"/etc/systemd/system/{unit}.service", unit_content)
        guarded(server, instance_id, token,
                f"systemctl daemon-reload && systemctl enable {shq(unit)}.service",
                timeout=60)
        if manifest_changes:
            update_manifest(
                server, app_home, instance_id, token,
                **dict(manifest_changes, config_digest=digest,
                       file_digests={k: _sha(v) for k, v in files.items()},
                       unit_digest=_sha(unit_content)))
    except Exception:
        if not backup_ready:
            raise
        guarded(server, instance_id, token,
                f"rm -rf {shq(app_home + '/instance/conf')} && "
                f"cp -a {shq(backup + '/conf')} {shq(app_home + '/instance/conf')} && "
                f"cp -a {shq(manifest_backup)} {shq(manifest_path(app_home))} && "
                f"cp -a {shq(backup + '/unit.service')} "
                f"{shq('/etc/systemd/system/' + unit + '.service')} && "
                f"systemctl daemon-reload", timeout=60)
        raise


# ---------- 监控观测纯函数（无 SSH/IO，供 webapp.monitor 聚合调用） ----------

# 聚合优先级：归属异常 > 未纳管 > 维护中 > 受管（任一主机异常即整体异常）
_CONFIG_STATUS_ORDER = ("owner_mismatch", "unmanaged", "maintenance", "managed")


def config_status(manifest: dict, instance_id: str, project_id, env_name: str,
                  service: str) -> str:
    """归属清单 × 平台期望 → 配置状态；manifest 为空表示从未接入归属"""
    if not manifest:
        return "unmanaged"
    if (manifest.get("instance_id") != instance_id
            or manifest.get("project_id") != project_id
            or manifest.get("env") != env_name
            or manifest.get("service") != service):
        return "owner_mismatch"
    if (manifest.get("maintenance") or {}).get("active"):
        return "maintenance"
    return "managed"


def aggregate_config_status(rows: list) -> str:
    present = {(row or {}).get("config_status") or "unmanaged" for row in rows}
    for value in _CONFIG_STATUS_ORDER:
        if value in present:
            return value
    return "unmanaged"


def aggregate_operation_state(rows: list) -> str:
    if any((row or {}).get("operation_state") == "in_progress" for row in rows):
        return "in_progress"
    return "idle"
