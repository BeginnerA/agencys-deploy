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
