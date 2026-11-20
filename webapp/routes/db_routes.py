"""平台库快照与恢复 API（全部 admin）

边界放在这一层：HTTP 面只负责鉴权、把入参形状挡住、把 db_backup 的异常翻成
状态码 + 中文说明；快照/轮转/校验的真实判据都在 `webapp/db_backup.py`，
这里不复制第二份（保留份数的范围、校验值算法都由那一侧单点定义）。

错误体只有 `error` 一个字段装中文说明：api.js 把 `data.error` 直接当提示文案
（`error` 是机器码的那一族响应，是给需要分支的前端用的，这里没有分支需求）。

恢复是「暂存 + 重启生效」两段式，所以这一层的响应必须把 `restart_required`
说清楚，并提供重启前的撤回入口。
"""
from flask import Blueprint, current_app, jsonify, request

from .. import db_backup
from ..auth import audit, require_auth, require_role

db_bp = Blueprint("db", __name__)

# 请求字段名 → platform_settings 键：只认这两项，其余键忽略
_RETENTION_FIELDS = {"keep": db_backup.KEEP_KEY,
                     "audit_retention_days": db_backup.AUDIT_DAYS_KEY}


def _fail(message: str, code: int) -> tuple:
    return jsonify({"error": message}), code


@db_bp.route("/db/backups", methods=["GET"])
@require_auth
@require_role("admin")
def list_backups():
    """快照台账 + 待恢复标记 + 当前保留策略（前端一屏要用的三样一起给）"""
    db_path = current_app.config["DB_PATH"]
    pending = db_backup.pending_restore(db_path)
    return jsonify({
        "backups": db_backup.list_snapshots(db_path),
        "pending": None if not pending else {
            "name": pending.get("name", ""),
            "kind": pending.get("kind", ""),
            "staged_at": pending.get("staged_at", 0),
            "restart_required": True,
        },
        "retention": db_backup.retention_settings(db_path),
    })


@db_bp.route("/db/backups", methods=["POST"])
@require_auth
@require_role("admin")
def create_backup():
    """一键快照：不必停平台，活库有未提交写事务也拍得下来（只读 VACUUM INTO）"""
    body = request.get_json(silent=True)
    body = body if isinstance(body, dict) else {}
    label = body.get("label", "")
    if not isinstance(label, str):
        return _fail("label 必须是字符串", 400)
    db_path = current_app.config["DB_PATH"]
    try:
        rec = db_backup.snapshot(db_path, kind="manual", label=label.strip()[:200])
    except Exception as e:
        # 磁盘满/权限/库被占用：给前端可读的失败，并留一条审计（失败的快照同样要查）
        audit("db.snapshot", "db", 0, {"error": str(e)})
        return _fail(f"快照失败：{e}", 500)
    audit("db.snapshot", "db", 0, {"name": rec["name"], "label": rec["label"],
                                   "size_bytes": rec["size_bytes"]})
    return jsonify(rec)


@db_bp.route("/db/backups/<path:name>/restore", methods=["POST"])
@require_auth
@require_role("admin")
def stage_restore(name: str):
    """暂存一份快照待重启后换库（此刻活库未动，可撤回）"""
    db_path = current_app.config["DB_PATH"]
    try:
        res = db_backup.stage_restore(db_path, name)
    except ValueError as e:
        # 名字来自 URL：没登记过（含带斜杠、改名、拼接串）一律挡回，不拿去拼路径
        return _fail(f"快照名不可用：{e}", 400)
    except db_backup.RestoreError as e:
        return _fail(f"快照不可用：{e}", 409)
    except Exception as e:
        audit("db.restore_stage", "db", 0, {"name": name, "error": str(e)})
        return _fail(f"暂存恢复失败：{e}", 500)
    audit("db.restore_stage", "db", 0, {"name": name, "kind": res.get("kind")})
    return jsonify(res)


@db_bp.route("/db/restore-pending", methods=["DELETE"])
@require_auth
@require_role("admin")
def cancel_restore():
    """撤回待恢复（重启前有效）；没有待恢复项时是幂等空操作"""
    pending = db_backup.cancel_pending_restore(current_app.config["DB_PATH"])
    audit("db.restore_cancel", "db", 0,
          {"name": (pending or {}).get("name", ""), "cancelled": bool(pending)})
    return jsonify({"cancelled": bool(pending),
                    "name": (pending or {}).get("name", "")})


@db_bp.route("/db/retention", methods=["GET"])
@require_auth
@require_role("admin")
def get_retention():
    return jsonify(db_backup.retention_settings(current_app.config["DB_PATH"]))


@db_bp.route("/db/retention", methods=["PUT"])
@require_auth
@require_role("admin")
def put_retention():
    """改保留策略：下一轮周期维护（采集线程每小时一次）即见效，不必等下次快照"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _fail("请求体必须为 JSON 对象", 400)
    values = {_RETENTION_FIELDS[field]: body[field]
              for field in _RETENTION_FIELDS if field in body}
    if not values:
        return _fail(f"没有可改的项（可传：{'、'.join(_RETENTION_FIELDS)}）", 400)
    try:
        settings = db_backup.set_retention(current_app.config["DB_PATH"], values)
    except ValueError as e:
        return _fail(str(e), 400)
    audit("db.retention_update", "db", 0, {
        field: body[field] for field in _RETENTION_FIELDS if field in body})
    return jsonify(settings)
