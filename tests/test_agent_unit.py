# tests/test_agent_unit.py
"""Agent 模块单元测试：Schema/记忆/LLM 网关/工具注册表/审批执行/建议引擎/MCP"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm

USER = {"id": 1, "username": "tester", "role": "admin"}


class AgentBase(unittest.TestCase):
    """公共夹具：空库 + 项目 + 服务 + 环境 + 服务器"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))
        self.svc_id = dbm.create_record(self.db, "services", dict(
            project_id=self.proj_id, name="auth", image_name="auth",
            service_name="hqb-auth", docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        self.env_id = dbm.create_record(self.db, "environments", dict(
            project_id=self.proj_id, name="prod", is_default=1,
            registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
            build="{}", deploy="{}", monitor_enabled=1))
        self.server_id = dbm.create_record(self.db, "servers", dict(
            host="10.0.0.1", ssh_port=22, ssh_user="root", ssh_password="x",
            nacos_ip="", remark="", enabled=1))
        dbm.create_record(self.db, "env_servers", dict(
            env_id=self.env_id, server_id=self.server_id, order_no=0))

    def make_task(self, type_="project", version="1.1.0",
                  previous_version="1.0.0", status="pending",
                  finished_at=None, log_file="", name=None):
        task_id = dbm.create_task(self.db, self.proj_id, type_, self.svc_id,
                                  name or "auth", "prod", version,
                                  previous_version, 1)
        if status != "pending":
            fields = {"status": status}
            if finished_at:
                fields["finished_at"] = finished_at
            if log_file:
                fields["log_file"] = log_file
            dbm.update_task(self.db, task_id, **fields)
        return task_id


class TestAgentSchema(AgentBase):
    def test_tables_created(self):
        conn = dbm.get_conn(self.db)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('agent_runs','agent_messages','agent_advice',"
                "'agent_cases')").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 4)

    def test_messages_roundtrip(self):
        mid = dbm.create_record(self.db, "agent_messages", dict(
            session_id="s1", role="user", content="你好",
            created_by=1, created_at=int(time.time())))
        rec = dbm.get_record(self.db, "agent_messages", mid)
        self.assertEqual(rec["role"], "user")
        rows = dbm.list_records(self.db, "agent_messages",
                                "session_id=? AND role IN ('user','assistant')",
                                ("s1",), "id ASC")
        self.assertEqual(len(rows), 1)

    def test_advice_roundtrip(self):
        aid = dbm.create_record(self.db, "agent_advice", dict(
            scope="canary", ref_id=7, title="灰度建议", body="建议继续",
            evidence={"decision": "continue", "confidence": 0.7},
            model="rule-based", created_at=int(time.time()),
            created_by=0, created_by_name="advisor"))
        rec = dbm.get_record(self.db, "agent_advice", aid)
        self.assertEqual(rec["scope"], "canary")
        # evidence 在 JSON_FIELDS：应为 dict（或可被 json 解析的字符串）
        ev = rec["evidence"]
        if isinstance(ev, str):
            ev = json.loads(ev)
        self.assertEqual(ev["decision"], "continue")


class TestMemory(AgentBase):
    def _mk(self, title, symptom, service="", tags=None, **kw):
        return dbm.create_record(self.db, "agent_cases", dict(
            title=title, service=service, symptom=symptom,
            root_cause=kw.get("root_cause", ""), action=kw.get("action", ""),
            result=kw.get("result", "resolved"), lesson=kw.get("lesson", ""),
            tags=tags or [], source=kw.get("source", "manual"),
            advice_id=kw.get("advice_id", 0), created_by=1,
            created_by_name="tester", created_at=int(time.time()),
            updated_at=int(time.time())))

    def test_case_crud_and_tags(self):
        from agent.memory import create_case, get_case, list_cases, \
            update_case, delete_case
        cid = create_case(self.db, "磁盘写满", service="auth", env="prod",
                          tags=["磁盘", "发布失败"], symptom="df 100%",
                          user_id=1, username="tester")
        rec = get_case(self.db, cid)
        self.assertEqual(rec["service"], "auth")
        tags = rec["tags"]
        if isinstance(tags, str):
            tags = json.loads(tags)
        self.assertIn("磁盘", tags)
        self.assertEqual(len(list_cases(self.db)), 1)
        self.assertTrue(update_case(self.db, cid, {"result": "failed"}))
        self.assertEqual(get_case(self.db, cid)["result"], "failed")
        self.assertTrue(delete_case(self.db, cid))
        self.assertIsNone(get_case(self.db, cid))

    def test_search_ranks_similar_higher(self):
        self._mk("数据库连接池耗尽导致超时", "大量连接超时 waiting",
                 service="order", tags=["mysql", "连接池"])
        self._mk("前端按钮样式微调", "页面字体改小", service="web")
        from agent.memory import search_cases
        rows = search_cases(self.db, keyword="数据库连接超时", limit=5)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 1)  # 相似度更高的排前面
        self.assertGreater(rows[0]["score"], 0)

    def test_search_by_service(self):
        self._mk("订单接口 500", "500 错误", service="order")
        self._mk("支付回调失败", "回调 500", service="pay")
        from agent.memory import list_cases, search_cases
        self.assertEqual(len(list_cases(self.db, service="order")), 1)
        self.assertEqual(len(search_cases(self.db, service="order")), 1)

    def test_list_by_tag_and_search_exclude(self):
        self._mk("磁盘写满", "df 100%", service="auth", tags=["磁盘", "OOM"])
        self._mk("连接池耗尽", "timeout", service="order", tags=["mysql"])
        from agent.memory import list_cases, search_cases
        self.assertEqual(len(list_cases(self.db, tag="磁盘")), 1)
        self.assertEqual(list_cases(self.db, tag="磁盘")[0]["title"], "磁盘写满")
        self.assertEqual(len(list_cases(self.db, tag="不存在的标签")), 0)
        # 相似召回排除自身（详情页"相似案例"推荐）
        rows = search_cases(self.db, keyword="磁盘", exclude_id=1, limit=5)
        self.assertNotIn(1, [r["id"] for r in rows])
        # 无关键字时同样排除自身
        rows2 = search_cases(self.db, exclude_id=1, limit=5)
        self.assertEqual([r["id"] for r in rows2], [2])


class TestLlmGateway(AgentBase):
    def test_chat_requires_config(self):
        from agent import llm as llm_mod
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LLM_BASE_URL", None)
            os.environ.pop("AGENT_LLM_API_KEY", None)
            with self.assertRaises(llm_mod.LlmConfigError):
                llm_mod.chat(self.db, [{"role": "user", "content": "hi"}])

    def test_chat_success_and_normalization(self):
        from agent import llm as llm_mod
        fake = mock.Mock()
        fake.status_code = 200
        fake.json.return_value = {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "list_projects",
                                         "arguments": "{}"}}]}}]}
        with mock.patch("requests.post", return_value=fake) as post:
            with mock.patch.dict(os.environ, {
                    "AGENT_LLM_BASE_URL": "https://x/v1",
                    "AGENT_LLM_API_KEY": "sk-test"}, clear=False):
                msg = llm_mod.chat(self.db, [])
        # tool_calls 缺省归一化为空列表
        self.assertEqual(msg["role"], "assistant")
        self.assertIsNone(msg["content"])
        self.assertEqual(len(msg["tool_calls"]), 1)
        # 端点拼接：base_url 不以 /chat/completions 结尾时自动追加
        called_url = post.call_args[0][0]
        self.assertTrue(called_url.endswith("/chat/completions"))

    def test_chat_http_error_mapping(self):
        from agent import llm as llm_mod
        fake = mock.Mock()
        fake.status_code = 401
        fake.json.return_value = {"error": {"message": "invalid key"}}
        with mock.patch("requests.post", return_value=fake):
            with mock.patch.dict(os.environ, {
                    "AGENT_LLM_BASE_URL": "https://x/v1",
                    "AGENT_LLM_API_KEY": "bad"}, clear=False):
                with self.assertRaises(llm_mod.LlmApiError) as cm:
                    llm_mod.chat(self.db, [])
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("鉴权", str(cm.exception))

    def test_parse_tool_calls(self):
        from agent import llm as llm_mod
        msg = {"tool_calls": [
            {"function": {"name": "a", "arguments": '{"x":1}'}},
            {"function": {"name": "b", "arguments": {"y": 2}}}]}
        out = llm_mod.parse_tool_calls(msg)
        self.assertEqual(out, [("a", {"x": 1}), ("b", {"y": 2})])
        bad = {"tool_calls": [{"function": {"name": "a",
                                            "arguments": "{oops"}}]}
        with self.assertRaises(llm_mod.LlmApiError):
            llm_mod.parse_tool_calls(bad)

    def test_config_env_precedence_over_db(self):
        from agent import config as agent_config
        agent_config.save_llm_settings(self.db, base_url="https://db/v1",
                                       api_key="db-key", model="db-model")
        s = agent_config.get_llm_settings(self.db)
        self.assertEqual(s["base_url"], "https://db/v1")
        with mock.patch.dict(os.environ, {
                "AGENT_LLM_BASE_URL": "https://env/v1",
                "AGENT_LLM_API_KEY": "env-key"}, clear=False):
            s2 = agent_config.get_llm_settings(self.db)
        self.assertEqual(s2["base_url"], "https://env/v1")
        self.assertTrue(agent_config.llm_available(self.db))

    def test_stream_chat_sse_parse_and_tool_assembly(self):
        from agent import llm as llm_mod
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "你"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "好"}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "list_projects",
                              "arguments": ""}}]}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '{"x"'}}]}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ": 1}"}}]}}]}),
            "data: [DONE]",
        ]
        fake = mock.Mock()
        fake.status_code = 200
        fake.headers = {"Content-Type": "text/event-stream"}
        fake.iter_lines.return_value = iter(lines)
        with mock.patch("requests.post", return_value=fake) as post:
            with mock.patch.dict(os.environ, {
                    "AGENT_LLM_BASE_URL": "https://x/v1",
                    "AGENT_LLM_API_KEY": "sk-test"}, clear=False):
                evs = list(llm_mod.stream_chat(self.db, []))
        # stream: true 已置入请求体
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        deltas = "".join(e["text"] for e in evs if e["type"] == "delta")
        self.assertEqual(deltas, "你好")
        msg = [e for e in evs if e["type"] == "message"][-1]["message"]
        self.assertEqual(msg["content"], "你好")
        call = msg["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "list_projects")
        # 分片 arguments 已拼装回完整 JSON
        self.assertEqual(json.loads(call["function"]["arguments"]), {"x": 1})

    def test_stream_chat_downgrade_when_not_sse(self):
        # 网关忽略 stream 参数直接回完整 JSON：无 delta，仅 message 事件
        from agent import llm as llm_mod
        fake = mock.Mock()
        fake.status_code = 200
        fake.headers = {"Content-Type": "application/json"}
        fake.json.return_value = {"choices": [{"message": {
            "content": "完整回复"}}]}
        with mock.patch("requests.post", return_value=fake):
            with mock.patch.dict(os.environ, {
                    "AGENT_LLM_BASE_URL": "https://x/v1",
                    "AGENT_LLM_API_KEY": "sk-test"}, clear=False):
                evs = list(llm_mod.stream_chat(self.db, []))
        self.assertEqual([e["type"] for e in evs], ["message"])
        self.assertEqual(evs[0]["message"]["content"], "完整回复")

    def test_stream_chat_http_error(self):
        from agent import llm as llm_mod
        fake = mock.Mock()
        fake.status_code = 401
        fake.json.return_value = {"error": {"message": "invalid key"}}
        with mock.patch("requests.post", return_value=fake):
            with mock.patch.dict(os.environ, {
                    "AGENT_LLM_BASE_URL": "https://x/v1",
                    "AGENT_LLM_API_KEY": "bad"}, clear=False):
                with self.assertRaises(llm_mod.LlmApiError) as cm:
                    list(llm_mod.stream_chat(self.db, []))
        self.assertEqual(cm.exception.status, 401)


class TestTools(AgentBase):
    def test_role_filtering(self):
        from agent import tools as tools_mod
        viewer_names = {t["name"] for t in tools_mod.visible_tools("viewer")}
        admin_names = {t["name"] for t in tools_mod.visible_tools("admin")}
        self.assertIn("list_projects", viewer_names)
        self.assertIn("plan_operation", admin_names)
        self.assertNotIn("plan_operation", viewer_names)
        self.assertIn("ack_alert", admin_names)
        self.assertNotIn("ack_alert", viewer_names)
        # OpenAI schema 形态
        schemas = tools_mod.tool_schemas("viewer")
        self.assertTrue(all(s["type"] == "function" for s in schemas))

    def _ctx(self, role="admin"):
        return type("Ctx", (), {"db_path": self.db, "user_id": 1,
                                "username": "tester", "role": role})()

    def test_execute_read_and_unknown(self):
        from agent import tools as tools_mod
        out = tools_mod.execute_tool("viewer", "list_projects", {},
                                     self._ctx("viewer"))
        data = json.loads(out)
        self.assertIsInstance(data["projects"], list)
        self.assertEqual(len(data["projects"]), 1)
        err = json.loads(tools_mod.execute_tool("viewer", "nope", {},
                                                self._ctx("viewer")))
        self.assertIn("error", err)

    def test_write_denied_for_viewer(self):
        from agent import tools as tools_mod
        out = json.loads(tools_mod.execute_tool(
            "viewer", "plan_operation",
            {"op": "deploy", "target": "auth", "env": "prod",
             "version": "1.2.0"}, self._ctx("viewer")))
        self.assertIn("error", out)
        self.assertIn("operator/admin", out["error"])

    def test_case_update_full_fields(self):
        # 工具入口与 Web PUT 字段白名单对齐（含 service/env/version/symptom）
        from agent import tools as tools_mod
        from agent import memory as memory_mod
        cid = memory_mod.create_case(self.db, "初始案例", service="auth",
                                     symptom="初始症状", user_id=1,
                                     username="tester")
        out = json.loads(tools_mod.execute_tool(
            "admin", "case_update",
            {"case_id": cid, "service": "order", "env": "staging",
             "version": "2.0.0", "symptom": "更新后的症状"},
            self._ctx("admin")))
        self.assertNotIn("error", out)
        rec = memory_mod.get_case(self.db, cid)
        self.assertEqual(rec["service"], "order")
        self.assertEqual(rec["env"], "staging")
        self.assertEqual(rec["version"], "2.0.0")
        self.assertEqual(rec["symptom"], "更新后的症状")

    def test_plan_operation_tool_creates_run(self):
        from agent import tools as tools_mod
        out = json.loads(tools_mod.execute_tool(
            "operator", "plan_operation",
            {"op": "deploy", "target": "auth", "env": "prod",
             "version": "1.2.0", "intent": "发布 auth 1.2.0"},
            self._ctx("operator")))
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertGreater(out["run_id"], 0)
        rec = dbm.get_record(self.db, "agent_runs", out["run_id"])
        self.assertEqual(rec["status"], "awaiting_approval")
        self.assertIn("cli", out["plan"])


class TestExecutor(AgentBase):
    def _plan(self, op="deploy", **kw):
        from agent.executor import plan_operation
        return plan_operation(self.db, USER, op=op, target=kw.get(
            "target", "auth"), env=kw.get("env", "prod"),
            version=kw.get("version", "1.2.0"),
            previous_version=kw.get("previous_version", ""),
            canary=kw.get("canary", False), auto_version=kw.get(
                "auto_version", False), intent=kw.get("intent", ""))

    def test_deploy_plan_fields(self):
        run = self._plan()
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["name"], "auth")
        plan = run["plan"]
        if isinstance(plan, str):
            plan = json.loads(plan)
        self.assertEqual(plan["op"], "deploy")
        self.assertEqual(plan["env"], "prod")
        self.assertEqual(plan["version"], "1.2.0")
        self.assertIn("--project auth", plan["cli"])
        self.assertIn("目标: 1 台服务器", "\n".join(plan["steps"]))

    def test_plan_validations(self):
        from agent.executor import ExecutorError
        with self.assertRaises(ExecutorError):
            self._plan(env="")                       # 缺环境
        with self.assertRaises(ExecutorError):
            self._plan(version="abc")                # 版本非法
        with self.assertRaises(ExecutorError):
            self._plan(op="deploy", target="ghost")  # 目标不存在
        with self.assertRaises(ExecutorError):
            self._plan(op="restart", env="qa")       # 环境不属于项目
        with self.assertRaises(ExecutorError):
            self._plan(op="rollback", canary=True, version="1.0.0")

    def test_rollback_plan_flag(self):
        run = self._plan(op="rollback", version="1.0.0",
                         previous_version="1.2.0")
        plan = run["plan"]
        if isinstance(plan, str):
            plan = json.loads(plan)
        self.assertTrue(plan["flags"]["rollback"])
        self.assertIn("--rollback", plan["cli"])
        run2 = self._plan(op="rollback", version="1.0.0")
        self.assertEqual(run2["version"], "1.0.0")
        # 回滚与 canary 互斥
        from agent.executor import ExecutorError
        with self.assertRaises(ExecutorError):
            self._plan(op="rollback", canary=True, version="1.0.0")

    def test_block_window_forbids_plan(self):
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="封网", start_at=now - 10, end_at=now + 600,
            block_level="block", repeat_yearly=0, created_by=1,
            created_at=now))
        from agent.executor import ExecutorError
        with self.assertRaises(ExecutorError) as cm:
            self._plan()
        self.assertIn("封网", str(cm.exception))

    def test_warn_window_requires_force_on_approve(self):
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="重保窗口", start_at=now - 10,
            end_at=now + 600, block_level="warn", repeat_yearly=0,
            created_by=1, created_at=now))
        run = self._plan()
        plan = run["plan"]
        if isinstance(plan, str):
            plan = json.loads(plan)
        self.assertTrue(plan["window_warning"])
        from agent.executor import ExecutorError, decide_run
        with self.assertRaises(ExecutorError):
            decide_run(self.db, run["id"], USER, action="approve")
        # 显式确认后可提交执行
        with mock.patch("agent.executor.task_manager.submit_task") as sub:
            r2 = decide_run(self.db, run["id"], USER, action="approve",
                            note="重保已获批", force_window=True)
        self.assertEqual(r2["status"], "executing")
        sub.assert_called_once()
        task = dbm.get_task(self.db, r2["task_id"])
        self.assertIn("--force-window", task["extra"])

    def test_approve_and_reject_flow(self):
        from agent.executor import ExecutorError, decide_run, get_run
        run = self._plan()
        # 非法动作
        with self.assertRaises(ExecutorError):
            decide_run(self.db, run["id"], USER, action="later")
        # 驳回
        r1 = decide_run(self.db, run["id"], USER, action="reject",
                        note="需求取消")
        self.assertEqual(r1["status"], "rejected")
        self.assertEqual(r1["decided_by_name"], "tester")
        # 已驳回不可再审
        with self.assertRaises(ExecutorError):
            decide_run(self.db, run["id"], USER, action="approve")
        # 通过 → 创建平台任务并进入 executing
        run2 = self._plan()
        with mock.patch("agent.executor.task_manager.submit_task"):
            r2 = decide_run(self.db, run2["id"], USER, action="approve")
        self.assertEqual(r2["status"], "executing")
        self.assertGreater(r2["task_id"], 0)
        task = dbm.get_task(self.db, r2["task_id"])
        self.assertEqual(task["action"], "deploy")
        self.assertEqual(task["version"], "1.2.0")
        self.assertNotIn("--rollback", task["extra"])

    def test_rollback_approve_submits_rollback(self):
        from agent.executor import decide_run
        run = self._plan(op="rollback", version="1.0.0",
                         previous_version="1.2.0")
        with mock.patch("agent.executor.task_manager.submit_task"):
            r2 = decide_run(self.db, run["id"], USER, action="approve")
        task = dbm.get_task(self.db, r2["task_id"])
        self.assertEqual(task["action"], "rollback")
        self.assertIn("--rollback", task["extra"])

    def test_sync_executing_runs(self):
        from agent.executor import decide_run, sync_executing_runs
        run = self._plan()
        with mock.patch("agent.executor.task_manager.submit_task"):
            r2 = decide_run(self.db, run["id"], USER, action="approve")
        task = dbm.get_task(self.db, r2["task_id"])
        dbm.update_task(self.db, task["id"], status="success",
                        finished_at=int(time.time()))
        self.assertEqual(sync_executing_runs(self.db), 1)
        rec = dbm.get_record(self.db, "agent_runs", r2["id"])
        self.assertEqual(rec["status"], "success")
        self.assertIn("任务 #", rec["summary"])


class TestAdvisor(AgentBase):
    def _alert(self, severity="P1", subject="订单服务连接超时"):
        return dbm.create_record(self.db, "alerts", dict(
            rule_id=0, fingerprint=f"fp-{int(time.time()*1000)}-{severity}",
            status="firing", severity=severity, subject=subject,
            description="curl timeout", target_info=json.dumps(
                {"service": "auth", "env": "prod", "project": "平台"}),
            first_seen_at=int(time.time()), last_seen_at=int(time.time())))

    def test_analyze_alert_and_dedupe(self):
        from agent import advisor as adv
        aid = self._alert()
        advice = adv.analyze_alert(self.db, aid, actor=USER)
        self.assertEqual(advice["scope"], "alert")
        self.assertIn("归因", advice["title"])
        # TTL 内重复调用返回同一条
        again = adv.analyze_alert(self.db, aid, actor=USER)
        self.assertEqual(again["id"], advice["id"])
        # force 强制重生成
        forced = adv.analyze_alert(self.db, aid, actor=USER, force=True)
        self.assertNotEqual(forced["id"], advice["id"])

    def test_analyze_failure_rollback_suggestion(self):
        from agent import advisor as adv
        log = os.path.join(self.tmp, "task.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("[ERROR] docker pull failed: connection timeout\n"
                    "发布第 2/5 台失败，已停止\n")
        tid = self.make_task(status="failed", log_file=log,
                             finished_at=int(time.time()))
        advice = adv.analyze_failure(self.db, tid, actor=USER)
        self.assertEqual(advice["scope"], "task_failed")
        self.assertIn("1.0.0", advice["body"])  # 回滚目标
        self.assertIn("回滚", advice["body"])
        # 无回滚版本（首次发布）→ 建议人工排查
        tid2 = self.make_task(previous_version="", version="1.0.0",
                              status="failed", log_file=log,
                              finished_at=int(time.time()))
        advice2 = adv.analyze_failure(self.db, tid2, actor=USER)
        self.assertIn("首次发布", advice2["body"])

    def test_advise_canary_continue_with_no_evidence(self):
        from agent import advisor as adv
        tid = self.make_task(version="1.2.0", status="running",
                             previous_version="1.1.0")
        advice = adv.advise_canary(self.db, tid, actor=USER)
        self.assertEqual(advice["scope"], "canary")
        self.assertIn("continue", advice["body"])

    def test_run_precheck(self):
        from agent import advisor as adv
        # 缺 env → 阻断项存在但接口不抛错
        res = adv.run_precheck(self.db, "auth", env="", version="",
                               actor=USER)
        self.assertFalse(res["passed"])
        # 完整环境 + 合法版本 → 通过
        res2 = adv.run_precheck(self.db, "auth", env="prod", version="1.2.0",
                                actor=USER)
        self.assertTrue(res2["passed"])
        # 非法版本 → 阻断
        res3 = adv.run_precheck(self.db, "auth", env="prod", version="x",
                                actor=USER)
        self.assertFalse(res3["passed"])
        # 不存在的服务
        with self.assertRaises(ValueError):
            adv.run_precheck(self.db, "ghost", env="prod")

    def test_save_case_from_advice(self):
        from agent import advisor as adv
        aid = self._alert()
        advice = adv.analyze_alert(self.db, aid, actor=USER)
        cid = adv.save_case_from_advice(self.db, advice["id"], actor=USER)
        case = dbm.get_record(self.db, "agent_cases", cid)
        self.assertEqual(case["source"], "advice")
        self.assertEqual(case["service"], "auth")
        self.assertEqual(case["advice_id"], advice["id"])
        with self.assertRaises(ValueError):
            adv.save_case_from_advice(self.db, 9999, actor=USER)

    def test_sweep_no_crash_empty(self):
        from agent import advisor as adv
        result = adv.sweep(self.db, actor=USER)
        self.assertEqual(result["errors"], [])
        # 失败任务 + P0 告警 → 各产出建议
        log = os.path.join(self.tmp, "t2.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("ERROR: OOM killed\n")
        self.make_task(status="failed", log_file=log,
                       finished_at=int(time.time()))
        self._alert(severity="P0")
        result2 = adv.sweep(self.db, actor=USER)
        self.assertGreaterEqual(result2["failure"], 1)
        self.assertGreaterEqual(result2["alert"], 1)
        # 第二轮（TTL 内）不重复生成
        result3 = adv.sweep(self.db, actor=USER)
        self.assertEqual(result3["failure"], 0)
        self.assertEqual(result3["alert"], 0)


class TestScheduler(AgentBase):
    def test_start_stop(self):
        from agent.scheduler import start_scheduler, stop_scheduler, \
            is_running
        try:
            self.assertTrue(start_scheduler(self.db, interval=3600))
            self.assertTrue(is_running())
            self.assertFalse(start_scheduler(self.db, interval=3600))
        finally:
            stop_scheduler()


class TestCoreLoop(AgentBase):
    def _llm_msg(self, content=None, tool_calls=None):
        return {"role": "assistant", "content": content,
                "tool_calls": tool_calls or []}

    def test_chat_once_persists_messages(self):
        from agent import core as core_mod
        seq = [
            self._llm_msg(content="平台共 1 个项目。"),
        ]
        with mock.patch("agent.core.llm_mod.chat", side_effect=seq):
            out = core_mod.chat_once(self.db, USER, "有哪些项目？",
                                     session_id="s1")
        self.assertIn("1 个项目", out["reply"])
        self.assertEqual(out["tool_calls"], 0)
        rows = dbm.list_records(self.db, "agent_messages",
                                "session_id=? AND role IN ('user','assistant')",
                                ("s1",), "id ASC")
        self.assertEqual(len(rows), 2)

    def test_tool_round_robin_and_plan_capture(self):
        from agent import core as core_mod
        plan_msg = self._llm_msg(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "plan_operation",
                         "arguments": json.dumps(
                             {"op": "deploy", "target": "auth", "env": "prod",
                              "version": "1.3.0", "intent": "测试"})}}])
        final_msg = self._llm_msg(content="已生成审批单，等待管理员审批。")
        with mock.patch("agent.core.llm_mod.chat",
                        side_effect=[plan_msg, final_msg]):
            out = core_mod.chat_once(self.db, USER, "帮我发布 auth 1.3.0",
                                     session_id="s2")
        self.assertEqual(out["tool_calls"], 1)
        self.assertGreater(out["plan_run_id"], 0)
        run = dbm.get_record(self.db, "agent_runs", out["plan_run_id"])
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["intent"], "测试")

    def test_llm_unavailable_raises(self):
        from agent import core as core_mod, llm as llm_mod
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LLM_BASE_URL", None)
            os.environ.pop("AGENT_LLM_API_KEY", None)
            with self.assertRaises(llm_mod.LlmConfigError):
                core_mod.chat_once(self.db, USER, "hi", session_id="s3")

    def test_run_stream_text_and_done_events(self):
        from agent import core as core_mod
        final = self._llm_msg(content="auth 服务运行正常。")
        with mock.patch("agent.core.llm_mod.stream_chat",
                        return_value=iter([
                            {"type": "delta", "text": "auth 服务"},
                            {"type": "delta", "text": "运行正常。"},
                            {"type": "message", "message": final}])):
            session = core_mod.AgentSession(self.db, USER, session_id="s4")
            events = list(session.run_stream("auth 状态？"))
        self.assertEqual(events[-1]["type"], "done")
        self.assertNotIn("tool", [e["type"] for e in events])
        texts = "".join(e["text"] for e in events if e["type"] == "text")
        self.assertEqual(texts, "auth 服务运行正常。")
        self.assertEqual(events[-1]["result"]["reply"],
                         "auth 服务运行正常。")

    def test_run_stream_tool_events(self):
        from agent import core as core_mod
        tool_msg = self._llm_msg(tool_calls=[{
            "id": "call_1", "type": "function",
            "function": {"name": "list_projects", "arguments": "{}"}}])
        final = self._llm_msg(content="共 1 个项目。")
        with mock.patch("agent.core.llm_mod.stream_chat", side_effect=[
                iter([{"type": "message", "message": tool_msg}]),
                iter([{"type": "delta", "text": "共 1 个项目。"},
                      {"type": "message", "message": final}])]):
            session = core_mod.AgentSession(self.db, USER, session_id="s5")
            events = list(session.run_stream("有哪些项目？"))
        names = [e["name"] for e in events
                 if e["type"] in ("tool", "tool_result")]
        self.assertEqual(names, ["list_projects", "list_projects"])
        tr = [e for e in events if e["type"] == "tool_result"][0]
        self.assertTrue(tr["ok"])
        done = events[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["result"]["tool_calls"], 1)
        self.assertEqual(done["result"]["reply"], "共 1 个项目。")

    def test_run_stream_equivalent_to_run(self):
        """流式与批量两通道最终结果一致（同一轮对话）"""
        from agent import core as core_mod
        final = self._llm_msg(content="两通道结论一致。")
        with mock.patch("agent.core.llm_mod.chat", return_value=final):
            out = core_mod.chat_once(self.db, USER, "hi", session_id="s6")
        with mock.patch("agent.core.llm_mod.stream_chat",
                        return_value=iter([
                            {"type": "delta", "text": "两通道结论一致。"},
                            {"type": "message", "message": final}])):
            session = core_mod.AgentSession(self.db, USER, session_id="s7")
            events = list(session.run_stream("hi"))
        self.assertEqual(events[-1]["result"]["reply"], out["reply"])
        self.assertEqual(events[-1]["result"]["tool_calls"],
                         out["tool_calls"])

    def test_run_stream_empty_input(self):
        from agent import core as core_mod
        session = core_mod.AgentSession(self.db, USER, session_id="s8")
        events = list(session.run_stream("  "))
        self.assertEqual(events[-1]["result"]["reply"], "请输入问题")


class TestMcpServer(AgentBase):
    def _session(self, role="viewer"):
        from agent.mcp_server import McpSession
        return McpSession(self.db, role)

    def test_initialize_and_handshake(self):
        s = self._session()
        res = s.handle({"jsonrpc": "2.0", "id": 1,
                        "method": "initialize", "params": {}})
        self.assertIn("protocolVersion", res)
        self.assertEqual(res["serverInfo"]["name"], "agencys-deploy-agent")
        self.assertIsNone(s.handle({"jsonrpc": "2.0",
                                    "method": "notifications/initialized"}))
        self.assertEqual(s.handle({"jsonrpc": "2.0", "id": 2,
                                   "method": "ping"}), {})

    def test_unknown_method(self):
        from agent.mcp_server import McpError
        s = self._session()
        with self.assertRaises(McpError):
            s.handle({"jsonrpc": "2.0", "id": 1,
                      "method": "tools/unknown"})

    def test_tools_list_role_filtered(self):
        viewer = self._session("viewer")
        names = {t["name"] for t in viewer._tool_list()}
        self.assertIn("list_projects", names)
        self.assertNotIn("plan_operation", names)
        admin = self._session("admin")
        anames = {t["name"] for t in admin._tool_list()}
        self.assertIn("plan_operation", anames)
        self.assertIn("inputSchema", admin._tool_list()[0])

    def test_tools_call_read(self):
        s = self._session("viewer")
        res = s._tool_call({"name": "list_projects", "arguments": {}})
        self.assertFalse(res["isError"])
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(len(payload), 1)

    def test_tools_call_write_denied_for_viewer(self):
        s = self._session("viewer")
        res = s._tool_call({"name": "plan_operation",
                            "arguments": {"op": "deploy", "target": "auth",
                                          "env": "prod", "version": "1.2.0"}})
        self.assertTrue(res["isError"])
        self.assertIn("operator/admin", res["content"][0]["text"])

    def test_tools_call_plan_creates_run(self):
        s = self._session("operator")
        res = s._tool_call({"name": "plan_operation",
                            "arguments": {"op": "deploy", "target": "auth",
                                          "env": "prod", "version": "1.2.0"}})
        self.assertFalse(res["isError"])
        payload = json.loads(res["content"][0]["text"])
        self.assertEqual(payload["status"], "awaiting_approval")


class TestAgentManageTools(AgentBase):
    """配置管理工具接入：定时计划 / 发布窗口 / 灰度策略 / 灰度决策 / 告警闭环"""

    def _ctx(self, role="admin"):
        return type("Ctx", (), {"db_path": self.db, "user_id": 1,
                                "username": "tester", "role": role})()

    def _call(self, name, args, role="admin"):
        from agent import tools as tools_mod
        return json.loads(tools_mod.execute_tool(role, name, args,
                                                 self._ctx(role)))

    def test_new_tools_role_filtering(self):
        from agent import tools as tools_mod
        viewer_names = {t["name"] for t in tools_mod.visible_tools("viewer")}
        admin_names = {t["name"] for t in tools_mod.visible_tools("admin")}
        for n in ("manage_projects", "manage_services",
                  "manage_schedules", "manage_windows", "manage_canary",
                  "canary_decision", "set_alert_status"):
            self.assertNotIn(n, viewer_names)
            self.assertIn(n, admin_names)

    def test_projects_services_tool_lifecycle(self):
        # viewer 拒绝（与 Web 端 admin-only 对齐）
        denied = self._call("manage_projects",
                            {"action": "create", "name": "订单", "code": "order"},
                            role="viewer")
        self.assertIn("error", denied)
        # create 项目：code 白名单 + 唯一
        out = self._call("manage_projects",
                         {"action": "create", "name": "订单中心",
                          "code": "order", "git_org": "shop"})
        self.assertIn("id", out)
        pid = out["id"]
        bad_code = self._call("manage_projects",
                              {"action": "create", "name": "x", "code": "非法!"})
        self.assertIn("error", bad_code)
        dup = self._call("manage_projects",
                         {"action": "create", "name": "x2", "code": "order"})
        self.assertIn("error", dup)
        rows = self._call("manage_projects", {"action": "list"})
        codes = [p["code"] for p in rows["projects"]]
        self.assertIn("order", codes)
        upd = self._call("manage_projects",
                         {"action": "update", "project_id": pid,
                          "description": "订单域"})
        self.assertTrue(upd["ok"])
        # 服务注册：git_repo 远程 URL / 本地越界护栏 / 项目内唯一
        svc = self._call("manage_services", {
            "action": "create", "project": "order", "name": "order-api",
            "image_name": "order", "service_name": "hqb-order",
            "git_repo": "https://git.example.com/order-api.git",
            "git_pull_before_build": True, "health_port": 8080,
            "nacos_groups": ["DEFAULT_GROUP"]})
        self.assertIn("id", svc)
        sid = svc["id"]
        rec = dbm.get_record(self.db, "services", sid)
        self.assertEqual(rec["git_repo"], "https://git.example.com/order-api.git")
        self.assertEqual(rec["git_pull_before_build"], 1)
        self.assertEqual(rec["nacos_groups"], ["DEFAULT_GROUP"])
        escape = self._call("manage_services", {
            "action": "create", "project": "order", "name": "bad",
            "image_name": "b", "service_name": "b",
            "git_repo": "../evil"})
        self.assertIn("error", escape)
        bad_url = self._call("manage_services", {
            "action": "create", "project": "order", "name": "bad2",
            "image_name": "b", "service_name": "b",
            "git_repo": "https://git.example.com/非法.git"})
        self.assertIn("error", bad_url)
        dup_svc = self._call("manage_services", {
            "action": "create", "project": "order", "name": "order-api",
            "image_name": "b", "service_name": "b"})
        self.assertIn("error", dup_svc)
        # update：改 health_url + git_branch
        upd2 = self._call("manage_services", {
            "action": "update", "service_id": sid,
            "health_url": "/actuator/health", "git_branch": "master"})
        self.assertTrue(upd2["ok"])
        rec = dbm.get_record(self.db, "services", sid)
        self.assertEqual(rec["git_branch"], "master")
        # 项目删除被服务阻塞（与 Web 端 delete_project 一致）
        blocked = self._call("manage_projects",
                             {"action": "delete", "project_id": pid})
        self.assertIn("error", blocked)
        # 删服务 → 删项目成功
        dele = self._call("manage_services",
                          {"action": "delete", "service_id": sid})
        self.assertTrue(dele["ok"])
        dele_p = self._call("manage_projects",
                            {"action": "delete", "project_id": pid})
        self.assertTrue(dele_p["ok"])

    def test_schedules_lifecycle(self):
        out = self._call("manage_schedules", {
            "action": "create", "project": "plat", "name": "夜间发布",
            "type": "once", "env": "prod", "version": "1.2.0",
            "previous_version": "1.1.0",
            "schedule_at": int(time.time()) + 3600})
        self.assertIn("id", out)
        sid = out["id"]
        rows = self._call("manage_schedules", {"action": "list",
                                               "project": "plat"})
        self.assertEqual(len(rows["schedules"]), 1)
        self.assertEqual(rows["schedules"][0]["enabled"], 1)
        # cron 计划与表达式校验
        out2 = self._call("manage_schedules", {
            "action": "create", "project": "plat", "name": "每日发布窗口",
            "type": "cron", "cron_expr": "0 3 * * *", "env": "test",
            "version": "1.2.1"})
        self.assertIn("id", out2)
        bad = self._call("manage_schedules", {
            "action": "create", "project": "plat", "name": "坏表达式",
            "type": "cron", "cron_expr": "not-a-cron"})
        self.assertIn("error", bad)
        upd = self._call("manage_schedules", {
            "action": "update", "schedule_id": sid, "name": "夜间发布v2"})
        self.assertTrue(upd["ok"])
        tog = self._call("manage_schedules", {"action": "toggle",
                                              "schedule_id": sid})
        self.assertFalse(tog["enabled"])
        dele = self._call("manage_schedules", {"action": "delete",
                                               "schedule_id": sid})
        self.assertTrue(dele["ok"])

    def test_schedule_run_requires_approval_then_executes(self):
        out = self._call("manage_schedules", {
            "action": "create", "project": "plat", "name": "手动执行计划",
            "type": "once", "service": "auth", "env": "prod",
            "version": "1.2.0", "previous_version": "1.1.0",
            "schedule_at": int(time.time()) + 60})
        sid = out["id"]
        run_out = self._call("manage_schedules", {"action": "run",
                                                  "schedule_id": sid})
        self.assertEqual(run_out["status"], "awaiting_approval")
        run_id = run_out["run_id"]
        rec = dbm.get_record(self.db, "agent_runs", run_id)
        self.assertEqual(rec["kind"], "schedule_run")
        self.assertEqual(rec["ref_type"], "deploy_schedules")
        # 审批通过 → 复用平台任务通道（mock 子进程提交）
        with mock.patch("agent.executor.task_manager.submit_task") as sub:
            from agent.executor import decide_run
            r2 = decide_run(self.db, run_id, USER, action="approve")
        self.assertEqual(r2["status"], "executing")
        sub.assert_called_once()
        task = dbm.get_task(self.db, r2["task_id"])
        self.assertEqual(task["type"], "service")   # 计划的 ref_type
        self.assertEqual(task["name"], "auth")       # 服务级计划用服务名

    def test_windows_admin_only_and_lifecycle(self):
        denied = self._call("manage_windows", {"action": "list"},
                            role="operator")
        self.assertIn("error", denied)
        now = int(time.time())
        out = self._call("manage_windows", {
            "action": "create", "name": "春节封网", "block_level": "block",
            "start_at": now + 3600, "end_at": now + 7200,
            "scope": {"type": "project", "ids": [self.proj_id]}})
        self.assertIn("id", out)
        wid = out["id"]
        rows = self._call("manage_windows", {"action": "list"})
        self.assertEqual(rows["windows"][0]["name"], "春节封网")
        upd = self._call("manage_windows", {
            "action": "update", "window_id": wid,
            "block_level": "warn", "repeat_yearly": True})
        self.assertTrue(upd["ok"])
        bad_scope = self._call("manage_windows", {
            "action": "create", "name": "坏scope", "start_at": now,
            "end_at": now + 60, "scope": {"type": "ghost", "ids": []}})
        self.assertIn("error", bad_scope)
        dele = self._call("manage_windows", {"action": "delete",
                                             "window_id": wid})
        self.assertTrue(dele["ok"])

    def test_canary_default_uniqueness(self):
        steps = [{"batch": 1, "servers": 1, "observe_minutes": 5}]
        c1 = self._call("manage_canary", {
            "action": "create", "project": "plat", "name": "默认策略",
            "steps": steps, "is_default": True})
        c2 = self._call("manage_canary", {
            "action": "create", "project": "plat", "name": "备用策略",
            "steps": steps})
        # set_default 切换后，同项目仅剩一个默认
        self._call("manage_canary", {"action": "set_default",
                                     "config_id": c2["id"]})
        rows = self._call("manage_canary", {"action": "list",
                                            "project": "plat"})
        defaults = [c for c in rows["canary_configs"] if c["is_default"]]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0]["id"], c2["id"])
        bad_steps = self._call("manage_canary", {
            "action": "create", "project": "plat", "name": "坏steps",
            "steps": [{"servers": 1}]})
        self.assertIn("error", bad_steps)
        dele = self._call("manage_canary", {"action": "delete",
                                            "config_id": c1["id"]})
        self.assertTrue(dele["ok"])

    def test_canary_decision_requires_waiting(self):
        out = self._call("canary_decision", {"task_id": 999,
                                             "action": "continue"})
        self.assertIn("error", out)

    def test_alert_resolve_reopen(self):
        now = int(time.time())
        alert_id = dbm.create_record(self.db, "alerts", dict(
            rule_id=0, fingerprint="fp-1", status="firing", severity="P1",
            subject="auth 502", description="", target_info="{}",
            first_seen_at=now, last_seen_at=now))
        out = self._call("set_alert_status", {
            "action": "resolve", "alert_id": alert_id})
        self.assertEqual(out["status"], "resolved")
        rec = dbm.get_record(self.db, "alerts", alert_id)
        self.assertEqual(rec["resolved_by"], "tester")
        # 重复解决被拒
        dup = self._call("set_alert_status", {
            "action": "resolve", "alert_id": alert_id})
        self.assertIn("error", dup)
        # reopen 回 firing
        reo = self._call("set_alert_status", {
            "action": "reopen", "alert_id": alert_id})
        self.assertEqual(reo["status"], "firing")
        rec2 = dbm.get_record(self.db, "alerts", alert_id)
        self.assertEqual(rec2["status"], "firing")


class TestAgentAdviceAction(AgentBase):
    """建议一键采纳：失败归因→回滚审批单 / 灰度建议→决策文件"""

    def _save_advice(self, scope, evidence, title="测试建议"):
        from agent import advisor
        aid = advisor.save_advice(
            self.db, scope, int(evidence.get("task", {}).get("id", 0)),
            title, "## 建议动作\n回滚", evidence=evidence, model="rule-based",
            actor={"id": 0, "username": "advisor"})
        return aid

    def test_task_failed_with_prev_creates_rollback_run(self):
        task_id = self.make_task(version="1.1.0", previous_version="1.0.0",
                                 status="failed")
        aid = self._save_advice("task_failed", {
            "task": {"id": task_id, "name": "auth", "env": "prod",
                     "version": "1.1.0", "previous_version": "1.0.0"}})
        from agent import executor
        res = executor.advice_to_action(
            self.db, aid, {"id": 1, "username": "tester"})
        self.assertEqual(res["action"], "run")
        self.assertGreater(res["run_id"], 0)
        run = dbm.get_record(self.db, "agent_runs", res["run_id"])
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["name"], "auth")
        self.assertEqual(run["env"], "prod")
        self.assertEqual(run["version"], "1.0.0")  # 回滚目标版本
        self.assertIn("失败归因建议", run["intent"])
        # 采纳后建议自动标 adopted（终态，带产出关联）
        adv = dbm.get_record(self.db, "agent_advice", aid)
        self.assertEqual(adv["status"], "adopted")
        self.assertEqual(adv["action_ref"], f"agent_run:{res['run_id']}")
        self.assertEqual(adv["status_by"], "tester")

    def test_task_failed_without_prev_rejected(self):
        task_id = self.make_task(version="1.1.0", previous_version="",
                                 status="failed")
        aid = self._save_advice("task_failed", {
            "task": {"id": task_id, "name": "auth", "env": "prod",
                     "version": "1.1.0", "previous_version": ""}})
        from agent import executor
        with self.assertRaises(executor.ExecutorError):
            executor.advice_to_action(self.db, aid,
                                      {"id": 1, "username": "tester"})

    def test_canary_writes_decision_file(self):
        task_id = self.make_task(version="1.2.0", status="running")
        dec_file = os.path.join(self.tmp, "canary_decision_1.json")
        aid = self._save_advice("canary", {
            "task": {"id": task_id, "name": "auth", "env": "prod",
                     "version": "1.2.0"}, "decision": "rollback"})
        from agent import executor
        with mock.patch("deploy.read_canary_decision",
                        return_value={"task_id": task_id, "state": "waiting"}), \
                mock.patch("deploy.canary_decision_path",
                           return_value=dec_file):
            res = executor.advice_to_action(
                self.db, aid, {"id": 1, "username": "tester"})
        self.assertEqual(res["action"], "canary")
        self.assertEqual(res["decision"], "rollback")
        with open(dec_file, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["state"], "rollback")
        # 采纳后建议自动标 adopted
        adv = dbm.get_record(self.db, "agent_advice", aid)
        self.assertEqual(adv["status"], "adopted")
        self.assertEqual(adv["action_ref"], f"task:{task_id}")

    def test_canary_not_waiting_rejected(self):
        task_id = self.make_task(version="1.2.0", status="success")
        aid = self._save_advice("canary", {
            "task": {"id": task_id, "name": "auth", "env": "prod",
                     "version": "1.2.0"}, "decision": "continue"})
        from agent import executor
        with mock.patch("deploy.read_canary_decision",
                        return_value={"task_id": task_id, "state": "none"}):
            with self.assertRaises(executor.ExecutorError):
                executor.advice_to_action(self.db, aid,
                                          {"id": 1, "username": "tester"})

    def test_scope_without_action_rejected(self):
        from agent import executor
        aid = self._save_advice("alert", {"target": {"service": "auth"}})
        with self.assertRaises(executor.ExecutorError):
            executor.advice_to_action(self.db, aid,
                                      {"id": 1, "username": "tester"})
        # 不存在建议
        with self.assertRaises(executor.ExecutorError):
            executor.advice_to_action(self.db, 99999,
                                      {"id": 1, "username": "tester"})


class TestAdviceLifecycle(AgentBase):
    """建议生命周期：默认 pending / 状态流转 / 过滤 / 工具参数"""

    def test_save_default_pending(self):
        from agent import advisor
        aid = advisor.save_advice(
            self.db, "alert", 0, "告警归因", "## 分析", evidence={},
            model="rule-based", actor={"id": 0, "username": "advisor"})
        rec = dbm.get_record(self.db, "agent_advice", aid)
        self.assertEqual(rec["status"], "pending")
        self.assertIsNone(rec["status_at"])

    def test_status_flow_and_terminal(self):
        from agent import advisor
        aid = advisor.save_advice(
            self.db, "alert", 0, "告警归因", "## 分析", evidence={},
            actor={"id": 0, "username": "advisor"})
        user = {"id": 1, "username": "op"}
        # pending → dismissed → reopen → adopted（终态）
        advisor.set_advice_status(self.db, aid, "dismissed", user)
        self.assertEqual(dbm.get_record(
            self.db, "agent_advice", aid)["status"], "dismissed")
        advisor.set_advice_status(self.db, aid, "pending", user)
        self.assertEqual(dbm.get_record(
            self.db, "agent_advice", aid)["status"], "pending")
        advisor.set_advice_status(self.db, aid, "adopted", user,
                                  action_ref="agent_run:9")
        rec = dbm.get_record(self.db, "agent_advice", aid)
        self.assertEqual(rec["status"], "adopted")
        self.assertEqual(rec["action_ref"], "agent_run:9")
        self.assertEqual(rec["status_by"], "op")
        self.assertGreater(rec["status_at"], 0)
        # adopted 终态：忽略/重开/再采纳均拒绝
        for target in ("dismissed", "pending", "adopted"):
            with self.assertRaises(ValueError):
                advisor.set_advice_status(self.db, aid, target, user)

    def test_invalid_transitions_and_missing(self):
        from agent import advisor
        aid = advisor.save_advice(
            self.db, "alert", 0, "告警归因", "## 分析", evidence={},
            actor={"id": 0, "username": "advisor"})
        user = {"id": 1, "username": "op"}
        # 非法状态值 / 不存在建议
        with self.assertRaises(ValueError):
            advisor.set_advice_status(self.db, aid, "closed", user)
        with self.assertRaises(ValueError):
            advisor.set_advice_status(self.db, 99999, "dismissed", user)
        # pending 不能重开（仅 dismissed 可）
        with self.assertRaises(ValueError):
            advisor.set_advice_status(self.db, aid, "pending", user)

    def test_list_filter_by_status(self):
        from agent import advisor
        a1 = advisor.save_advice(
            self.db, "alert", 0, "告警归因一", "x", actor={})
        a2 = advisor.save_advice(
            self.db, "alert", 0, "告警归因二", "x", actor={})
        advisor.set_advice_status(self.db, a2, "dismissed",
                                  {"id": 1, "username": "op"})
        pend = advisor.list_advice(self.db, status="pending")
        self.assertEqual([r["id"] for r in pend], [a1])
        dism = advisor.list_advice(self.db, status="dismissed")
        self.assertEqual([r["id"] for r in dism], [a2])
        self.assertEqual(len(advisor.list_advice(self.db)), 2)

    def test_advice_list_tool_status_param(self):
        from agent import tools as tools_mod
        from agent import advisor as advisor_mod
        ctx = type("Ctx", (), {"db_path": self.db, "user_id": 1,
                              "username": "tester", "role": "admin"})()
        a1 = advisor_mod.save_advice(
            self.db, "alert", 0, "告警归因", "x", actor={})
        advisor_mod.set_advice_status(self.db, a1, "adopted",
                                      {"id": 1, "username": "op"},
                                      action_ref="task:5")
        out = json.loads(tools_mod.execute_tool(
            "admin", "advice_list", {"status": "adopted"}, ctx))
        self.assertEqual(len(out["advice"]), 1)
        self.assertEqual(out["advice"][0]["status"], "adopted")
        self.assertEqual(out["advice"][0]["action_ref"], "task:5")
        # 非法 status → error dict
        out = json.loads(tools_mod.execute_tool(
            "admin", "advice_list", {"status": "bad"}, ctx))
        self.assertIn("error", out)


class TestAgentDigest(AgentBase):
    """巡检日报：配置读写 / 到点判断 / 聚合 / 渲染 / 发送 / 历史"""

    def test_config_default_and_save(self):
        from agent import digest
        cfg = digest.get_digest_config(self.db)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["cron"], "0 9 * * *")
        self.assertEqual(cfg["channels"], [])
        # 保存后读取一致
        cfg = digest.save_digest_config(self.db, enabled=1,
                                        cron="30 8 * * 1-5",
                                        channels=["wecom", "feishu"])
        self.assertEqual(cfg["enabled"], 1)
        cfg2 = digest.get_digest_config(self.db)
        self.assertEqual(cfg2["channels"], ["wecom", "feishu"])
        self.assertEqual(cfg2["cron"], "30 8 * * 1-5")
        # 重复保存（update 路径）
        digest.save_digest_config(self.db, enabled=0)
        self.assertEqual(digest.get_digest_config(self.db)["enabled"], 0)
        # 非法渠道/非法 cron 报错
        with self.assertRaises(ValueError):
            digest.save_digest_config(self.db, channels=["sms_twilio"])
        with self.assertRaises(Exception):
            digest.save_digest_config(self.db, cron="not-a-cron")

    def test_due_ts_window(self):
        from agent import digest
        from datetime import datetime
        # 10:00 时今天 09:00 是最近应发时刻（在 48h 窗口内且已过）
        now = int(datetime(2026, 10, 10, 10, 0).timestamp())
        due = digest._due_ts("0 9 * * *", now)
        self.assertLess(due, now)
        self.assertGreater(now - due, 0)
        self.assertLess(now - due, digest._DUE_WINDOW)
        # 刚过 09:00 也能算出同一 due（窗口内向后找第一个触发点）
        now2 = int(datetime(2026, 10, 10, 9, 1).timestamp())
        self.assertEqual(digest._due_ts("0 9 * * *", now2), due)

    def test_check_and_send_disabled(self):
        from agent import digest
        res = digest.check_and_send(self.db)
        self.assertFalse(res["sent"])
        self.assertEqual(res["reason"], "disabled")

    def test_build_and_send_requires_channels(self):
        from agent import digest
        with self.assertRaises(ValueError):
            digest.build_and_send(self.db)

    def test_build_and_send_and_check_due(self):
        from agent import digest
        digest.save_digest_config(self.db, enabled=1,
                                  cron="* * * * *",  # 每分钟：永远 due
                                  channels=["wecom"])
        sent = []
        with mock.patch("webapp.alert_channels.send_custom",
                        side_effect=lambda db, text, ch, subject="s": (
                            sent.append((text, ch)) or [("wecom", True, "ok")])):
            res = digest.check_and_send(self.db)
        self.assertTrue(res["sent"])
        self.assertEqual(len(sent), 1)
        text, chans = sent[0]
        self.assertIn("巡检日报", text)
        self.assertEqual(chans, ["wecom"])
        # last_sent_at 已回写：下一轮不再发
        with mock.patch("webapp.alert_channels.send_custom") as m:
            res2 = digest.check_and_send(self.db)
        self.assertFalse(res2["sent"])
        m.assert_not_called()

    def test_collect_snapshot_and_render(self):
        from agent import digest
        now = int(time.time())
        # 造数据：一条未解决告警 + 一失败一成功任务 + 一待审审批单 + 一计划
        dbm.create_record(self.db, "alerts", dict(
            rule_id=0, fingerprint="fp-1", status="firing", severity="P1",
            subject="auth 连接超时", description="",
            target_info='{"service": "auth", "env": "prod"}',
            first_seen_at=now, last_seen_at=now))
        self.make_task(version="1.1.0", status="failed", finished_at=now)
        self.make_task(version="1.0.9", status="success", finished_at=now)
        dbm.create_record(self.db, "agent_runs", dict(
            kind="operation", project_id=self.proj_id, ref_type="services",
            ref_id=self.svc_id, name="auth", env="prod", version="1.2.0",
            plan="{}", status="awaiting_approval",
            created_by=1, created_at=now))
        dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="cron", name="每日构建",
            ref_type="project", ref_id=self.proj_id, env="prod",
            version="", previous_version="", extra="[]",
            schedule_at=0, cron_expr="0 3 * * *",
            enabled=1, last_run_at=0, next_run_at=now + 3600,
            created_by=1, created_at=now))
        snap = digest.collect_snapshot(self.db, now=now)
        self.assertTrue(snap["alerts"]["ok"])
        self.assertEqual(snap["alerts"]["total"], 1)
        self.assertEqual(snap["deploys"]["failed"], 1)
        self.assertEqual(snap["deploys"]["success"], 1)
        self.assertEqual(snap["approvals"]["pending"], 1)
        self.assertEqual(snap["schedules"]["enabled"], 1)
        text = digest.render_text(snap)
        self.assertIn("巡检日报", text)
        self.assertIn("auth 连接超时", text)
        self.assertIn("失败 1", text)
        self.assertIn("待审审批单 1 张", text)
        self.assertIn("每日构建", text)

    def test_send_digest_tool(self):
        from agent import tools as tools_mod
        from agent import digest as digest_mod
        ctx = type("Ctx", (), {"db_path": self.db, "user_id": 1,
                              "username": "tester", "role": "admin"})()
        # 未配置渠道 → 错误信息（execute_tool 统一转 error dict）
        out = json.loads(tools_mod.execute_tool(
            "admin", "send_digest", {}, ctx))
        self.assertIn("error", out)
        digest_mod.save_digest_config(self.db, channels=["wecom"])
        with mock.patch("webapp.alert_channels.send_custom",
                        return_value=[("wecom", True, "ok")]):
            out = json.loads(tools_mod.execute_tool(
                "admin", "send_digest", {}, ctx))
        self.assertIn("summary", out)
        self.assertIn("巡检日报", out["text"])
        # 工具表注册：viewer 不可见
        names = {t["name"] for t in tools_mod.visible_tools("viewer")}
        self.assertNotIn("send_digest", names)

    def test_build_and_send_writes_history_log(self):
        from agent import digest
        digest.save_digest_config(self.db, channels=["wecom", "feishu"])
        # 全部成功
        with mock.patch("webapp.alert_channels.send_custom",
                        return_value=[("wecom", True, "ok"),
                                      ("feishu", True, "ok")]):
            digest.build_and_send(self.db)
        rows = dbm.list_records(self.db, "agent_digest_logs", order="id DESC")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ok_count"], 2)
        self.assertEqual(rows[0]["total_count"], 2)
        self.assertIn("2/2 渠道成功", rows[0]["summary"])
        self.assertIn("wecom", rows[0]["channels"])
        # 部分失败也落历史（第二次发送）
        with mock.patch("webapp.alert_channels.send_custom",
                        return_value=[("wecom", False, "timeout"),
                                      ("feishu", True, "ok")]):
            digest.build_and_send(self.db)
        rows = dbm.list_records(self.db, "agent_digest_logs", order="id DESC")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ok_count"], 1)
        self.assertEqual(rows[0]["total_count"], 2)
        self.assertIn("timeout", rows[0]["detail"])


if __name__ == "__main__":
    unittest.main()
