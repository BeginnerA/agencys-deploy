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


if __name__ == "__main__":
    unittest.main()
