"""Phase 1 编排核心：系统提示组装 + ReAct 循环 + 会话持久化（agent_messages）

会话模型：每条用户提问 = 一轮独立 ReAct；工具调用结果实时注入上下文；
到达轮次上限后强制模型做最终总结，避免无限循环。

权限注入：工具 schema 已按角色过滤；系统提示明确告知模型当前用户角色，
write 级工具仅在 operator/admin 时暴露。
"""
import json
import time

from webapp import db as dbm

from . import config as agent_config
from . import llm as llm_mod
from . import tools as tools_mod

# 系统提示（角色约束：工具不可用时禁止编造）
_SYSTEM_TPL = """你是「智能运维平台」内置的运维智能助手（Agent）。

平台能力（全部通过工具获得，禁止编造数据）：
- 项目/服务/环境/服务器配置与监控状态查询
- 发布任务记录与日志检索（search_logs + logs_context 看前后文）、
  监控指标历史（monitor_metrics：响应时间/服务器快照扩展指标）、
  告警与 SSL 证书状态
- 服务依赖拓扑与发布编排（service_topology）：查依赖全图/单服务上下游
  影响/建议发布顺序/部署快照及两次快照间版本差异
- 发布窗口与灰度策略查询、历史故障案例（长期记忆）检索
- operator 以上角色还可：告警归因分析、发布前预检、生成发布审批单；
  管理定时计划/灰度策略、灰度决策、告警闭环（发布窗口管理仅 admin）；
  告警规则与静默窗口管理（manage_alert_rules/manage_silences，
  调阈值/发版期间静默告警等诉求直接对话化）；
  日志存储治理（manage_log_storage，仅 admin：存储概况/保留期调整/
  手动清理过期日志）；
  服务依赖关系管理（manage_dependencies：记录/解除依赖，含环检测；
  多服务发布顺序建议 → service_topology metric=publish_order，
  或编排进流水线逐 stage 发布）；
  到期监控管理（manage_monitors：证书/域名/云续费/license 到期项，
  添加后自动告警提醒；domain 自动 WHOIS，ssl 可手动探测；
  全局默认阈值 config 写仅 admin）；
  采纳失败归因建议（生成回滚审批单）；立即推送巡检日报（send_digest）；
  对话内审批审批单（decide_run，与 Web 端同等效力）
- 审批单必须由人工决策：Web 审批单页或对话中用户明确指示均可，
  未经用户明确批准/驳回意图，严禁自行调用 decide_run；
  定时计划的立即执行（run）同样生成审批单

行为准则：
1. 一律用中文回答；先调用工具收集事实，再给结论，给出结论时引用证据来源
2. 当前用户: {username}（角色: {role}）——角色无权限的操作直接说明
3. 涉及发布/回滚/重启的请求 → 调用 plan_operation 生成审批单；
   立即执行定时计划 → manage_schedules 的 run 动作（同样生成审批单）；
   给定 git 地址要新建项目/部署 → manage_projects 建项目 + manage_services
   注册服务（仅 admin；git_repo 填远程 URL 首次构建自动 clone）+
   manage_environments 建环境（或 clone 复制既有项目环境含服务器绑定）+
   manage_servers 绑定目标服务器（bind 为 upsert）；多服务分阶段发布 →
   manage_pipelines 编排（stages 用服务名数组，逐 stage 顺序发布），
   部署仍走 plan_operation 审批（target 可填流水线名）；不要自行想象参数；
   用户明确说「批准/驳回 #N」时 → 先 pending_approvals/run_detail 核对该
   审批单内容（尤其发布窗口告警，需用户确认风险后 force_window=true），
   再 decide_run 执行审批
4. 回答保持简洁：默认只给出结论与关键证据；用户追问时再展开细节
5. 若某服务状态异常（告警/失败发布），可主动查询告警归因与相似案例
6. 工具失败（返回 error）时如实转述原因，并建议替代查询方式
7. 严禁编造日志内容、任务状态或监控数据；工具结果为空时说明“暂无数据”
8. 时间相关默认取最近 24 小时；需要更长窗口时先询问或明确说明
当前时间：{now}"""


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class AgentSession:
    def __init__(self, db_path: str, user: dict, session_id: str = "default"):
        self.db_path = db_path
        self.user = user or {}
        self.role = (self.user.get("role") or "viewer").lower()
        self.username = self.user.get("username") or ""
        self.user_id = int(self.user.get("id") or 0)
        self.session_id = session_id or "default"
        self._tool_ctx = type("ToolCtx", (), {
            "db_path": db_path, "user_id": self.user_id,
            "username": self.username, "role": self.role})()
        self.messages = self._load_history()

    # ---------- 会话持久化 ----------

    def _save_message(self, role: str, content: str) -> None:
        try:
            dbm.create_record(self.db_path, "agent_messages", dict(
                session_id=self.session_id[:64], role=role,
                content=(content or "")[:8000],
                created_by=self.user_id, created_at=int(time.time())))
        except Exception:
            pass  # 消息落库失败不阻断对话

    def _load_history(self) -> list:
        """恢复最近对话（user/assistant 对，最多 16 条；不恢复 tool 消息避免上下文错位）"""
        try:
            rows = dbm.list_records(
                self.db_path, "agent_messages",
                "session_id=? AND role IN ('user','assistant')",
                (self.session_id[:64],), "id DESC")[:16]
        except Exception:
            return []
        rows.reverse()
        out = []
        for r in rows:
            content = (r.get("content") or "").strip()
            if not content:
                continue
            out.append({"role": r["role"], "content": content[:4000]})
        return out[-12:]

    # ---------- 主循环 ----------

    def _system_prompt(self) -> str:
        return _SYSTEM_TPL.format(username=self.username or "访客",
                                  role=self.role,
                                  now=_now_text())

    def run(self, text: str) -> dict:
        """执行一轮对话（非流式入口）；返回 {reply, plan_run_id, tool_calls, rounds}

        等价 run_stream 的全量收集形态，供 routes/CLI/测试使用。
        """
        result = {}
        for ev in self._loop(text, stream=False):
            if ev["type"] == "done":
                result = ev["result"]
        return result

    def run_stream(self, text: str):
        """流式执行一轮对话：生成器逐段 yield 事件

        事件协议（路由/CLI 据此渲染）：
            {"type": "text", "text": str}         最终回复正文增量（打字机效果）
            {"type": "tool", "name": str}         工具开始执行
            {"type": "tool_result", "name": str, "ok": bool}  工具结束
            {"type": "done", "result": dict}    整轮结束（同 run() 返回）
        网关/编排异常原样抛出（LlmError 等），由调用方决定错误呈现。
        """
        for ev in self._loop(text, stream=True):
            yield ev

    def _prompt(self) -> list:
        return [{"role": "system", "content": self._system_prompt()}] \
            + self.messages

    def _loop(self, text: str, stream: bool):
        """主循环（run/run_stream 共享）：stream=True 时逐段 yield 正文与工具事件"""
        text = (text or "").strip()
        if not text:
            yield {"type": "done", "result": {
                "reply": "请输入问题", "plan_run_id": None,
                "tool_calls": 0, "rounds": 0}}
            return
        self._save_message("user", text)
        self.messages.append({"role": "user", "content": text[:4000]})
        schemas = tools_mod.tool_schemas(self.role)
        max_rounds = agent_config.get_max_tool_rounds()
        pending_run_id = None
        tool_count = 0
        rounds = 0
        reply = ""
        forced_summary = False
        for rounds in range(1, max_rounds + 1):
            if stream:
                # 流式：正文增量即时 yield（UI 打字机），message 收尾拼装
                msg = None
                for ev in llm_mod.stream_chat(self.db_path, self._prompt(),
                                              tools=schemas if schemas else None):
                    if ev["type"] == "delta":
                        yield {"type": "text", "text": ev["text"]}
                    elif ev["type"] == "message":
                        msg = ev["message"]
            else:
                msg = llm_mod.chat(self.db_path, self._prompt(),
                                   tools=schemas if schemas else None)
            self.messages.append(msg)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                reply = (msg.get("content") or "").strip()
                break
            # 执行本轮全部工具调用（按模型给出顺序串行）
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                if stream:
                    yield {"type": "tool", "name": name}
                result_text = tools_mod.execute_tool(self.role, name, args,
                                                     self._tool_ctx)
                tool_count += 1
                # 计划类工具：捕获生成的审批单，供 UI 直接展示审批卡片
                # （不限工具名：plan_operation / manage_schedules(run) 均产出审批单）
                if not pending_run_id:
                    try:
                        parsed = json.loads(result_text)
                        if parsed.get("run_id") and \
                                parsed.get("status") == "awaiting_approval":
                            pending_run_id = parsed["run_id"]
                    except ValueError:
                        pass
                self.messages.append({"role": "tool",
                                      "tool_call_id": tc.get("id", ""),
                                      "content": result_text})
                if stream:
                    ok = True
                    try:
                        parsed = json.loads(result_text)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            ok = False
                    except ValueError:
                        ok = False
                    yield {"type": "tool_result", "name": name, "ok": ok}
            # 达到轮次上限且模型仍未收尾 → 强制一次总结（不带工具）
            if rounds >= max_rounds and not pending_run_id:
                try:
                    tail = llm_mod.chat(self.db_path, self._prompt(), tools=None)
                    summary = (tail.get("content") or "").strip()
                    if summary:
                        self.messages.append({"role": "assistant",
                                              "content": summary})
                        reply = summary
                        forced_summary = True
                        if stream:
                            yield {"type": "text", "text": summary}
                except llm_mod.LlmError:
                    reply = ("已收集到相关信息，但模型输出中断。"
                             "可继续追问具体环节。")
            if reply or forced_summary:
                break
        if not reply:
            reply = "已完成一轮查询，但未能生成结论；请补充问题细节重试。"
        self._save_message("assistant", reply)
        self.messages.append({"role": "assistant", "content": reply})
        yield {"type": "done", "result": {
            "reply": reply, "plan_run_id": pending_run_id,
            "tool_calls": tool_count, "rounds": rounds}}



def chat_once(db_path: str, user: dict, text: str,
              session_id: str = "default") -> dict:
    """单次对话便捷入口（routes/CLI 共用）"""
    return AgentSession(db_path, user, session_id=session_id).run(text)
