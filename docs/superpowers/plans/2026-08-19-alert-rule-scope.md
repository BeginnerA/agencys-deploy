# 告警规则作用域级联选择器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将告警规则作用域从纯文本输入升级为按规则类型智能筛选的级联多选（环境→服务器/服务），后端匹配改用数字 ID。

**Architecture:** 后端新增 3 个 scope 数据 API（environments/servers/services），`_in_scope` 改用数字 ID 匹配并兼容旧字符串格式，引擎评估时解析 hostname→server_id / service_name→service_id。前端规则抽屉的作用域区域根据规则类型动态筛选可选类型，选 server/service 时先选环境再选目标。

**Tech Stack:** Python/Flask (后端 API + 引擎), Vanilla JS (前端), SQLite (数据存储)

**Spec:** `docs/superpowers/specs/2026-08-19-alert-rule-scope-design.md`

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `webapp/routes/alert_routes.py` | 新增 3 个 scope 数据 API + 规则 scope 校验 | 修改 |
| `webapp/alert_engine.py` | `_in_scope` 数字 ID 匹配 + 引擎评估适配 | 修改 |
| `webapp/static/js/pages/alerts.js` | 规则抽屉作用域级联选择器 + 规则列表 scope 展示 | 修改 |
| `webapp/static/css/pages/alerts.css` | 级联选择器样式 | 修改 |
| `tests/test_alert_engine.py` | scope 匹配 + 引擎评估测试 | 修改 |
| `tests/test_alert_routes.py` | scope API 端点测试 | 修改 |

---

### Task 1: Backend — Scope API 端点 + 规则校验

**Files:**
- Modify: `webapp/routes/alert_routes.py:1-20` (imports), `webapp/routes/alert_routes.py:184-237` (_validate_rule)
- Modify: `tests/test_alert_routes.py`

- [ ] **Step 1: 编写 scope API 测试**

在 `tests/test_alert_routes.py` 的 `TestAlertRoutes` 类末尾（`test_update_rule_scalar_types_rejected` 之后）添加：

```python
    def _setup_scope_data(self):
        """插入 scope API 测试所需的项目/环境/服务器/服务数据"""
        h = self._h(self.token)
        # 创建项目
        r = self.c.post("/api/config/projects", json={
            "name": "scope-proj", "code": "scope-p1", "description": ""},
            headers=h)
        pid = r.get_json()["id"]
        # 创建环境
        r = self.c.post(f"/api/config/projects/{pid}/environments", json={
            "name": "prod", "is_default": 1}, headers=h)
        env1 = r.get_json()["id"]
        r = self.c.post(f"/api/config/projects/{pid}/environments", json={
            "name": "test", "is_default": 0}, headers=h)
        env2 = r.get_json()["id"]
        # 创建服务器（直接写 DB，config_routes 无服务器创建 API 时备用）
        from webapp import db as dbm
        from pathlib import Path
        db_path = str(Path(_TMP) / "platform.db")
        sid = dbm.create_record(db_path, "servers", {"host": "10.0.0.1"})
        dbm.bind_server(db_path, env1, sid)
        # 创建服务
        svc_id = dbm.create_record(db_path, "services", dict(
            project_id=pid, name="scope-svc", image_name="scope-svc",
            service_name="scope-svc"))
        return env1, env2, sid, svc_id, pid

    def test_scope_environments(self):
        self._setup_scope_data()
        r = self.c.get("/api/alerts/scope/environments",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertGreaterEqual(len(data), 2)
        self.assertIn("id", data[0])
        self.assertIn("name", data[0])
        self.assertIn("project_name", data[0])

    def test_scope_servers_by_env(self):
        env1, env2, sid, _, _ = self._setup_scope_data()
        r = self.c.get(f"/api/alerts/scope/servers?env_ids={env1}",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], sid)
        self.assertEqual(data[0]["host"], "10.0.0.1")
        # 另一个环境无服务器
        r = self.c.get(f"/api/alerts/scope/servers?env_ids={env2}",
                        headers=self._h(self.token))
        self.assertEqual(r.get_json(), [])

    def test_scope_services_by_env(self):
        env1, env2, _, svc_id, pid = self._setup_scope_data()
        r = self.c.get(f"/api/alerts/scope/services?env_ids={env1}",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], svc_id)
        self.assertEqual(data[0]["name"], "scope-svc")

    def test_scope_no_env_ids_returns_empty(self):
        r = self.c.get("/api/alerts/scope/servers",
                        headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), [])

    def test_create_rule_scope_invalid_type(self):
        r = self._create_rule(scope={"type": "bogus", "ids": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("scope", r.get_json()["error"])

    def test_create_rule_scope_env_ids_must_be_list(self):
        r = self._create_rule(scope={"type": "server", "ids": [1], "env_ids": "bad"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("env_ids", r.get_json()["error"])

    def test_create_rule_scope_metric_cannot_use_service(self):
        """指标规则不能选「服务」作用域"""
        r = self._create_rule(
            metric_type="metric", metric="cpu",
            scope={"type": "service", "ids": [1]})
        self.assertEqual(r.status_code, 400)

    def test_create_rule_scope_status_cannot_use_server(self):
        """状态规则不能选「服务器」作用域"""
        r = self._create_rule(
            metric_type="status", metric="service_down",
            scope={"type": "server", "ids": [1]})
        self.assertEqual(r.status_code, 400)

    def test_create_rule_scope_with_env_ids_ok(self):
        r = self._create_rule(scope={
            "type": "server", "ids": [1, 2], "env_ids": [1]})
        self.assertEqual(r.status_code, 200)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_alert_routes.py -v -k "scope"`
Expected: FAIL — 404 (端点不存在) 或断言失败

- [ ] **Step 3: 实现 scope API 端点**

在 `webapp/routes/alert_routes.py` 中，`list_rules` 函数（约 L177）之前插入：

```python
# ---------- 作用域数据 API ----------

@alert_bp.route("/alerts/scope/environments")
@require_auth
def scope_environments():
    """全部环境列表（供级联选择第一级）"""
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT e.id, e.name, p.name as project_name "
            "FROM environments e JOIN projects p ON e.project_id=p.id "
            "ORDER BY p.name, e.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@alert_bp.route("/alerts/scope/servers")
@require_auth
def scope_servers():
    """指定环境下的服务器列表"""
    env_ids_raw = request.args.get("env_ids", "")
    if not env_ids_raw:
        return jsonify([])
    try:
        env_ids = [int(x) for x in env_ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify([])
    if not env_ids:
        return jsonify([])
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        placeholders = ",".join("?" * len(env_ids))
        rows = conn.execute(
            f"SELECT DISTINCT s.id, s.host FROM servers s "
            f"JOIN env_servers es ON es.server_id=s.id "
            f"WHERE es.env_id IN ({placeholders}) AND s.enabled=1 "
            f"ORDER BY s.host",
            env_ids
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@alert_bp.route("/alerts/scope/services")
@require_auth
def scope_services():
    """指定环境下的服务列表（通过环境→项目间接关联）"""
    env_ids_raw = request.args.get("env_ids", "")
    if not env_ids_raw:
        return jsonify([])
    try:
        env_ids = [int(x) for x in env_ids_raw.split(",") if x.strip()]
    except ValueError:
        return jsonify([])
    if not env_ids:
        return jsonify([])
    db = current_app.config["DB_PATH"]
    conn = dbm.get_conn(db)
    try:
        placeholders = ",".join("?" * len(env_ids))
        rows = conn.execute(
            f"SELECT DISTINCT s.id, s.name FROM services s "
            f"WHERE s.project_id IN ("
            f"  SELECT DISTINCT project_id FROM environments WHERE id IN ({placeholders})"
            f") ORDER BY s.name",
            env_ids
        ).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()
```

- [ ] **Step 4: 添加规则 scope 校验**

在 `alert_routes.py` 的 `_validate_rule` 函数中，在 `if has("channels"):` 校验块之前（约 L231）插入 scope 校验：

```python
    if has("scope"):
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            return "scope 格式错误"
        valid_types = ("all", "project", "env", "server", "service")
        if scope.get("type") not in valid_types:
            return "scope type 非法"
        if not isinstance(scope.get("ids"), list):
            return "scope.ids 需为列表"
        if "env_ids" in scope and not isinstance(scope["env_ids"], list):
            return "scope.env_ids 需为列表"
        # 按规则类型限制 scope type
        mt = body.get("metric_type") or (merged.get("metric_type") if 'merged' in dir() else None)
        if mt == "metric" and scope.get("type") == "service":
            return "指标规则不能选择服务作用域"
        if mt in ("status", "log_keyword") and scope.get("type") == "server":
            return "状态/日志规则不能选择服务器作用域"
```

注意：`_validate_rule` 在 create 和 update 中调用方式不同。create 时直接调用 `_validate_rule(body)`，update 时先调 `_validate_rule(body, partial=True)` 再调 `_validate_rule(merged)`。merged 是完整合并后的 dict，所以 `body.get("scope")` 在 merged 中就是合并后的 scope。上面代码中 `mt` 的取值需兼容两种场景——实际上 `body` 参数在两种场景下都传入了，且 update 场景下 `merged` 是局部变量。

修正：`_validate_rule` 没有 `merged` 参数。在 update 场景中，第二次调用 `_validate_rule(merged)` 时 `body` 参数实际是 `merged`。所以直接用 `body.get("metric_type")` 即可：

```python
    if has("scope"):
        scope = body.get("scope") or {}
        if not isinstance(scope, dict):
            return "scope 格式错误"
        valid_types = ("all", "project", "env", "server", "service")
        if scope.get("type") not in valid_types:
            return "scope type 非法"
        if not isinstance(scope.get("ids"), list):
            return "scope.ids 需为列表"
        if "env_ids" in scope and not isinstance(scope["env_ids"], list):
            return "scope.env_ids 需为列表"
        mt = body.get("metric_type")
        if mt == "metric" and scope.get("type") == "service":
            return "指标规则不能选择服务作用域"
        if mt in ("status", "log_keyword") and scope.get("type") == "server":
            return "状态/日志规则不能选择服务器作用域"
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_alert_routes.py -v -k "scope"`
Expected: 全部 PASS

- [ ] **Step 6: 运行全量测试确认无回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add webapp/routes/alert_routes.py tests/test_alert_routes.py
git commit -m "feat(alert): add scope data APIs and rule scope validation"
```

---

### Task 2: Backend — `_in_scope` 数字 ID 匹配 + 引擎评估适配

**Files:**
- Modify: `webapp/alert_engine.py:149-201` (_evaluate_metric_rules, _evaluate_status_rules), `webapp/alert_engine.py:327-344` (_in_scope), `webapp/alert_engine.py:440-453` (trigger_log_alert)
- Modify: `tests/test_alert_engine.py`

- [ ] **Step 1: 编写 `_in_scope` 新测试**

在 `tests/test_alert_engine.py` 的 `test_scope_env_filter` 之后（约 L343）添加：

```python
    def test_scope_server_by_id(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "server", "ids": [10], "env_ids": []})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(rule, env, server_id=10))
        self.assertFalse(e._in_scope(rule, env, server_id=20))

    def test_scope_server_with_env_ids_filter(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "server", "ids": [10], "env_ids": [self.env_id]})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(rule, env, server_id=10))
        # env 不匹配
        other_env = {"id": 999, "project_id": self.proj_id, "name": "other"}
        self.assertFalse(e._in_scope(rule, other_env, server_id=10))

    def test_scope_service_by_id(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "service", "ids": [5], "env_ids": []})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(rule, env, service_id=5))
        self.assertFalse(e._in_scope(rule, env, service_id=6))

    def test_scope_backward_compat_string_ids(self):
        """旧规则 ids 为字符串（hostname）时降级为字符串匹配"""
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "server", "ids": ["host-a"]})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(rule, env, host="host-a"))
        self.assertFalse(e._in_scope(rule, env, host="host-b"))

    def test_scope_backward_compat_service_string(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "service", "ids": ["my-svc"]})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertTrue(e._in_scope(rule, env, service="my-svc"))
        self.assertFalse(e._in_scope(rule, env, service="other"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_alert_engine.py -v -k "scope_server or scope_service or scope_backward"`
Expected: FAIL（`_in_scope` 尚未接受 `server_id`/`service_id` 参数）

- [ ] **Step 3: 改写 `_in_scope` 方法**

替换 `webapp/alert_engine.py` 中 `_in_scope` 方法（L327-344）为：

```python
    def _in_scope(self, rule: dict, env: dict, host: str = None,
                  service: str = None, server_id: int = None,
                  service_id: int = None) -> bool:
        """规则作用域匹配：all/project/env/server/service

        优先使用数字 ID（server_id/service_id）匹配；
        向后兼容：ids 含字符串元素时降级为 hostname/service name 匹配。
        env_ids 非空时额外检查当前环境是否在列表中。
        """
        scope = rule.get("scope") or {"type": "all", "ids": []}
        if not isinstance(scope, dict):
            return True  # 脏数据：非法 scope 按全局放行
        t, ids = scope.get("type", "all"), scope.get("ids") or []
        env_ids = scope.get("env_ids") or []
        if t == "all":
            return True
        if t == "project":
            return env["project_id"] in ids
        if t == "env":
            return env["id"] in ids
        if t == "server":
            if env_ids and env["id"] not in env_ids:
                return False
            # 向后兼容：ids 含字符串 → 按 hostname 匹配
            if ids and isinstance(ids[0], str):
                return host in ids
            if server_id is not None:
                return server_id in ids
            return host in ids  # server_id 未传，降级
        if t == "service":
            if env_ids and env["id"] not in env_ids:
                return False
            if ids and isinstance(ids[0], str):
                return service in ids
            if service_id is not None:
                return service_id in ids
            return service in ids  # service_id 未传，降级
        return True
```

- [ ] **Step 4: 运行 `_in_scope` 测试确认通过**

Run: `pytest tests/test_alert_engine.py -v -k "scope_server or scope_service or scope_backward"`
Expected: 全部 PASS

- [ ] **Step 5: 编写引擎评估适配测试**

在 `tests/test_alert_engine.py` 中添加（需要创建 server 记录和 env_servers 关联）：

```python
    def test_metric_rule_scope_by_server_id(self):
        """指标规则 scope=server 按 server_id 匹配"""
        # 创建服务器并绑定到环境
        sid = dbm.create_record(self.db, "servers",
                                dict(host="scope-host", enabled=1))
        dbm.bind_server(self.db, self.env_id, sid)
        # 规则仅匹配 sid
        self._metric_rule(metric="cpu", duration=0,
                          scope={"type": "server", "ids": [sid], "env_ids": []})
        e = self._engine(overviews={
            (self.proj_id, "test"): {"services": {},
                                     "host_metrics": {"scope-host": {"cpu": 95}}}
        })
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)

    def test_metric_rule_scope_excludes_other_server(self):
        """指标规则 scope=server 不匹配其他 server_id"""
        sid1 = dbm.create_record(self.db, "servers",
                                 dict(host="inc-host", enabled=1))
        sid2 = dbm.create_record(self.db, "servers",
                                 dict(host="exc-host", enabled=1))
        dbm.bind_server(self.db, self.env_id, sid1)
        dbm.bind_server(self.db, self.env_id, sid2)
        # 规则只匹配 sid1
        self._metric_rule(metric="cpu", duration=0,
                          scope={"type": "server", "ids": [sid1], "env_ids": []})
        e = self._engine(overviews={
            (self.proj_id, "test"): {"services": {},
                                     "host_metrics": {"exc-host": {"cpu": 95}}}
        })
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 0)

    def test_status_rule_scope_by_service_id(self):
        """状态规则 scope=service 按 service_id 匹配"""
        svc_id = dbm.create_record(self.db, "services", dict(
            project_id=self.proj_id, name="scope-svc",
            image_name="scope-svc", service_name="scope-svc"))
        self._metric_rule(metric="cpu", duration=0,
                          metric_type="status", metric="service_down",
                          scope={"type": "service", "ids": [svc_id], "env_ids": []})
        e = self._engine(overviews={
            (self.proj_id, "test"): {
                "services": {"scope-svc": {"status": "not_deployed"}},
                "host_metrics": {}}
        })
        e._evaluate_status_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
```

- [ ] **Step 6: 运行测试确认失败**

Run: `pytest tests/test_alert_engine.py -v -k "scope_by_server_id or scope_by_service_id or scope_excludes"`
Expected: FAIL（引擎尚未传递 server_id/service_id）

- [ ] **Step 7: 适配 `_evaluate_metric_rules`**

替换 `webapp/alert_engine.py` 中 `_evaluate_metric_rules` 方法（L149-181）为：

```python
    def _evaluate_metric_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "metric"]
        if not rules or not self._monitor:
            return
        # 1) 数据源：monitor 探测缓存 → 灌入滑动窗口
        metrics = {r.get("metric") or "cpu" for r in rules} & {"cpu", "mem", "disk", "load"}
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for host, m in (data.get("host_metrics") or {}).items():
                for metric in metrics:
                    self._push_history((env["id"], host, metric), m.get(metric))
        # 2) 构建 hostname → server_id 映射
        host_to_sid = {}
        for s in dbm.list_records(self._db_path, "servers"):
            host_to_sid[s["host"]] = s["id"]
        # 3) 评估滑动窗口
        env_map = {e["id"]: e for e in dbm.list_records(self._db_path, "environments")}
        for (env_id, host, metric) in list(self._history):
            env = env_map.get(env_id)
            if env is None:
                continue
            key = (env_id, host, metric)
            value = self._history[key][-1][1]
            server_id = host_to_sid.get(host)
            for rule in rules:
                if (rule.get("metric") or "cpu") != metric:
                    continue
                if not self._duration_hit(rule, key):
                    continue
                if not self._in_scope(rule, env, host=host, server_id=server_id):
                    continue
                self._fire(rule, f"host:{env_id}:{host}",
                           subject=f"{host} {metric.upper()} 过高",
                           description=f"当前 {value}%，阈值 {rule['compare']}{rule['threshold']}%，"
                                       f"持续 {rule['duration_seconds']}s",
                           target_info={"env": env["name"], "host": host})
```

- [ ] **Step 8: 适配 `_evaluate_status_rules`**

替换 `_evaluate_status_rules` 方法（L185-201）为：

```python
    def _evaluate_status_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "status"]
        if not rules or not self._monitor:
            return
        # 构建 service name → service_id 映射
        name_to_sid = {}
        for s in dbm.list_records(self._db_path, "services"):
            name_to_sid[s["name"]] = s["id"]
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for name, st in (data.get("services") or {}).items():
                for rule in rules:
                    metric = rule.get("metric", "service_down")
                    if metric == "service_down" and st.get("status") == "not_deployed":
                        service_id = name_to_sid.get(name)
                        if not self._in_scope(rule, env, service=name,
                                              service_id=service_id):
                            continue
                        self._fire(rule, f"service:{env['id']}:{name}",
                                   subject=f"服务下线: {name}",
                                   description=f"服务 {name} 在环境 {env['name']} 未部署或已停止",
                                   target_info={"env": env["name"], "service": name})
```

- [ ] **Step 9: 适配 `trigger_log_alert`**

替换 `trigger_log_alert` 方法（L440-453）为：

```python
    def trigger_log_alert(self, rule: dict, service: str, env_name: str,
                          line: str, context: list, env_id: int,
                          matched_keyword: str = ""):
        """日志关键词命中：创建/更新日志告警（指纹含关键词，收敛由部分唯一索引保证）

        matched_keyword: 实际命中的那个关键词（多关键词场景下精确到单个词）
        """
        # 查询 service name → service_id
        service_id = None
        svcs = dbm.list_records(self._db_path, "services", "name=?", (service,))
        if svcs:
            service_id = svcs[0]["id"]
        # 查询 env_id 对应的环境记录（用于 _in_scope 的 env 参数）
        envs = dbm.list_records(self._db_path, "environments", "id=?", (env_id,))
        env = envs[0] if envs else {"id": env_id, "project_id": 0, "name": env_name}
        keyword = matched_keyword or rule.get("metric") or "log"
        target = f"log:{env_id}:{service}:{keyword}"
        # scope 检查
        if not self._in_scope(rule, env, service=service, service_id=service_id):
            return
        self._fire(rule, target,
                   subject=f"日志告警 [{keyword}]: {service}",
                   description=f"{env_name}/{service} 日志命中关键词 {keyword}:\n{line}",
                   target_info={"env": env_name, "service": service,
                                "keyword": keyword, "context": context[:5]})
```

- [ ] **Step 10: 运行全部引擎测试确认通过**

Run: `pytest tests/test_alert_engine.py -v`
Expected: 全部 PASS（含新增的 8 个测试）

- [ ] **Step 11: 运行全量测试确认无回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 12: Commit**

```bash
git add webapp/alert_engine.py tests/test_alert_engine.py
git commit -m "feat(alert): rewrite _in_scope for numeric ID matching with backward compat"
```

---

### Task 3: Frontend — 规则抽屉作用域级联选择器

**Files:**
- Modify: `webapp/static/js/pages/alerts.js:48-56` (constants), `webapp/static/js/pages/alerts.js:605-736` (openRuleDrawer + saveRule), `webapp/static/js/pages/alerts.js:759-765` (parseScope)
- Modify: `webapp/static/css/pages/alerts.css`

- [ ] **Step 1: 添加 SCOPE_OPTIONS_BY_TYPE 常量**

在 `alerts.js` 的 `SCOPE_TYPES` 常量（L56）之后添加：

```javascript
  // 按规则类型筛选的可选作用域
  const SCOPE_OPTIONS_BY_TYPE = {
    metric: [['all','全部'],['project','项目'],['env','环境'],['server','服务器']],
    status: [['all','全部'],['project','项目'],['env','环境'],['service','服务']],
    log_keyword: [['all','全部'],['project','项目'],['env','环境'],['service','服务']]
  };
```

- [ ] **Step 2: 更新 parseScope 支持 env_ids**

替换 `parseScope` 函数（L759-765）为：

```javascript
  function parseScope(raw){
    if (!raw) return {type:'all', ids:[], env_ids:[]};
    if (typeof raw === 'string') {
      try { raw = JSON.parse(raw); } catch(e) { return {type:'all', ids:[], env_ids:[]}; }
    }
    return {
      type: raw.type || 'all',
      ids: raw.ids || [],
      env_ids: raw.env_ids || []
    };
  }
```

- [ ] **Step 3: 替换规则抽屉中的作用域区域 HTML**

在 `openRuleDrawer` 函数中，替换作用域相关的 HTML 部分（`<div class="field"><label>作用域</label>` 到 `rfScopeIdsWrap` 的 `</div>`，约 L629-634）为：

```javascript
        <div class="field"><label>作用域</label>
          <select id="rfScopeType"></select></div>
        <div class="field full" id="rfScopeCascadeWrap" style="display:none">
          <label>环境</label>
          <div id="rfScopeEnvTags" class="scope-tag-box"></div>
          <div id="rfScopeTargetWrap" style="margin-top:8px;display:none">
            <label id="rfScopeTargetLabel">目标</label>
            <div id="rfScopeTargetTags" class="scope-tag-box">
              <div class="muted" style="font-size:12px">请先选择环境</div>
            </div>
          </div>
        </div>
```

- [ ] **Step 4: 替换作用域联动逻辑**

在 `openRuleDrawer` 函数中，替换原有的作用域联动代码（`// 作用域联动` 注释到 `selScope.onchange` 的闭包，约 L678-682）为完整的级联选择逻辑：

```javascript
    // ---- 作用域级联选择 ----
    const selScope = document.getElementById('rfScopeType');
    const cascadeWrap = document.getElementById('rfScopeCascadeWrap');
    const envTagsEl = document.getElementById('rfScopeEnvTags');
    const targetWrap = document.getElementById('rfScopeTargetWrap');
    const targetTagsEl = document.getElementById('rfScopeTargetTags');
    const targetLabel = document.getElementById('rfScopeTargetLabel');
    let scopeEnvs = [];       // 全部环境列表（API 缓存）
    let scopeSelectedEnvs = new Set(scope.env_ids || []);
    let scopeSelectedTargets = new Set(scope.ids || []);

    // 根据规则类型筛选可选 scope type
    function updateScopeTypeOpts() {
      const mt = selType.value;
      const opts = SCOPE_OPTIONS_BY_TYPE[mt] || SCOPE_OPTIONS_BY_TYPE.metric;
      const curVal = selScope.value || scope.type || 'all';
      selScope.innerHTML = opts.map(([k,v]) =>
        `<option value="${k}" ${curVal===k?'selected':''}>${v}</option>`).join('');
      // 如果旧值不在新选项中，回退到 all
      if (![...opts.map(o=>o[0])].includes(selScope.value)) selScope.value = 'all';
    }

    function updateScopeVis() {
      const st = selScope.value;
      const needCascade = (st === 'server' || st === 'service');
      cascadeWrap.style.display = needCascade ? '' : 'none';
      if (needCascade) {
        targetLabel.textContent = st === 'server' ? '服务器' : '服务';
        renderEnvTags();
        if (scopeSelectedEnvs.size > 0) loadTargets();
        else { targetWrap.style.display='none'; }
      }
    }

    function renderEnvTags() {
      if (!scopeEnvs.length) {
        envTagsEl.innerHTML = '<div class="muted" style="font-size:12px">加载中...</div>';
        return;
      }
      envTagsEl.innerHTML = scopeEnvs.map(e => {
        const active = scopeSelectedEnvs.has(e.id) ? 'active' : '';
        return `<span class="scope-tag ${active}" data-eid="${e.id}">${esc(e.project_name)}/${esc(e.name)}</span>`;
      }).join('');
      envTagsEl.querySelectorAll('.scope-tag').forEach(tag => {
        tag.onclick = () => {
          const eid = Number(tag.dataset.eid);
          if (scopeSelectedEnvs.has(eid)) scopeSelectedEnvs.delete(eid);
          else scopeSelectedEnvs.add(eid);
          tag.classList.toggle('active');
          if (scopeSelectedEnvs.size > 0) loadTargets();
          else { targetWrap.style.display='none'; targetTagsEl.innerHTML=''; }
        };
      });
    }

    async function loadScopeEnvs() {
      try {
        scopeEnvs = await api('/api/alerts/scope/environments');
        renderEnvTags();
        // 编辑时：如果已有 env_ids，自动加载目标
        if (scopeSelectedEnvs.size > 0) loadTargets();
      } catch(e) {
        envTagsEl.innerHTML = '<div class="muted" style="color:var(--danger);font-size:12px">环境加载失败</div>';
      }
    }

    async function loadTargets() {
      const st = selScope.value;
      const ep = st === 'server' ? 'servers' : 'services';
      const envIds = [...scopeSelectedEnvs].join(',');
      targetWrap.style.display = '';
      targetTagsEl.innerHTML = '<div class="muted" style="font-size:12px">加载中...</div>';
      try {
        const targets = await api(`/api/alerts/scope/${ep}?env_ids=${envIds}`);
        if (!targets.length) {
          targetTagsEl.innerHTML = '<div class="muted" style="font-size:12px">所选环境下无可用目标</div>';
          return;
        }
        targetTagsEl.innerHTML = targets.map(t => {
          const label = st === 'server' ? t.host : t.name;
          const checked = scopeSelectedTargets.has(t.id) ? 'checked' : '';
          return `<label class="scope-target-cb"><input type="checkbox" data-tid="${t.id}" ${checked}> ${esc(label)}</label>`;
        }).join('');
        targetTagsEl.querySelectorAll('input[data-tid]').forEach(cb => {
          cb.onchange = () => {
            const tid = Number(cb.dataset.tid);
            if (cb.checked) scopeSelectedTargets.add(tid);
            else scopeSelectedTargets.delete(tid);
          };
        });
      } catch(e) {
        targetTagsEl.innerHTML = '<div class="muted" style="color:var(--danger);font-size:12px">目标加载失败</div>';
      }
    }

    // 绑定事件：规则类型变更 → 更新指标选项 + scope 选项 + 可见性
    selType.onchange = () => { updateMetricOpts(); updateScopeTypeOpts(); updateFieldVis(); updateScopeVis(); };
    // 初始化
    updateScopeTypeOpts();
    updateScopeVis();
    if (selScope.value === 'server' || selScope.value === 'service') {
      loadScopeEnvs();
    }
```

**重要**：上面的代码块已包含 `selType.onchange` 的新绑定（L751），因此需要**删除**原代码中 L673 的 `selType.onchange = () => { updateMetricOpts(); updateFieldVis(); };` 这一行，避免重复绑定。同时删除 L677 的 `updateFieldVis();` 调用（初始化已在级联代码块末尾统一处理）。

- [ ] **Step 5: 更新 saveRule 函数收集级联选择数据**

替换 `saveRule` 函数中 scope 收集逻辑（约 L707-711）：

原代码：
```javascript
      const scopeType = document.getElementById('rfScopeType').value;
      const scopeIdsRaw = document.getElementById('rfScopeIds').value.trim();
      const scopeIds = scopeType === 'all' ? []
        : scopeIdsRaw.split(',').map(s => s.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n));
      const scope = {type: scopeType, ids: scopeIds};
```

替换为：

```javascript
      const scopeType = document.getElementById('rfScopeType').value;
      let scopeIds, scopeEnvIds;
      if (scopeType === 'server' || scopeType === 'service') {
        scopeIds = [...(typeof scopeSelectedTargets !== 'undefined' ? scopeSelectedTargets : new Set())];
        scopeEnvIds = [...(typeof scopeSelectedEnvs !== 'undefined' ? scopeSelectedEnvs : new Set())];
      } else if (scopeType === 'all') {
        scopeIds = [];
        scopeEnvIds = [];
      } else {
        // project/env 类型：保留手动输入（使用增强下拉或文本框）
        const raw = (document.getElementById('rfScopeIds') || {}).value || '';
        scopeIds = raw.trim() ? raw.split(',').map(s => s.trim()).filter(Boolean).map(Number).filter(n => !isNaN(n)) : [];
        scopeEnvIds = [];
      }
      const scope = {type: scopeType, ids: scopeIds};
      if (scopeEnvIds.length) scope.env_ids = scopeEnvIds;
```

注意：`scopeSelectedTargets` 和 `scopeSelectedEnvs` 是 `openRuleDrawer` 的局部变量，`saveRule` 无法直接访问。需要将它们提升为模块级变量。

在模块顶部状态区域（约 L29 `let ruleSaving = false;` 之后）添加：

```javascript
  let scopeSelectedEnvs = new Set();
  let scopeSelectedTargets = new Set();
```

并在 `openRuleDrawer` 中，将 `let scopeSelectedEnvs` 和 `let scopeSelectedTargets` 的 `let` 去掉（改为赋值），即在级联选择代码块中：

```javascript
    scopeSelectedEnvs = new Set(scope.env_ids || []);
    scopeSelectedTargets = new Set(scope.ids || []);
```

- [ ] **Step 6: 为 project/env 类型保留简单输入框**

在级联选择代码块的 `updateScopeVis` 函数中，需要在 scope type 为 project/env 时显示 `rfScopeIdsWrap`（文本输入框）。

在 `openRuleDrawer` 的 HTML 中，需要保留原来的 `rfScopeIdsWrap`（L632-634），但将其改为仅对 project/env 类型可见：

在原有 `rfScopeIdsWrap` 的 HTML 中修改初始 style：

```javascript
        <div class="field" id="rfScopeIdsWrap" style="display:none">
          <label>作用域 IDs（逗号分隔）</label>
          <input id="rfScopeIds" value="" placeholder="1,2,3"></div>
```

在 `updateScopeVis` 函数中添加：

```javascript
      const idsWrap = document.getElementById('rfScopeIdsWrap');
      if (idsWrap) idsWrap.style.display = (st === 'project' || st === 'env') ? '' : 'none';
```

- [ ] **Step 7: 添加级联选择器 CSS**

在 `webapp/static/css/pages/alerts.css` 末尾添加：

```css
/* ==================== 作用域级联选择器 ==================== */
.scope-tag-box{display:flex;flex-wrap:wrap;gap:6px;padding:4px 0}
.scope-tag{display:inline-flex;align-items:center;padding:4px 10px;border-radius:var(--r-full);
  font-size:var(--fs-12);cursor:pointer;transition:var(--t-fast);
  background:rgba(0,212,255,.06);color:var(--text-2);border:1px solid var(--border);user-select:none}
.scope-tag:hover{border-color:var(--primary);color:var(--text-1)}
.scope-tag.active{background:rgba(0,212,255,.15);color:var(--primary);border-color:var(--primary)}
.scope-target-cb{display:inline-flex;align-items:center;gap:5px;font-size:var(--fs-13);
  color:var(--text-1);cursor:pointer;padding:3px 0}
.scope-target-cb input{width:auto;margin:0}
```

- [ ] **Step 8: 浏览器验证**

1. 启动应用
2. 进入告警中心 → 规则管理 → 新建规则
3. 选择「指标」类型 → 作用域下拉应显示：全部/项目/环境/服务器
4. 选择「服务器」→ 出现环境标签 → 点击环境 → 加载服务器列表 → 勾选
5. 切换「状态」类型 → 作用域下拉应显示：全部/项目/环境/服务（无「服务器」）
6. 保存规则 → 编辑该规则 → 确认级联选择正确回填
7. Commit:

```bash
git add webapp/static/js/pages/alerts.js webapp/static/css/pages/alerts.css
git commit -m "feat(alert): cascading scope selector in rule drawer"
```

---

### Task 4: Frontend — 规则列表作用域展示

**Files:**
- Modify: `webapp/static/js/pages/alerts.js:558-603` (renderRuleList)

- [ ] **Step 1: 添加规则列表 scope 展示缓存变量**

在模块顶部状态区域（约 L29）添加：

```javascript
  let scopeNameCache = null;  // {servers: {id: host}, services: {id: name}}
```

- [ ] **Step 2: 添加 scope 名称解析辅助函数**

在 `parseScope` 函数之后添加：

```javascript
  async function ensureScopeNames() {
    if (scopeNameCache) return scopeNameCache;
    try {
      const envs = await api('/api/alerts/scope/environments');
      const envIds = envs.map(e => e.id).join(',');
      const [servers, services] = await Promise.all([
        envIds ? api(`/api/alerts/scope/servers?env_ids=${envIds}`) : [],
        envIds ? api(`/api/alerts/scope/services?env_ids=${envIds}`) : []
      ]);
      scopeNameCache = {
        servers: Object.fromEntries(servers.map(s => [s.id, s.host])),
        services: Object.fromEntries(services.map(s => [s.id, s.name]))
      };
    } catch(e) {
      scopeNameCache = {servers: {}, services: {}};
    }
    return scopeNameCache;
  }

  function scopeSummary(scope, names) {
    const s = parseScope(scope);
    const typeLabel = {all:'全部',project:'项目',env:'环境',server:'服务器',service:'服务'};
    const label = typeLabel[s.type] || s.type;
    if (s.type === 'all' || !s.ids || !s.ids.length) return label;
    const nameMap = (s.type === 'server') ? names.servers : names.services;
    const resolved = (s.ids || []).map(id => nameMap?.[id] || `#${id}`);
    return `${label}(${s.ids.length}): ${resolved.slice(0, 3).join(', ')}${resolved.length > 3 ? '…' : ''}`;
  }
```

- [ ] **Step 3: 修改 renderRuleList 添加作用域列**

在 `renderRuleList` 函数中：

1. 将函数改为 async
2. 在函数开头加载名称缓存
3. 在表头添加「作用域」列
4. 在每行添加作用域单元格

替换 `renderRuleList` 函数为：

```javascript
  async function renderRuleList(){
    const box = document.getElementById('ruleListContainer');
    if (!box) return;
    const names = await ensureScopeNames();
    const filtered = ruleTypeFilter === 'all' ? rules : rules.filter(r => r.metric_type === ruleTypeFilter);
    if (!filtered.length) {
      box.innerHTML = `<div class="panel">${emptyState('📏', '暂无规则')}</div>`;
      return;
    }
    box.innerHTML = `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl rules-tbl">
        <thead><tr><th>名称</th><th>类型</th><th>指标</th><th>条件</th><th>作用域</th><th>级别</th><th>渠道</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${filtered.map(r => {
          const scope = parseScope(r.scope);
          const scopeStr = scopeSummary(r.scope, names);
          const chList = (r.channels || []).map(c => CHANNELS.find(([k]) => k === c)?.[1] || c);
          let cond = '-';
          if (r.metric_type === 'metric') cond = `${CMP_LABEL[r.compare] || r.compare} ${r.threshold}`;
          else if (r.metric_type === 'log_keyword') {
            const kws = (r.metric || '').split(',').map(k => k.trim()).filter(Boolean);
            cond = kws.length ? kws.map(k => `<span class="rule-kw-tag">${esc(k)}</span>`).join('') : '-';
          }
          return `<tr>
            <td><b>${esc(r.name)}</b>${r.remark ? `<div class="rule-remark">${esc(r.remark)}</div>` : ''}</td>
            <td><span class="rule-mt-badge mt-${esc(r.metric_type)}">${MT_LABEL[r.metric_type] || r.metric_type}</span></td>
            <td>${esc(r.metric || '-')}</td>
            <td class="num">${cond}${r.duration_seconds ? `<div class="rule-sub">持续 ${r.duration_seconds}s</div>` : ''}</td>
            <td><span class="rule-scope-summary" title="${esc(scopeStr)}">${esc(scopeStr)}</span></td>
            <td><span class="badge ${esc(r.severity||'P2').toLowerCase()}">${esc(r.severity||'P2')}</span></td>
            <td><div class="rule-ch-list">${chList.map(c => `<span class="rule-ch-tag">${esc(c)}</span>`).join('')}</div></td>
            <td><span class="badge ${r.enabled ? 'success' : 'pending'}">${r.enabled ? '启用' : '停用'}</span></td>
            <td class="rule-actions">
              <button class="btn sm" data-ra="edit" data-rid="${r.id}">编辑</button>
              <button class="btn sm" data-ra="toggle" data-rid="${r.id}">${r.enabled ? '停用' : '启用'}</button>
              <button class="btn sm danger" data-ra="del" data-rid="${r.id}">删除</button>
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table></div></div>`;
    // 事件委托（与原有逻辑一致）
    box.onclick = async (e) => {
      const btn = e.target.closest('[data-ra]');
      if (!btn) return;
      const id = Number(btn.dataset.rid);
      if (btn.dataset.ra === 'edit') { const r = rules.find(x => x.id === id); if (r) openRuleDrawer(r); }
      else if (btn.dataset.ra === 'toggle') await toggleRule(id);
      else if (btn.dataset.ra === 'del') await deleteRule(id);
    };
  }
```

- [ ] **Step 4: 添加 scope summary 样式**

在 `alerts.css` 末尾添加：

```css
/* 规则列表作用域摘要 */
.rule-scope-summary{font-size:var(--fs-12);color:var(--text-2);
  max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block}
```

- [ ] **Step 5: 确保 loadRules 后清除缓存**

在 `loadRules` 函数中，加载完成后重置缓存以便刷新名称：

在 `rules = await api(...)` 之后、`renderRuleList()` 之前添加：

```javascript
      scopeNameCache = null;  // 清除缓存，renderRuleList 会重新加载
```

- [ ] **Step 6: 浏览器验证**

1. 进入规则管理 Tab
2. 确认表格新增「作用域」列
3. 全部类型规则显示「全部」
4. 指定服务器的规则显示「服务器(N): host1, host2」
5. 指定服务的规则显示「服务(N): svc1, svc2」
6. Commit:

```bash
git add webapp/static/js/pages/alerts.js webapp/static/css/pages/alerts.css
git commit -m "feat(alert): show scope summary in rule list table"
```

---

### Task 5: 全量集成验证

- [ ] **Step 1: 运行全量测试**

Run: `pytest tests/ -v`
Expected: 全部 PASS，无回归

- [ ] **Step 2: 端到端手动验证清单**

1. 新建指标规则 → 作用域选「服务器」→ 选环境 → 选服务器 → 保存
2. 编辑该规则 → 确认环境/服务器正确回填
3. 新建状态规则 → 作用域选「服务」→ 选环境 → 选服务 → 保存
4. 确认指标规则的作用域下拉中无「服务」选项
5. 确认状态规则的作用域下拉中无「服务器」选项
6. 规则列表中查看作用域列显示正确
7. 旧规则（scope.ids 含字符串）编辑保存后自动转为数字 ID 格式

- [ ] **Step 3: 最终 Commit**

```bash
git add -A
git commit -m "feat(alert): rule scope cascading selector - complete implementation"
```
