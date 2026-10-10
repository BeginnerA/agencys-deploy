# tests/test_agent_api.py
"""Agent Web API 测试：对话 / 审批决策 / 建议 / 案例 / 网关配置 / 灰度建议挂接"""
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


class AgentApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        self.app = create_app()
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        with self.app.app_context():
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="平台", code="plat", description="", git_org="",
                created_at=int(time.time())))
            self.svc_id = dbm.create_record(self.db_path, "services", dict(
                project_id=self.proj_id, name="auth", image_name="auth",
                service_name="hqb-auth", docker_context="", dockerfile="",
                build_order="[]", nacos_groups="[]", dubbo_services="[]",
                snailjob_group="", health_url="", health_port=0,
                log_error_keywords="[]", clear_log_dir="", git_repo="",
                enabled=1))
            self.env_id = dbm.create_record(self.db_path, "environments",
                                            dict(project_id=self.proj_id,
                                                 name="prod", is_default=1,
                                                 registry="{}", nacos="{}",
                                                 mysql_snailjob="{}",
                                                 ssh="{}", build="{}",
                                                 deploy="{}",
                                                 monitor_enabled=1))
            self.server_id = dbm.create_record(self.db_path, "servers", dict(
                host="10.0.0.1", ssh_port=22, ssh_user="root",
                ssh_password="x", nacos_ip="", remark="", enabled=1))
            dbm.create_record(self.db_path, "env_servers", dict(
                env_id=self.env_id, server_id=self.server_id, order_no=0))
            self._mk_user("admin", "admin")
            self._mk_user("viewer1", "viewer")
        self.client = self.app.test_client()
        self.headers = self._login("admin")
        self.viewer_headers = self._login("viewer1")

    def _mk_user(self, name, role):
        from werkzeug.security import generate_password_hash
        dbm.create_record(self.db_path, "users", dict(
            username=name,
            password_hash=generate_password_hash("test123"),
            role=role, must_change_password=0, enabled=1,
            created_at=int(time.time())))

    def _login(self, name):
        resp = self.client.post("/api/auth/login", json={
            "username": name, "password": "test123"})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return {"Authorization": "Bearer " + resp.get_json()["token"]}

    def _make_task(self, **kw):
        task_id = dbm.create_task(
            self.db_path, kw.get("project_id", self.proj_id),
            kw.get("type", "project"), kw.get("ref_id", self.svc_id),
            kw.get("name", "auth"), kw.get("env", "prod"),
            kw.get("version", "1.1.0"),
            kw.get("previous_version", "1.0.0"), 1)
        return task_id

    def _make_alert(self):
        return dbm.create_record(self.db_path, "alerts", dict(
            rule_id=0, fingerprint=f"fp-{int(time.time()*1000)}",
            status="firing", severity="P1", subject="auth 服务连接超时",
            description="timeout", target_info=json.dumps(
                {"service": "auth", "env": "prod", "project": "平台"}),
            first_seen_at=int(time.time()), last_seen_at=int(time.time())))

    # ---------- 对话 ----------

    def test_chat_requires_llm_config(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LLM_BASE_URL", None)
            os.environ.pop("AGENT_LLM_API_KEY", None)
            resp = self.client.post("/api/agent/chat", headers=self.headers,
                                    json={"message": "你好", "session_id": "w1"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("LLM 网关未配置", data["error"])
        self.assertIn("hint", data)

    def test_chat_ok(self):
        with mock.patch("agent.core.llm_mod.chat", return_value={
                "role": "assistant", "content": "一切正常",
                "tool_calls": []}):
            resp = self.client.post("/api/agent/chat", headers=self.headers,
                                    json={"message": "服务正常吗？",
                                          "session_id": "w2"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["reply"], "一切正常")
        # 会话消息可回溯
        resp2 = self.client.get("/api/agent/sessions/w2/messages",
                                headers=self.headers)
        self.assertEqual(resp2.status_code, 200)
        roles = [m["role"] for m in resp2.get_json()["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_chat_bad_body(self):
        resp = self.client.post("/api/agent/chat", headers=self.headers,
                                data="not-json",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_chat_stream_ok_and_persist(self):
        with mock.patch("agent.core.llm_mod.stream_chat",
                        return_value=iter([
                            {"type": "delta", "text": "你好"},
                            {"type": "delta", "text": "，我是助手。"},
                            {"type": "message", "message": {
                                "role": "assistant",
                                "content": "你好，我是助手。",
                                "tool_calls": []}}])):
            resp = self.client.post("/api/agent/chat/stream",
                                    headers=self.headers,
                                    json={"message": "你好",
                                          "session_id": "ws1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)
        body = resp.get_data(as_text=True)
        # 事件序列：text 增量（core 层）+ done 收尾（含 reply）
        self.assertIn('"type": "text"', body)
        self.assertIn('"type": "done"', body)
        self.assertIn("你好，我是助手。", body)
        # 会话消息可回溯（assistant 已落库）
        resp2 = self.client.get("/api/agent/sessions/ws1/messages",
                                headers=self.headers)
        roles = [m["role"] for m in resp2.get_json()["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_chat_stream_error_event_when_no_config(self):
        # 网关未配置：HTTP 200 + SSE error 事件（前端可读流内提示）
        from agent import llm as llm_mod
        with mock.patch("agent.core.llm_mod.stream_chat",
                        side_effect=llm_mod.LlmConfigError(
                            "LLM 网关未配置")):
            resp = self.client.post("/api/agent/chat/stream",
                                    headers=self.headers,
                                    json={"message": "hi",
                                          "session_id": "ws2"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn('"type": "error"', body)
        self.assertIn("LLM 网关未配置", body)

    def test_chat_stream_bad_body(self):
        resp = self.client.post("/api/agent/chat/stream", headers=self.headers,
                                data="not-json",
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    # ---------- 会话管理 ----------

    def _msg(self, session, role, content, user_id=1, ts=None):
        dbm.create_record(self.db_path, "agent_messages", dict(
            session_id=session, role=role, content=content,
            created_by=user_id, created_at=ts or int(time.time())))

    def test_sessions_list_scope_and_owner(self):
        # setUp 用户：admin=1、viewer1=2；此处补 op1=3
        self._mk_user("op1", "operator")
        op_headers = self._login("op1")
        t0 = int(time.time())
        self._msg("sA", "user", "服务正常吗", user_id=1, ts=t0)
        self._msg("sA", "assistant", "一切正常", user_id=1, ts=t0 + 2)
        self._msg("sB", "user", "发布看看", user_id=3, ts=t0 + 1)
        # viewer 无权看会话列表
        r = self.client.get("/api/agent/sessions",
                            headers=self.viewer_headers)
        self.assertEqual(r.status_code, 403)
        # admin 默认 mine：仅 sA（含条数/归属/最后消息摘要）
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        sess = r.get_json()["sessions"]
        self.assertEqual([s["session_id"] for s in sess], ["sA"])
        self.assertEqual(sess[0]["n"], 2)
        self.assertEqual(sess[0]["owner"], "admin")
        self.assertEqual(sess[0]["last_content"], "一切正常")
        # admin scope=all：sA + sB 都可见
        r = self.client.get("/api/agent/sessions?scope=all",
                            headers=self.headers)
        names = {s["session_id"] for s in r.get_json()["sessions"]}
        self.assertEqual(names, {"sA", "sB"})
        # operator 只能看到自己的 sB
        r = self.client.get("/api/agent/sessions", headers=op_headers)
        self.assertEqual([s["session_id"] for s in r.get_json()["sessions"]],
                         ["sB"])

    def test_session_messages_user_scope(self):
        self._mk_user("op1", "operator")
        op_headers = self._login("op1")
        t0 = int(time.time())
        self._msg("sA", "user", "hi", user_id=1, ts=t0)
        self._msg("sA", "assistant", "hello", user_id=1, ts=t0 + 1)
        self._msg("sA", "user", "op 的消息", user_id=3, ts=t0 + 2)
        # mine 语义：admin 只看到自己的两条
        r = self.client.get("/api/agent/sessions/sA/messages",
                            headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["messages"]), 2)
        # viewer 角色无权限（require_role 先拦截）
        r = self.client.get("/api/agent/sessions/sA/messages?user_id=3",
                            headers=self.viewer_headers)
        self.assertEqual(r.status_code, 403)
        # operator 指定 user_id → 403（仅 admin 可跨用户审计）
        r = self.client.get("/api/agent/sessions/sA/messages?user_id=3",
                            headers=op_headers)
        self.assertEqual(r.status_code, 403)
        self.assertIn("仅管理员", r.get_json()["error"])
        # admin 指定 user_id=3 → 仅 op 消息
        r = self.client.get("/api/agent/sessions/sA/messages?user_id=3",
                            headers=self.headers)
        msgs = r.get_json()["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "op 的消息")
        # admin user_id=0（CLI 会话归属）→ 无匹配
        r = self.client.get("/api/agent/sessions/sA/messages?user_id=0",
                            headers=self.headers)
        self.assertEqual(r.get_json()["messages"], [])

    def test_delete_session_permissions(self):
        self._mk_user("op1", "operator")
        op_headers = self._login("op1")
        t0 = int(time.time())
        self._msg("sA", "user", "admin 会话", user_id=1, ts=t0)
        self._msg("sB", "user", "op 会话", user_id=3, ts=t0 + 1)
        self._msg("cli-default", "user", "CLI 会话", user_id=0, ts=t0)
        # viewer 无权删除
        r = self.client.delete("/api/agent/sessions/sB",
                               headers=self.viewer_headers)
        self.assertEqual(r.status_code, 403)
        # operator 删自己的 sB
        r = self.client.delete("/api/agent/sessions/sB", headers=op_headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["deleted"], 1)
        # operator 不指定 user_id 删 admin 的 sA → 无匹配（0 条），会话仍在
        r = self.client.delete("/api/agent/sessions/sA", headers=op_headers)
        self.assertEqual(r.get_json()["deleted"], 0)
        # operator 指定 user_id → 403
        r = self.client.delete("/api/agent/sessions/sA?user_id=1",
                               headers=op_headers)
        self.assertEqual(r.status_code, 403)
        # admin 删自己的 sA + user_id=0 删 CLI 会话
        r = self.client.delete("/api/agent/sessions/sA", headers=self.headers)
        self.assertEqual(r.get_json()["deleted"], 1)
        r = self.client.delete("/api/agent/sessions/cli-default?user_id=0",
                               headers=self.headers)
        self.assertEqual(r.get_json()["deleted"], 1)
        r = self.client.get("/api/agent/sessions?scope=all",
                            headers=self.headers)
        self.assertEqual(r.get_json()["sessions"], [])

    def test_session_rename_and_badge(self):
        t0 = int(time.time())
        self._msg("sA", "user", "pay 服务为什么发布失败？请帮我分析原因并给出建议",
                  user_id=1, ts=t0)
        self._msg("sA", "assistant", "根据日志分析…", user_id=1, ts=t0 + 1)
        # 自动标题 = 首条用户消息（无重命名时）
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        sess = r.get_json()["sessions"]
        self.assertEqual(
            sess[0]["title"],
            "pay 服务为什么发布失败？请帮我分析原因并给出建议")
        # 重命名：viewer 403 / admin 200 且列表展示手动标题
        r = self.client.put("/api/agent/sessions/sA/title",
                            headers=self.viewer_headers, json={"title": "x"})
        self.assertEqual(r.status_code, 403)
        r = self.client.put("/api/agent/sessions/sA/title",
                            headers=self.headers, json={"title": "pay 发布排障"})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        self.assertEqual(r.get_json()["sessions"][0]["title"], "pay 发布排障")
        # 空标题 = 清除重命名，回退自动标题
        r = self.client.put("/api/agent/sessions/sA/title",
                            headers=self.headers, json={"title": "  "})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        self.assertEqual(
            r.get_json()["sessions"][0]["title"],
            "pay 服务为什么发布失败？请帮我分析原因并给出建议")
        # 删除会话同步清理重命名元数据（防孤儿行）
        r = self.client.put("/api/agent/sessions/sA/title",
                            headers=self.headers, json={"title": "临时名"})
        r = self.client.delete("/api/agent/sessions/sA", headers=self.headers)
        self.assertEqual(r.get_json()["deleted"], 2)  # user + assistant 各一条
        rows = dbm.list_records(self.db_path, "agent_session_meta",
                                "session_id=? AND user_id=?", ("sA", 1))
        self.assertEqual(rows, [])

    def test_session_pin_and_search(self):
        # 两个会话：sA 较旧、sB 较新（默认按最后活动倒序：sB 在前）
        t0 = int(time.time())
        self._msg("sA", "user", "订单服务告警排查", user_id=1, ts=t0)
        self._msg("sA", "assistant", "已定位根因", user_id=1, ts=t0 + 1)
        self._msg("sB", "user", "灰度发布计划", user_id=1, ts=t0 + 10)
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        sess = r.get_json()["sessions"]
        self.assertEqual([s["session_id"] for s in sess], ["sB", "sA"])
        self.assertEqual(sess[0]["pinned"], 0)
        # viewer 无权置顶
        r = self.client.put("/api/agent/sessions/sA/pin",
                            headers=self.viewer_headers, json={"pinned": True})
        self.assertEqual(r.status_code, 403)
        # 置顶 sA：排序优先于最近活动（sA 反超 sB）
        r = self.client.put("/api/agent/sessions/sA/pin",
                            headers=self.headers, json={"pinned": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["pinned"])
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        sess = r.get_json()["sessions"]
        self.assertEqual([s["session_id"] for s in sess], ["sA", "sB"])
        self.assertEqual(sess[0]["pinned"], 1)
        self.assertEqual(sess[1]["pinned"], 0)
        # 非布尔 pinned → 400
        r = self.client.put("/api/agent/sessions/sA/pin",
                            headers=self.headers, json={"pinned": "yes"})
        self.assertEqual(r.status_code, 400)
        # 取消置顶：恢复活动序
        r = self.client.put("/api/agent/sessions/sA/pin",
                            headers=self.headers, json={"pinned": False})
        r = self.client.get("/api/agent/sessions", headers=self.headers)
        self.assertEqual(
            [s["session_id"] for s in r.get_json()["sessions"]], ["sB", "sA"])
        # 会话搜索：按标题（自动标题含关键词）命中 sA
        r = self.client.get("/api/agent/sessions?q=告警",
                            headers=self.headers)
        self.assertEqual(
            [s["session_id"] for s in r.get_json()["sessions"]], ["sA"])
        # 按最后消息内容命中 sB
        r = self.client.get("/api/agent/sessions?q=灰度",
                            headers=self.headers)
        self.assertEqual(
            [s["session_id"] for s in r.get_json()["sessions"]], ["sB"])
        # 按手动标题命中（重命名后 title 覆盖自动标题）
        self.client.put("/api/agent/sessions/sB/title",
                        headers=self.headers, json={"title": "发布窗口讨论"})
        r = self.client.get("/api/agent/sessions?q=窗口",
                            headers=self.headers)
        self.assertEqual(
            [s["session_id"] for s in r.get_json()["sessions"]], ["sB"])
        # 无命中 → 空列表
        r = self.client.get("/api/agent/sessions?q=不存在的关键词xyz",
                            headers=self.headers)
        self.assertEqual(r.get_json()["sessions"], [])
        # 删除会话同步清理置顶元数据（防孤儿行，含 pinned）
        self.client.put("/api/agent/sessions/sA/pin",
                        headers=self.headers, json={"pinned": True})
        self.client.delete("/api/agent/sessions/sA", headers=self.headers)
        rows = dbm.list_records(self.db_path, "agent_session_meta",
                                "session_id=? AND user_id=?", ("sA", 1))
        self.assertEqual(rows, [])

    def test_session_meta_pinned_migration(self):
        # 存量库无 pinned 列 → _migrate 幂等补列（老库升级路径）
        old_db = os.path.join(self.tmp, "old.db")
        conn = dbm.get_conn(old_db)
        try:
            conn.execute(
                "CREATE TABLE agent_session_meta ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " session_id TEXT NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,"
                " title TEXT DEFAULT '', updated_at INTEGER NOT NULL,"
                " UNIQUE(session_id, user_id))")
            conn.execute(
                "INSERT INTO agent_session_meta"
                " (session_id, user_id, title, updated_at)"
                " VALUES ('sOld', 1, '老库会话', 1)")
            conn.commit()
        finally:
            conn.close()
        dbm.init_db(old_db)  # 幂等：补列 + 既有数据保留
        cols = {r["name"] for r in dbm.get_conn(old_db).execute(
            "PRAGMA table_info(agent_session_meta)").fetchall()}
        self.assertIn("pinned", cols)
        rows = dbm.list_records(old_db, "agent_session_meta",
                                "session_id=?", ("sOld",))
        self.assertEqual(rows[0]["title"], "老库会话")
        self.assertEqual(rows[0]["pinned"], 0)

    def test_advice_badge_counts(self):
        # 待处理建议角标：登录用户可查，计数随状态流转变化
        from agent import advisor as adv
        a1 = adv.save_advice(self.db_path, "alert", 0, "t1", "b", actor={})
        adv.save_advice(self.db_path, "alert", 0, "t2", "b", actor={})
        r = self.client.get("/api/agent/advice/badge",
                            headers=self.viewer_headers)
        self.assertEqual(r.get_json()["pending"], 2)
        adv.set_advice_status(self.db_path, a1, "dismissed",
                              {"id": 1, "username": "admin"})
        r = self.client.get("/api/agent/advice/badge", headers=self.headers)
        self.assertEqual(r.get_json()["pending"], 1)

    # ---------- 审批单 ----------

    def _plan_run(self, **kw):
        from agent.executor import plan_operation
        return plan_operation(
            self.db_path, {"id": 1, "username": "admin"},
            op=kw.get("op", "deploy"), target="auth", env="prod",
            version=kw.get("version", "1.2.0"),
            previous_version=kw.get("previous_version", ""))

    def test_runs_list_and_detail(self):
        run = self._plan_run()
        resp = self.client.get("/api/agent/runs", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()["runs"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "awaiting_approval")
        resp = self.client.get(f"/api/agent/runs/{run['id']}",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/agent/runs/9999", headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_run_approve_and_reject(self):
        run = self._plan_run()
        # viewer 无审批权
        resp = self.client.post(
            f"/api/agent/runs/{run['id']}/decision",
            headers=self.viewer_headers, json={"action": "approve"})
        self.assertEqual(resp.status_code, 403)
        # 驳回
        resp = self.client.post(
            f"/api/agent/runs/{run['id']}/decision", headers=self.headers,
            json={"action": "reject", "note": "先验证镜像"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["run"]["status"], "rejected")
        # 通过（mock 掉真实提交）
        run2 = self._plan_run()
        with mock.patch("agent.executor.task_manager.submit_task"):
            resp = self.client.post(
                f"/api/agent/runs/{run2['id']}/decision", headers=self.headers,
                json={"action": "approve", "note": "ok"})
        self.assertEqual(resp.status_code, 200)
        run2 = resp.get_json()["run"]
        self.assertEqual(run2["status"], "executing")
        self.assertGreater(run2["task_id"], 0)
        # 重复审批 → 409
        resp = self.client.post(
            f"/api/agent/runs/{run2['id']}/decision", headers=self.headers,
            json={"action": "approve"})
        self.assertEqual(resp.status_code, 409)

    def test_run_decision_bad_action(self):
        run = self._plan_run()
        resp = self.client.post(
            f"/api/agent/runs/{run['id']}/decision", headers=self.headers,
            json={"action": "maybe"})
        self.assertEqual(resp.status_code, 400)

    # ---------- 建议 ----------

    def test_alert_analyze_and_advice_list(self):
        aid = self._make_alert()
        resp = self.client.post(f"/api/agent/analyze/alert/{aid}",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        advice = resp.get_json()["advice"]
        self.assertEqual(advice["scope"], "alert")
        resp = self.client.get("/api/agent/advice?scope=alert",
                               headers=self.headers)
        self.assertEqual(len(resp.get_json()["advice"]), 1)
        # 不存在告警 → 404
        resp = self.client.post("/api/agent/analyze/alert/9999",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 404)
        # viewer 无归因权限
        resp = self.client.post(f"/api/agent/analyze/alert/{aid}",
                                headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)

    def test_advice_to_case_and_delete(self):
        aid = self._make_alert()
        resp = self.client.post(f"/api/agent/analyze/alert/{aid}",
                                headers=self.headers)
        advice_id = resp.get_json()["advice"]["id"]
        resp = self.client.post(f"/api/agent/advice/{advice_id}/to_case",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        case_id = resp.get_json()["case_id"]
        case = dbm.get_record(self.db_path, "agent_cases", case_id)
        self.assertEqual(case["source"], "advice")
        # viewer 不能删除
        resp = self.client.delete(f"/api/agent/advice/{advice_id}",
                                  headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        # admin 删除
        resp = self.client.delete(f"/api/agent/advice/{advice_id}",
                                  headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_precheck_api(self):
        resp = self.client.post("/api/agent/precheck", headers=self.headers,
                                json={"target": "auth", "env": "prod"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["passed"])
        self.assertGreater(len(data["checks"]), 0)
        resp = self.client.post("/api/agent/precheck", headers=self.headers,
                                json={"target": "ghost", "env": "prod"})
        self.assertEqual(resp.status_code, 404)

    # ---------- 建议一键采纳 / 审批单沉淀案例 ----------

    def test_advice_to_run_creates_rollback(self):
        tid = self._make_task(version="1.1.0", status="failed")
        from agent import advisor as adv
        aid = adv.save_advice(
            self.db_path, "task_failed", tid,
            "任务失败归因：auth prod 1.1.0", "## 建议动作\n回滚",
            evidence={"task": {"id": tid, "name": "auth", "env": "prod",
                               "version": "1.1.0",
                               "previous_version": "1.0.0"}},
            model="rule-based", actor={"id": 0, "username": "advisor"})
        # viewer 无采纳权限
        resp = self.client.post(f"/api/agent/advice/{aid}/to_run",
                                headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        # admin 采纳 → 201 + 回滚审批单
        resp = self.client.post(f"/api/agent/advice/{aid}/to_run",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["action"], "run")
        run = dbm.get_record(self.db_path, "agent_runs", data["run_id"])
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["version"], "1.0.0")
        # 不存在的建议 → 409
        resp = self.client.post("/api/agent/advice/99999/to_run",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 409)

    def test_run_to_case_endpoint(self):
        run = self._plan_run()
        dbm.update_record(self.db_path, "agent_runs", run["id"],
                          dict(status="failed", summary="构建失败"))
        # viewer 无权限
        resp = self.client.post(f"/api/agent/runs/{run['id']}/to_case",
                                headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(f"/api/agent/runs/{run['id']}/to_case",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        case_id = resp.get_json()["case_id"]
        case = dbm.get_record(self.db_path, "agent_cases", case_id)
        self.assertEqual(case["source"], "agent_run")
        # 未完结的审批单不可沉淀 → 409
        run2 = self._plan_run()
        resp = self.client.post(f"/api/agent/runs/{run2['id']}/to_case",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 409)

    # ---------- 巡检日报 ----------

    def test_digest_config_permissions_and_flow(self):
        # viewer/operator 均不能读写配置
        resp = self.client.get("/api/agent/digest/config",
                               headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        # admin 默认配置
        resp = self.client.get("/api/agent/digest/config", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        cfg = resp.get_json()["config"]
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["channels"], [])
        # 保存（非法渠道 → 400）
        resp = self.client.post("/api/agent/digest/config",
                                headers=self.headers,
                                json={"channels": ["nope"]})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/agent/digest/config",
                                headers=self.headers,
                                json={"enabled": True, "cron": "30 8 * * *",
                                      "channels": ["wecom"], "title": "早报"})
        self.assertEqual(resp.status_code, 200)
        cfg = resp.get_json()["config"]
        self.assertEqual(cfg["enabled"], 1)
        self.assertEqual(cfg["cron"], "30 8 * * *")
        self.assertEqual(cfg["channels"], ["wecom"])
        # 非法 cron → 400
        resp = self.client.post("/api/agent/digest/config",
                                headers=self.headers,
                                json={"cron": "bad"})
        self.assertEqual(resp.status_code, 400)
        # 预览（operator 可用）
        resp = self.client.get("/api/agent/digest/preview",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("早报", resp.get_json()["text"])
        # 手动发送（mock 渠道发送；operator 可用）
        with mock.patch("webapp.alert_channels.send_custom",
                        return_value=[("wecom", True, "ok")]):
            resp = self.client.post("/api/agent/digest/send",
                                    headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("1/1", resp.get_json()["summary"])
        # 发送历史落表且可查（operator 可用，viewer 403）
        resp = self.client.get("/api/agent/digest/logs",
                               headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.get("/api/agent/digest/logs",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        logs = resp.get_json()["logs"]
        self.assertGreaterEqual(len(logs), 1)
        self.assertEqual(logs[0]["ok_count"], 1)
        self.assertEqual(logs[0]["total_count"], 1)
        self.assertEqual(logs[0]["channels"], "wecom")

    # ---------- 建议生命周期 ----------

    def test_advice_lifecycle_endpoints(self):
        from agent import advisor as adv
        aid = adv.save_advice(
            self.db_path, "alert", 0, "告警归因：auth P1", "## 分析",
            evidence={"target": {"service": "auth"}},
            model="rule-based", actor={"id": 0, "username": "advisor"})
        # viewer 无忽略权限
        resp = self.client.post(f"/api/agent/advice/{aid}/dismiss",
                                headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 403)
        # admin 忽略 → 重开 → 过滤查询
        resp = self.client.post(f"/api/agent/advice/{aid}/dismiss",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/agent/advice?status=dismissed",
                               headers=self.headers)
        self.assertIn(aid, [a["id"] for a in resp.get_json()["advice"]])
        resp = self.client.get("/api/agent/advice?status=pending",
                               headers=self.headers)
        self.assertNotIn(aid, [a["id"] for a in resp.get_json()["advice"]])
        # 非法 status → 400
        resp = self.client.get("/api/agent/advice?status=bad",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        # 已忽略不能重复忽略 → 409；重开 → 回到待处理
        resp = self.client.post(f"/api/agent/advice/{aid}/dismiss",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 409)
        resp = self.client.post(f"/api/agent/advice/{aid}/reopen",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        rec = dbm.get_record(self.db_path, "agent_advice", aid)
        self.assertEqual(rec["status"], "pending")
        # 采纳后建议自动标 adopted，且不可再忽略
        tid = self._make_task(version="1.1.0", status="failed")
        dbm.update_record(self.db_path, "agent_advice", aid, dict(
            scope="task_failed", ref_id=tid,
            evidence='{"task": {"id": %d, "name": "auth", "env": "prod", '
                     '"version": "1.1.0", "previous_version": "1.0.0"}}' % tid))
        resp = self.client.post(f"/api/agent/advice/{aid}/to_run",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        rec = dbm.get_record(self.db_path, "agent_advice", aid)
        self.assertEqual(rec["status"], "adopted")
        self.assertTrue(rec["action_ref"].startswith("agent_run:"))
        resp = self.client.post(f"/api/agent/advice/{aid}/dismiss",
                                headers=self.headers)
        self.assertEqual(resp.status_code, 409)

    def test_advice_detail_endpoint(self):
        from agent import advisor as adv
        aid = adv.save_advice(
            self.db_path, "alert", 0, "告警归因：auth P1", "## 分析",
            evidence={"target": {"service": "auth"}},
            model="rule-based", actor={"id": 0, "username": "advisor"})
        # viewer 也可读单条（登录用户即可，互链跳转用）
        resp = self.client.get(f"/api/agent/advice/{aid}",
                               headers=self.viewer_headers)
        self.assertEqual(resp.status_code, 200)
        row = resp.get_json()["advice"]
        self.assertEqual(row["id"], aid)
        self.assertEqual(row["scope"], "alert")
        # 不存在 → 404
        resp = self.client.get("/api/agent/advice/99999",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    # ---------- 案例 CRUD ----------

    def test_cases_crud_permissions(self):
        # viewer 不能创建
        resp = self.client.post("/api/agent/cases", headers=self.viewer_headers,
                                json={"title": "x"})
        self.assertEqual(resp.status_code, 403)
        # 必填校验
        resp = self.client.post("/api/agent/cases", headers=self.headers,
                                json={"title": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/agent/cases", headers=self.headers,
                                json={"title": "磁盘写满导致发布失败",
                                      "service": "auth", "env": "prod",
                                      "version": "1.0.0",
                                      "symptom": "df 100%，容器 OOM",
                                      "root_cause": "日志未轮转",
                                      "action": "清理日志 + 扩容",
                                      "result": "成功",
                                      "lesson": "配置 logrotate",
                                      "tags": ["磁盘", "OOM"]})
        self.assertEqual(resp.status_code, 201)
        cid = resp.get_json()["case_id"]
        # 关键字检索（含中文相似匹配）
        resp = self.client.get("/api/agent/cases?keyword=磁盘写满",
                               headers=self.headers)
        self.assertGreaterEqual(len(resp.get_json()["cases"]), 1)
        # 更新
        resp = self.client.put(f"/api/agent/cases/{cid}",
                               headers=self.headers,
                               json={"result": "待验证", "tags": ["磁盘"]})
        self.assertEqual(resp.status_code, 200)
        # viewer 不能改
        resp = self.client.put(f"/api/agent/cases/{cid}",
                               headers=self.viewer_headers,
                               json={"result": "x"})
        self.assertEqual(resp.status_code, 403)
        # 删除仅 admin
        resp = self.client.delete(f"/api/agent/cases/{cid}",
                                  headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get(f"/api/agent/cases/{cid}",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_cases_tag_filter_and_exclude(self):
        self.client.post("/api/agent/cases", headers=self.headers,
                         json={"title": "磁盘写满导致发布失败", "service": "auth",
                               "symptom": "df 100%", "tags": ["磁盘", "OOM"]})
        self.client.post("/api/agent/cases", headers=self.headers,
                         json={"title": "连接池耗尽", "service": "order",
                               "symptom": "timeout", "tags": ["mysql"]})
        # tag 过滤（JSON 字段参数化 LIKE）
        resp = self.client.get("/api/agent/cases?tag=磁盘",
                               headers=self.headers)
        cases = resp.get_json()["cases"]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["title"], "磁盘写满导致发布失败")
        # 相似召回排除自身（详情页"相似案例"）
        cid = cases[0]["id"]
        resp = self.client.get(
            f"/api/agent/cases?keyword=磁盘&exclude_id={cid}",
            headers=self.headers)
        self.assertNotIn(cid,
                         [c["id"] for c in resp.get_json()["cases"]])

    # ---------- 网关配置 ----------

    def test_config_view_and_update(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LLM_BASE_URL", None)
            os.environ.pop("AGENT_LLM_API_KEY", None)
            resp = self.client.get("/api/agent/config", headers=self.headers)
            data = resp.get_json()
            self.assertFalse(data["available"])
            self.assertNotIn("api_key", data)
            # 保存配置（admin）
            resp = self.client.post("/api/agent/config", headers=self.headers,
                                    json={"base_url": "https://api.deepseek.com/v1",
                                          "api_key": "sk-secret",
                                          "model": "deepseek-chat"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.get_json()["available"])
            # DB 中已持久化且读取接口不泄露 api_key
            rows = dbm.list_records(self.db_path, "platform_settings",
                                    "key=?", ("agent_llm_api_key",))
            self.assertEqual(rows[0]["value"], "sk-secret")
            resp2 = self.client.get("/api/agent/config", headers=self.headers)
            self.assertNotIn("api_key", resp2.get_json())
            self.assertEqual(resp2.get_json()["model"], "deepseek-chat")
        # viewer 不能保存
        resp = self.client.post("/api/agent/config",
                                headers=self.viewer_headers,
                                json={"model": "x"})
        self.assertEqual(resp.status_code, 403)

    # ---------- 灰度决策 API 附带 AI 建议（Phase 2 挂接） ----------

    def test_canary_decision_carries_advice(self):
        tmp_dir = os.path.join(self.tmp, "decisions")
        os.makedirs(tmp_dir, exist_ok=True)

        def fake_path(task_id):
            return os.path.join(tmp_dir, f"canary_decision_{task_id}.json")

        tid = self._make_task(version="1.2.0", status="running")
        # 模拟 deploy.py 写入等待决策文件
        with open(fake_path(tid), "w", encoding="utf-8") as f:
            json.dump({"task_id": tid, "state": "waiting", "host": "10.0.0.1",
                       "version": "1.2.0", "previous_version": "1.1.0",
                       "updated_at": int(time.time())}, f,
                      ensure_ascii=False)
        # 预置一条灰度建议
        from agent import advisor as adv
        adv.save_advice(self.db_path, "canary", tid,
                        "灰度决策建议：auth prod 1.2.0 → continue",
                        "## 建议: continue（置信度 70%）\n\n## 理由\n- 首台健康",
                        evidence={"decision": "continue"},
                        model="rule-based",
                        actor={"id": 0, "username": "advisor"})
        with mock.patch("deploy.canary_decision_path", side_effect=fake_path):
            resp = self.client.get(f"/api/deploy/canary-decisions/{tid}",
                                   headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["state"], "waiting")
        self.assertIn("advice", data)
        self.assertEqual(data["advice"]["title"],
                         "灰度决策建议：auth prod 1.2.0 → continue")

    def test_canary_decision_without_advice_unchanged(self):
        tmp_dir = os.path.join(self.tmp, "dec2")
        os.makedirs(tmp_dir, exist_ok=True)

        def fake_path(task_id):
            return os.path.join(tmp_dir, f"canary_decision_{task_id}.json")

        with mock.patch("deploy.canary_decision_path", side_effect=fake_path):
            resp = self.client.get("/api/deploy/canary-decisions/1",
                                   headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("advice", resp.get_json())


if __name__ == "__main__":
    unittest.main()
