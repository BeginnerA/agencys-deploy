"""Agent Web API：对话 / 审批单 / 建议 / 案例 / 网关配置

权限对齐平台 RBAC：
    viewer           对话（只读工具）
    operator/admin   对话（含写工具）+ 审批发布单
    admin            网关配置与建议/案例删除
"""
import json
import re
import time

from flask import (Blueprint, Response, current_app, g, jsonify, request,
                   stream_with_context)

from webapp import db as dbm
from webapp.auth import audit, require_auth, require_role
from . import config as agent_config
from . import core as core_mod
from . import llm as llm_mod
from . import advisor as advisor_mod
from . import executor as executor_mod
from . import memory as memory_mod

agent_bp = Blueprint("agent", __name__)

_SAFE_SCOPE = re.compile(r"^[A-Za-z_]+$")


def _sse(obj: dict) -> str:
    """事件序列化为 SSE data 行（UTF-8 JSON）"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _str(body, key, default="", maxlen=2000):
    v = body.get(key, default)
    return str(v)[:maxlen] if v is not None else default


# ---------- 对话 ----------

@agent_bp.route("/agent/chat/stream", methods=["POST"])
@require_auth
def chat_stream():
    """流式对话（SSE）：事件类型 text/tool/tool_result/done/error

    与 /agent/chat 同一编排循环，正文增量实时下发；异常转为 error 事件
    （配置缺失带 hint），HTTP 状态始终 200 以便前端持续读取数据流。
    """
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    message = _str(body, "message", maxlen=2000).strip()
    if not message:
        return jsonify({"error": "message 不能为空"}), 400
    session_id = _str(body, "session_id", "default", maxlen=64)
    db_path = current_app.config["DB_PATH"]

    @stream_with_context
    def gen():
        session = core_mod.AgentSession(db_path, g.user,
                                        session_id=session_id)
        try:
            try:
                audit("agent_chat", "session", 0,
                      {"session_id": session_id,
                       "length": len(message), "stream": True})
            except Exception:
                pass  # 审计失败不阻断对话流
            for ev in session.run_stream(message):
                yield _sse(ev)
        except llm_mod.LlmConfigError as e:
            yield _sse({"type": "error", "error": str(e),
                        "hint": "管理员可在智能助手页配置网关，"
                                "或设置 AGENT_LLM_* 环境变量"})
        except llm_mod.LlmHttpError as e:
            yield _sse({"type": "error", "error": str(e)})
        except llm_mod.LlmApiError as e:
            yield _sse({"type": "error", "error": str(e)})
        except Exception as e:  # 编排层意外异常：流内兜底，不让连接半途中断
            current_app.logger.exception("[agent] chat stream 异常")
            yield _sse({"type": "error", "error": f"对话服务异常: {e}"})

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@agent_bp.route("/agent/chat", methods=["POST"])
@require_auth
def chat():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    message = _str(body, "message", maxlen=2000).strip()
    if not message:
        return jsonify({"error": "message 不能为空"}), 400
    session_id = _str(body, "session_id", "default", maxlen=64)
    try:
        result = core_mod.chat_once(
            current_app.config["DB_PATH"], g.user, message,
            session_id=session_id)
    except llm_mod.LlmConfigError as e:
        return jsonify({"error": str(e),
                        "hint": "管理员可在智能助手页配置网关，或设置 AGENT_LLM_* 环境变量"}), 400
    except llm_mod.LlmHttpError as e:
        return jsonify({"error": str(e)}), 502
    except llm_mod.LlmApiError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # 编排层意外异常：不让调用方看到 500 白屏
        current_app.logger.exception("[agent] chat 异常")
        return jsonify({"error": f"对话服务异常: {e}"}), 500
    audit("agent_chat", "session", 0,
          {"session_id": session_id, "length": len(message)})
    return jsonify({"reply": result["reply"],
                    "plan_run_id": result.get("plan_run_id"),
                    "tool_calls": result.get("tool_calls", 0)})


# ---------- 审批单（Phase 3） ----------

@agent_bp.route("/agent/runs")
@require_auth
def list_runs():
    status = request.args.get("status", "")
    if status not in executor_mod.RUN_STATUSES:
        status = ""
    project_id = request.args.get("project_id", 0, type=int)
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    rows = executor_mod.list_runs(current_app.config["DB_PATH"], status,
                                  project_id, limit)
    return jsonify({"runs": rows})


@agent_bp.route("/agent/runs/<int:run_id>")
@require_auth
def run_detail(run_id):
    try:
        run = executor_mod.get_run(current_app.config["DB_PATH"], run_id)
    except executor_mod.ExecutorError:
        return jsonify({"error": "审批单不存在"}), 404
    return jsonify({"run": run})


@agent_bp.route("/agent/runs/<int:run_id>/decision", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def run_decision(run_id):
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    action = _str(body, "action", maxlen=16)
    if action not in ("approve", "reject"):
        return jsonify({"error": "action 仅支持 approve/reject"}), 400
    note = _str(body, "note", maxlen=500)
    force_window = bool(body.get("force_window"))
    db_path = current_app.config["DB_PATH"]
    try:
        run = executor_mod.decide_run(
            db_path, run_id, {"id": g.user["id"],
                              "username": g.user["username"]},
            action=action, note=note, force_window=force_window)
    except executor_mod.ExecutorError as e:
        return jsonify({"error": str(e)}), 409
    audit("agent_" + action, "agent_run", run_id,
          {"name": run.get("name"), "env": run.get("env"),
           "version": run.get("version"), "note": note,
           "force_window": force_window})
    return jsonify({"run": run})


# ---------- 建议（Phase 2） ----------

@agent_bp.route("/agent/advice")
@require_auth
def list_advice():
    scope = request.args.get("scope", "")
    if scope and not _SAFE_SCOPE.match(scope):
        return jsonify({"error": "scope 非法"}), 400
    ref_id = request.args.get("ref_id", 0, type=int)
    limit = max(1, min(request.args.get("limit", 30, type=int), 100))
    rows = advisor_mod.list_advice(current_app.config["DB_PATH"], scope,
                                   ref_id, limit)
    return jsonify({"advice": rows})


@agent_bp.route("/agent/advice/<int:advice_id>")
@require_auth
def advice_detail(advice_id):
    rec = dbm.get_record(current_app.config["DB_PATH"], "agent_advice",
                         advice_id)
    if not rec:
        return jsonify({"error": "建议不存在"}), 404
    return jsonify({"advice": rec})


@agent_bp.route("/agent/advice/<int:advice_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
def advice_delete(advice_id):
    ok = advisor_mod.delete_advice(current_app.config["DB_PATH"], advice_id)
    if not ok:
        return jsonify({"error": "建议不存在"}), 404
    audit("agent_delete_advice", "agent_advice", advice_id)
    return jsonify({"ok": True})


@agent_bp.route("/agent/advice/<int:advice_id>/to_case", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def advice_to_case(advice_id):
    """建议一键沉淀为案例草稿"""
    try:
        case_id = advisor_mod.save_case_from_advice(
            current_app.config["DB_PATH"], advice_id,
            actor={"id": g.user["id"], "username": g.user["username"]})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    audit("agent_advice_to_case", "agent_advice", advice_id,
          {"case_id": case_id})
    return jsonify({"case_id": case_id}), 201


# ---------- 案例（Phase 4 记忆） ----------

@agent_bp.route("/agent/cases")
@require_auth
def list_cases():
    keyword = request.args.get("keyword", "")
    service = request.args.get("service", "")
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    db = current_app.config["DB_PATH"]
    if keyword:
        rows = memory_mod.search_cases(db, keyword=keyword,
                                       service=service or None, limit=limit)
    else:
        rows = memory_mod.list_cases(db, service=service, limit=limit)
    return jsonify({"cases": rows})


@agent_bp.route("/agent/cases/<int:case_id>")
@require_auth
def case_detail(case_id):
    case = memory_mod.get_case(current_app.config["DB_PATH"], case_id)
    if not case:
        return jsonify({"error": "案例不存在"}), 404
    return jsonify({"case": case})


@agent_bp.route("/agent/cases", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def case_create():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    title = _str(body, "title", maxlen=200).strip()
    if not title:
        return jsonify({"error": "title 必填"}), 400
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, list):
        return jsonify({"error": "tags 必须为数组"}), 400
    case_id = memory_mod.create_case(
        current_app.config["DB_PATH"], title,
        service=_str(body, "service", maxlen=100),
        env=_str(body, "env", maxlen=50),
        version=_str(body, "version", maxlen=50),
        symptom=_str(body, "symptom", maxlen=4000),
        root_cause=_str(body, "root_cause", maxlen=4000),
        action=_str(body, "action", maxlen=4000),
        result=_str(body, "result", maxlen=200),
        lesson=_str(body, "lesson", maxlen=4000),
        tags=tags, source=_str(body, "source", "manual", maxlen=20),
        user_id=g.user["id"], username=g.user["username"])
    audit("agent_case_create", "agent_case", case_id, {"title": title})
    return jsonify({"case_id": case_id}), 201


@agent_bp.route("/agent/cases/<int:case_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def case_update(case_id):
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    fields = {}
    for k in ("title", "service", "env", "version", "symptom",
              "root_cause", "action", "result", "lesson"):
        if k in body:
            fields[k] = _str(body, k, maxlen=4000 if k not in
                             ("title", "service", "env", "version", "result")
                             else 200)
    if "tags" in body:
        if not isinstance(body["tags"], list):
            return jsonify({"error": "tags 必须为数组"}), 400
        fields["tags"] = body["tags"]
    if not fields:
        return jsonify({"error": "无有效字段"}), 400
    ok = memory_mod.update_case(current_app.config["DB_PATH"], case_id, fields)
    if not ok:
        return jsonify({"error": "案例不存在"}), 404
    audit("agent_case_update", "agent_case", case_id, {"fields": list(fields)})
    return jsonify({"ok": True})


@agent_bp.route("/agent/cases/<int:case_id>", methods=["DELETE"])
@require_auth
@require_role("admin")
def case_delete(case_id):
    ok = memory_mod.delete_case(current_app.config["DB_PATH"], case_id)
    if not ok:
        return jsonify({"error": "案例不存在"}), 404
    audit("agent_case_delete", "agent_case", case_id)
    return jsonify({"ok": True})


# ---------- 网关配置 ----------

@agent_bp.route("/agent/config")
@require_auth
def config_view():
    """查看网关状态（不泄露 api_key；仅显示 base_url 主机与模型名）"""
    settings = agent_config.get_llm_settings(current_app.config["DB_PATH"])
    base_host = ""
    try:
        from urllib.parse import urlparse
        base_host = urlparse(settings["base_url"]).netloc
    except Exception:
        pass
    return jsonify({
        "available": agent_config.llm_available(current_app.config["DB_PATH"]),
        "base_url_host": base_host,
        "model": settings.get("model", ""),
        "role": g.user["role"],
    })


@agent_bp.route("/agent/config", methods=["POST"])
@require_auth
@require_role("admin")
def config_update():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    db = current_app.config["DB_PATH"]
    base_url = _str(body, "base_url", maxlen=300)
    api_key = _str(body, "api_key", maxlen=300)
    model = _str(body, "model", maxlen=100)
    if base_url:
        base_url = base_url.rstrip("/")
        if not re.match(r"^https?://", base_url):
            return jsonify({"error": "base_url 必须以 http(s):// 开头"}), 400
    if not base_url and not api_key and not model:
        return jsonify({"error": "至少提供一个配置字段"}), 400
    agent_config.save_llm_settings(db, base_url=base_url, api_key=api_key,
                                   model=model)
    audit("agent_config_update", "setting", 0,
          {"base_url": bool(base_url), "api_key": bool(api_key),
           "model": bool(model)})
    return jsonify({"ok": True,
                    "available": agent_config.llm_available(db)})


# ---------- 会话消息（审计查询） ----------

@agent_bp.route("/agent/sessions/<session_id>/messages")
@require_auth
@require_role("admin", "operator")
def session_messages(session_id):
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    rows = dbm.list_records(
        current_app.config["DB_PATH"], "agent_messages",
        "session_id=?", (session_id[:64],), "id ASC")
    out = [{"id": r["id"], "role": r["role"], "content": r["content"],
            "created_at": r["created_at"],
            "created_by": r.get("created_by", 0)}
           for r in rows[-limit:]]
    return jsonify({"messages": out})


# ---------- 手动触发建议（供前端按钮/测试） ----------

@agent_bp.route("/agent/analyze/alert/<int:alert_id>", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def analyze_alert_route(alert_id):
    try:
        advice = advisor_mod.analyze_alert(
            current_app.config["DB_PATH"], alert_id,
            actor={"id": g.user["id"], "username": g.user["username"]})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    audit("agent_analyze_alert", "alerts", alert_id,
          {"advice_id": advice["id"]})
    return jsonify({"advice": advice})


@agent_bp.route("/agent/precheck", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def precheck_route():
    body = _json_body()
    if body is None:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    target = _str(body, "target", maxlen=100).strip()
    env = _str(body, "env", maxlen=50).strip()
    version = _str(body, "version", maxlen=50).strip()
    try:
        res = advisor_mod.run_precheck(
            current_app.config["DB_PATH"], target, env=env, version=version,
            actor={"id": g.user["id"], "username": g.user["username"]})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    audit("agent_precheck", "service", 0,
          {"target": target, "env": env, "version": version,
           "advice_id": res["id"]})
    return jsonify({"advice_id": res["id"], "passed": res["passed"],
                    "checks": res.get("checks", []), "body": res["body"]})


def register_agent(app) -> None:
    """应用工厂装配（幂等）"""
    if "agent" not in app.blueprints:
        app.register_blueprint(agent_bp, url_prefix="/api")
