# 服务卡片重启功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在「项目总览」服务卡片上新增「重启」按钮，以 `type='restart'` 异步任务方式对当前环境全部服务器执行 `docker restart` + 健康检查，并复用任务体系（列表/日志/终止/删除/重新运行）与发布窗口约束。

**Architecture:** 后端扩展 `POST /api/tasks` 支持 `type='restart'`（env 必填且属于该项目）；`task_manager._run_task` 对 restart 类型走线程内 `_run_restart` 执行器（不产 CLI/子进程），复用 `monitor.py` 的 SSH 连接池（`_ssh_pool`/`_ssh_exec_multi`/`_merge_server_ssh`/`_probe_health_url`），`ThreadPoolExecutor` 并发处理每台服务器，逐台写日志并汇总判定 success/failed；`kill_task` 通过新增 `_RestartHandle` 实现协作取消。前端在 devices 卡片加「重启」按钮（`canDeploy()` 显隐、window_warn 二次确认、running 卡片「发布中」匹配扩展），deploy 页适配操作标签/类型列/筛选。

**Tech Stack:** Python 3 + Flask + SQLite（tasks 表 type 自由文本无 CHECK 约束）+ paramiko 连接池 + 原生 JS（无前端框架）

**规格文档:** `docs/superpowers/specs/2026-08-27-service-restart-design.md`

---

### Task 1: 路由层支持 `type='restart'` 创建任务

**Files:**
- Create: `tests/test_restart_task.py`
- Modify: `webapp/routes/deploy_routes.py`

- [ ] **Step 1: 写失败测试** — 创建 `tests/test_restart_task.py`，包含 helper 与 `TestRestartAPI`（5 个用例）

```python
# tests/test_restart_task.py
"""服务重启任务测试：路由创建 + task_manager 基建 + 执行器 + 分发"""
import os
import tempfile
import time
import unittest
from unittest import mock

from webapp import db as dbm


def _make_svc(db, proj_id, service_name="hqb-auth", health_port=0, health_url=""):
    return dbm.create_record(db, "services", dict(
        project_id=proj_id, name="auth", image_name="auth",
        service_name=service_name, docker_context="", dockerfile="",
        build_order="[]", nacos_groups="[]", dubbo_services="[]",
        snailjob_group="", health_url=health_url, health_port=health_port,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


def _make_env(db, proj_id, name="prod"):
    return dbm.create_record(db, "environments", dict(
        project_id=proj_id, name=name, is_default=1,
        registry="{}", nacos="{}", mysql_snailjob="{}",
        ssh='{"user":"root","password":"envpw"}',
        build="{}", deploy="{}", monitor_enabled=1))


class TestRestartAPI(unittest.TestCase):
    """POST /api/tasks type=restart 创建接口"""

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
                name="p1", code="p1", description="", git_org="",
                created_at=int(time.time())))
            self.svc_id = _make_svc(self.db_path, self.proj_id)
            _make_env(self.db_path, self.proj_id)
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=int(time.time())))
        self.client = self.app.test_client()
        resp = self.client.post("/api/auth/login",
                                json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_create_restart_task(self):
        with mock.patch("webapp.routes.deploy_routes.submit_task") as submit:
            resp = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 201)
        task = dbm.get_task(self.db_path, resp.get_json()["id"])
        self.assertEqual(task["type"], "restart")
        self.assertEqual(task["action"], "restart")
        self.assertEqual(task["env"], "prod")
        self.assertEqual(task["version"], "")
        submit.assert_called_once()

    def test_create_restart_env_not_belonging(self):
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "other"})
        self.assertEqual(resp.status_code, 400)

    def test_create_restart_ref_not_found(self):
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": 9999, "env": "prod"})
        self.assertEqual(resp.status_code, 404)

    def test_create_restart_block_window(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="封网", start_at=now - 100, end_at=now + 100,
            block_level="block", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 403)

    def test_create_restart_warn_window(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="警告", start_at=now - 100, end_at=now + 100,
            block_level="warn", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.post("/api/tasks", headers=self.headers, json={
            "project_id": self.proj_id, "type": "restart",
            "ref_id": self.svc_id, "env": "prod"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["error"], "window_warn")
        # force_window=true 二次确认放行
        with mock.patch("webapp.routes.deploy_routes.submit_task"):
            resp2 = self.client.post("/api/tasks", headers=self.headers, json={
                "project_id": self.proj_id, "type": "restart",
                "ref_id": self.svc_id, "env": "prod", "force_window": True})
        self.assertEqual(resp2.status_code, 201)


# ===== 后续任务追加的测试类锚点 =====
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_restart_task.py -v`
Expected: `test_create_restart_task` 等 FAIL — 当前 `type_ not in ("project", "pipeline")` 校验返回 400（`status_code != 201`）

- [ ] **Step 3: 实现路由分支** — 修改 `webapp/routes/deploy_routes.py`

`_resolve_ref`（第 27-33 行）改为 restart 也解析 services 表：

```python
def _resolve_ref(db_path, project_id, type_, ref_id):
    """按类型解析引用记录；不存在或不属于该项目返回 None（越权信息隐藏）"""
    table = "services" if type_ in ("project", "restart") else "pipelines"
    rec = dbm.get_record(db_path, table, ref_id)
    if not rec or rec["project_id"] != project_id:
        return None
    return rec
```

`create_task`（第 36-102 行）按以下四处修改：

1. 类型校验行改为：

```python
            or type_ not in ("project", "pipeline", "restart"):
        return jsonify({"error": "project_id/ref_id 必填（整数），type 为 project/pipeline/restart"}), 400
```

2. 在 `if auto_version and (version or previous_version):` 之前插入 restart 分支（restart 强制忽略版本/构建类字段）：

```python
    if type_ == "restart":
        # 重启：env 必填且必须属于该项目；版本/构建类字段不适用，一律强制缺省
        if not env:
            return jsonify({"error": "重启任务 env 必填"}), 400
        if not dbm.get_env_by_name(current_app.config["DB_PATH"], project_id, env):
            return jsonify({"error": "env 不属于该项目"}), 400
        rollback = auto_version = skip_build = False
        version = previous_version = ""
    elif auto_version and (version or previous_version):
        return jsonify({"error": "auto_version 与显式 version/previous_version 互斥"}), 400
```

3. warn 窗口放行分支改为 restart 不追加 `--force-window`（restart 不走子进程，extra 无意义）：

```python
        if not force_window:
            return jsonify({"error": "window_warn",
                            "window": {"name": window["name"], "id": window["id"]}}), 409
        if type_ != "restart":
            extra.append("--force-window")
```

4. 任务创建与审计分支：

```python
    task_id = dbm.create_task(current_app.config["DB_PATH"], project_id, type_,
                              ref_id, rec["name"], env, version,
                              previous_version, g.user["id"], extra=extra,
                              action='rollback' if rollback else
                              ('restart' if type_ == "restart" else 'deploy'))
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    submit_task(current_app.config["DB_PATH"], task, extra)
    if type_ == "restart":
        audit("restart", "service", ref_id, {"name": rec["name"], "env": env})
    else:
        detail_version = version or previous_version
        if not detail_version and auto_version:
            detail_version = "auto"
        audit("deploy" if not rollback else "rollback", type_, ref_id,
              {"name": rec["name"], "env": env, "version": detail_version})
    return jsonify({"id": task_id, "status": "pending"}), 201
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_restart_task.py -v`
Expected: `TestRestartAPI` 5 个用例全部 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_restart_task.py webapp/routes/deploy_routes.py
git commit -m "feat: 任务创建接口支持 type=restart（env 校验/窗口约束/审计）"
```

---

### Task 2: task_manager 基础设施（build_cli + _RestartHandle + kill_task）

**Files:**
- Modify: `tests/test_restart_task.py`（锚点处追加 `TestTaskManagerInfra`）
- Modify: `webapp/task_manager.py`

- [ ] **Step 1: 写失败测试** — 将 `tests/test_restart_task.py` 末尾锚点 `# ===== 后续任务追加的测试类锚点 =====` 替换为：

```python
class TestTaskManagerInfra(unittest.TestCase):
    """build_cli 对 restart 不产 CLI；kill_task 支持 restart 协作取消"""

    def test_build_cli_restart_empty(self):
        from webapp.task_manager import build_cli
        cmd = build_cli(dict(type="restart", project_id=1, name="auth",
                             env="prod", version="", previous_version=""))
        self.assertEqual(cmd, [])

    def test_build_cli_project_normal(self):
        """回归：project 类型仍组装 deploy.py CLI"""
        from webapp.task_manager import build_cli
        cmd = build_cli(dict(type="project", project_id=1, name="auth",
                             env="prod", version="1.0.0", previous_version=""))
        self.assertIn("deploy.py", cmd)

    def test_kill_task_restart_handle(self):
        from webapp.task_manager import _RestartHandle, kill_task, _task_registry
        handle = _RestartHandle()
        _task_registry[99] = handle
        try:
            ok = kill_task(99)
        finally:
            _task_registry.pop(99, None)
        self.assertTrue(ok)
        self.assertTrue(handle.cancel.is_set())


# ===== 后续任务追加的测试类锚点 =====
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_restart_task.py::TestTaskManagerInfra -v`
Expected: `ImportError: cannot import name '_RestartHandle'`（build_cli 同样因 restart 分支缺失而失败）

- [ ] **Step 3: 实现基础设施** — 修改 `webapp/task_manager.py`

1. 顶部 import 增加 `threading`：

```python
import os
import re
import signal
import sys
import threading
import time
import subprocess
```

2. `_task_registry` 定义后新增句柄类：

```python
class _RestartHandle:
    """restart 任务运行期句柄：协作取消（线程内每台服务器之间检查 cancel 事件）"""
    def __init__(self):
        self.cancel = threading.Event()
```

3. `build_cli` 开头增加 restart 分支（第 21-24 行处）：

```python
def build_cli(task: dict, extra_args: list = None) -> list:
    """根据任务记录组装 CLI 参数（db 模式）；restart 类型不产 CLI（线程内执行）"""
    if task["type"] == "restart":
        return []
    script = "deploy_pipeline.py" if task["type"] == "pipeline" else "deploy.py"
```

4. `kill_task`（第 167-205 行）在 Future 分支前后各加 handle 分支（覆盖「运行中直接命中」与「cancel 失败等待后切换为句柄」两种时序）：

```python
    entry = _task_registry.get(task_id)
    if entry is None:
        return False
    if isinstance(entry, _RestartHandle):
        entry.cancel.set()
        return True
    if isinstance(entry, Future):
        if entry.cancel():
            # 取消成功：任务不会执行，占位需手动清除（_run_task 不再有机会 pop）
            _task_registry.pop(task_id, None)
            if db_path:
                update_task(db_path, task_id, status="failed",
                            finished_at=time.time())
            return True
        # cancel 失败：_run_task 已启动但 Popen 注册可能尚未完成，短暂等待重试
        for _ in range(50):
            time.sleep(0.05)
            entry = _task_registry.get(task_id)
            if entry is not None and not isinstance(entry, Future):
                break
        else:
            # 任务已在执行且注册窗口已过但 registry 无 Popen（异常退出被 pop）
            return False
        if isinstance(entry, _RestartHandle):
            entry.cancel.set()
            return True
    proc = entry
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_restart_task.py::TestTaskManagerInfra -v`
Expected: 3 个用例全部 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_restart_task.py webapp/task_manager.py
git commit -m "feat: task_manager 支持 restart 句柄与协作取消"
```

---

### Task 3: _run_restart 执行器 + _run_task 类型分发

**Files:**
- Modify: `tests/test_restart_task.py`（锚点处追加 `TestRunRestart` + `TestDispatch`）
- Modify: `webapp/task_manager.py`

- [ ] **Step 1: 写失败测试** — 将锚点替换为：

```python
class TestRunRestart(unittest.TestCase):
    """_run_restart 执行器：多服务器并发 + 健康检查 + 汇总判定"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        self.svc_id = _make_svc(self.db, self.proj_id,
                                health_port=8080, health_url="/health")
        self.env_id = _make_env(self.db, self.proj_id)
        for i, host in enumerate(["10.0.0.1", "10.0.0.2"]):
            sid = dbm.create_record(self.db, "servers", dict(
                host=host, ssh_port=22, ssh_user="", ssh_password="",
                nacos_ip="", remark="", enabled=1))
            dbm.create_record(self.db, "env_servers", dict(
                env_id=self.env_id, server_id=sid, order_no=i))
        self.log_file = os.path.join(self.tmp, "task_1.log")
        from webapp.task_manager import _RestartHandle
        self.handle = _RestartHandle()
        self.task = dict(id=1, project_id=self.proj_id, ref_id=self.svc_id,
                         env="prod", type="restart")

    def _run(self):
        from webapp.task_manager import _run_restart
        return _run_restart(self.db, self.task, self.log_file, self.handle)

    def test_all_servers_restart_ok(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "hqb-auth"]), \
             mock.patch("webapp.monitor._probe_health_url",
                        return_value={"ok": True, "ms": 12}):
            status = self._run()
        self.assertEqual(status, "success")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("[10.0.0.1] 重启成功（健康检查 12ms）", log)
        self.assertIn("汇总：2 台成功", log)

    def test_one_server_restart_fails(self):
        def side_effect(host, port, ssh_cfg, cmds, timeout=10):
            if host == "10.0.0.1":
                return ["hqb-auth", "hqb-auth"]
            raise ConnectionError("ssh refused")
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        side_effect=side_effect):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("[10.0.0.2] 重启失败", log)
        self.assertIn("1 台失败", log)

    def test_container_missing_all_servers(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["", "hqb-auth"]):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("容器不存在，跳过", log)
        self.assertIn("未找到容器", log)

    def test_restart_output_mismatch_fails(self):
        """docker restart 输出不是容器名（stderr 错误文本）→ 判定失败"""
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "Error: No such container"]):
            status = self._run()
        self.assertEqual(status, "failed")

    def test_health_timeout_still_success(self):
        with mock.patch("webapp.monitor._ssh_exec_multi",
                        return_value=["hqb-auth", "hqb-auth"]), \
             mock.patch("webapp.monitor._probe_health_url",
                        return_value={"ok": False, "ms": -1}), \
             mock.patch("webapp.task_manager._RESTART_HEALTH_TIMEOUT", 0.1), \
             mock.patch("webapp.task_manager._RESTART_HEALTH_INTERVAL", 0.01):
            status = self._run()
        self.assertEqual(status, "success")
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("健康检查超时", log)

    def test_cancel_stops_before_servers(self):
        self.handle.cancel.set()
        with mock.patch("webapp.monitor._ssh_exec_multi") as exec_multi:
            status = self._run()
        self.assertEqual(status, "failed")
        exec_multi.assert_not_called()
        with open(self.log_file, encoding="utf-8") as f:
            log = f.read()
        self.assertIn("任务已被终止", log)


class TestDispatch(unittest.TestCase):
    """_run_task 按类型分发：restart 线程内执行，project 子进程回归"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.task = dict(id=1, project_id=1, type="restart", name="auth",
                         env="prod", version="", previous_version="", extra=[])

    def test_run_task_restart_dispatches(self):
        with mock.patch("webapp.task_manager._run_restart",
                        return_value="success") as rr, \
             mock.patch("webapp.task_manager.update_task") as upd:
            from webapp.task_manager import _run_task
            _run_task(self.db, self.task, [])
        rr.assert_called_once()
        # 最终状态落库 success
        final = upd.call_args_list[-1]
        self.assertEqual(final.kwargs.get("status"), "success")

    def test_run_task_project_still_subprocess(self):
        proc = mock.Mock()
        proc.wait.return_value = 0
        with mock.patch("webapp.task_manager.subprocess.Popen",
                        return_value=proc) as popen, \
             mock.patch("webapp.task_manager.update_task") as upd:
            from webapp.task_manager import _run_task
            task = dict(self.task, type="project", name="auth")
            _run_task(self.db, task, [])
        popen.assert_called_once()
        final = upd.call_args_list[-1]
        self.assertEqual(final.kwargs.get("status"), "success")


# ===== 后续任务追加的测试类锚点 =====
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_restart_task.py -v`
Expected: `TestRunRestart` 全部 FAIL（`ImportError: cannot import name '_run_restart'`）；`TestDispatch.test_run_task_restart_dispatches` FAIL（`_run_task` 未分发，走 Popen 路径，`_run_restart` 未被调用）；`TestDispatch.test_run_task_project_still_subprocess` 与 `TestTaskManagerInfra`/`TestRestartAPI` 仍 PASS

- [ ] **Step 3: 实现执行器与分发** — 修改 `webapp/task_manager.py`

1. `_run_task`（第 96-131 行）在 `update_task(..., status="running", ...)` 之后插入 restart 分支：

```python
        if task["type"] == "restart":
            # 重启任务：线程内直接执行（不产 CLI/子进程），日志由 _run_restart 写入；
            # 注册 _RestartHandle 供 kill_task 协作取消（覆盖排队期 Future 占位）
            handle = _RestartHandle()
            _task_registry[task["id"]] = handle
            try:
                status = _run_restart(db_path, task, str(log_file), handle)
            finally:
                _task_registry.pop(task["id"], None)
            update_task(db_path, task["id"], status=status, finished_at=time.time())
            return
        # 执行参数以持久化到任务记录的 extra 为准（submit 传入值与其一致），
        # 保证重新运行时恢复的参数与原始执行一致
        extra = extra_args if extra_args else (task.get("extra") or [])
```

2. 文件末尾新增常量与 `_run_restart`（含局部导入 dbm/monitor，与现有局部导入风格一致）：

```python
# restart 健康检查轮询参数（测试可 patch 缩短，避免真实等待 60s）
_RESTART_HEALTH_TIMEOUT = 60.0
_RESTART_HEALTH_INTERVAL = 5.0


def _run_restart(db_path: str, task: dict, log_file: str,
                 handle: "_RestartHandle") -> str:
    """执行服务重启：并发对每台服务器 docker restart + 健康检查。

    返回 "success" / "failed"。日志逐行写入 log_file（每台一行 + 汇总行）。
    复用 monitor 的 SSH 连接池（同进程），凭据合并语义与监控探测一致。
    """
    import shlex
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from . import db as dbm
    from . import monitor as monitor_mod

    rec = dbm.get_record(db_path, "services", task["ref_id"])
    env_obj = dbm.get_env_by_name(db_path, task["project_id"], task["env"])
    f = open(log_file, "w", encoding="utf-8")
    try:
        if not rec:
            f.write("[platform] 服务记录不存在，重启中止\n")
            return "failed"
        svc_name = rec.get("service_name") or ""
        if not svc_name:
            f.write(f"[platform] 服务「{rec['name']}」未配置容器名（service_name），重启中止\n")
            return "failed"
        if not env_obj:
            f.write(f"[platform] 环境「{task['env']}」不存在，重启中止\n")
            return "failed"
        servers = dbm.list_env_servers(db_path, env_obj["id"])
        if not servers:
            f.write("[platform] 环境未绑定服务器，重启中止\n")
            return "failed"
        f.write(f"[platform] 重启服务「{rec['name']}」（容器名 {svc_name}），"
                f"环境「{task['env']}」，共 {len(servers)} 台服务器\n")
        f.flush()
        env_ssh = env_obj.get("ssh") or {}
        health_port = int(rec.get("health_port") or 0)
        health_url = rec.get("health_url") or "/"
        results = []  # (host, status, msg)，status ∈ ok/skipped/failed/cancelled

        def restart_one(server: dict) -> tuple:
            host = server["host"]
            port = int(server.get("ssh_port") or 22)
            ssh_cfg = monitor_mod._merge_server_ssh(env_ssh, server)
            if handle.cancel.is_set():
                return host, "cancelled", "任务已终止，跳过"
            try:
                # 容器存在性精确匹配（grep -F 字面量、-x 整行，避免 hqb-auth 命中
                # hqb-auth-bak）；docker restart 成功时 stdout 为容器名，失败时
                # stderr 返回错误文本（_ssh_exec_multi 返回 out or err）
                outs = monitor_mod._ssh_exec_multi(host, port, ssh_cfg, [
                    "docker ps --format '{{.Names}}' | grep -Fx " + shlex.quote(svc_name),
                    "docker restart " + shlex.quote(svc_name),
                ], timeout=60)
            except Exception as exc:
                return host, "failed", f"SSH/执行失败: {exc}"
            if not outs[0].strip():
                return host, "skipped", "容器不存在，跳过"
            if outs[1].strip() != svc_name:
                return host, "failed", f"docker restart 失败: {outs[1].strip() or '无输出'}"
            if health_port <= 0 or handle.cancel.is_set():
                return host, "ok", "重启成功"
            # 健康检查：轮询 health_port+health_url 最多 60s；超时仅警告（应用启动慢属常见）
            t0 = time.time()
            while time.time() - t0 < _RESTART_HEALTH_TIMEOUT:
                if handle.cancel.is_set():
                    return host, "cancelled", "任务已终止，跳过"
                r = monitor_mod._probe_health_url(host, health_port, health_url)
                if r["ok"]:
                    return host, "ok", f"重启成功（健康检查 {r['ms']}ms）"
                time.sleep(_RESTART_HEALTH_INTERVAL)
            return host, "ok", "重启成功（健康检查超时（应用启动慢？），仍视为成功）"

        with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
            futures = [pool.submit(restart_one, s) for s in servers]
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as exc:
                    results.append(("?", "failed", f"执行异常: {exc}"))
        # 按服务器绑定顺序输出日志（并发结果乱序，先排回 order_no 顺序）
        order = {s["host"]: i for i, s in enumerate(servers)}
        results.sort(key=lambda r: order.get(r[0], len(servers)))
        for host, status, msg in results:
            f.write(f"[{host}] {msg}\n")
        ok_count = sum(1 for _, st, _ in results if st == "ok")
        fail_count = sum(1 for _, st, _ in results if st == "failed")
        skip_count = sum(1 for _, st, _ in results if st == "skipped")
        if handle.cancel.is_set():
            f.write("[platform] 任务已被终止\n")
            return "failed"
        if fail_count:
            f.write(f"[platform] 汇总：{ok_count} 台成功，{fail_count} 台失败，"
                    f"{skip_count} 台跳过 → 任务失败\n")
            return "failed"
        if ok_count == 0:
            f.write(f"[platform] 汇总：全部 {skip_count} 台未找到容器 → 任务失败\n")
            return "failed"
        f.write(f"[platform] 汇总：{ok_count} 台成功，{skip_count} 台跳过 → 任务成功\n")
        return "success"
    finally:
        f.flush()
        f.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_restart_task.py -v`
Expected: 全部 PASS（TestRestartAPI 5 + TestTaskManagerInfra 3 + TestRunRestart 6 + TestDispatch 2）

- [ ] **Step 5: Commit**

```bash
git add tests/test_restart_task.py webapp/task_manager.py
git commit -m "feat: 服务重启执行器 _run_restart（SSH 并发 + 健康检查 + 汇总判定）"
```

---

### Task 4: 前端 devices 服务卡片「重启」按钮

**Files:**
- Modify: `webapp/static/js/pages/devices.js`

- [ ] **Step 1: 扩展 running 匹配条件**（第 84 行）— 重启任务命中同一服务卡片：

```js
      const run = running.find(r =>
        (r.type === 'project' && r.name === s.name && r.env === state.currentEnv) ||
        (r.type === 'restart' && r.ref_id === s.id && r.env === state.currentEnv));
```

- [ ] **Step 2: 卡片 foot 增加重启按钮**（第 104-105 行）— `canDeploy()` 分支内「回滚」旁：

```js
          ${canDeploy() ? `<button class="btn sm primary" data-pub="${s.id}">发布</button>
          <button class="btn sm ghost" data-roll="${s.id}">回滚</button>
          <button class="btn sm ghost" data-restart="${s.id}">⭯ 重启</button>` : '<span class="weak" style="font-size:11px">只读账号</span>'}
```

- [ ] **Step 3: 绑定点击**（第 111 行后追加）：

```js
    wall.querySelectorAll('[data-restart]').forEach(b => b.onclick = () => restartService(Number(b.dataset.restart)));
```

- [ ] **Step 4: 新增 restartService** — 在 `openDeployDrawer`（第 137 行）之前插入：

```js
  // ---- 服务重启：确认 → 创建 restart 任务 → 跳发布中心（与发布/回滚同款窗口二次确认）----
  async function restartService(sid){
    const pid = state.currentProject.id;
    let services;
    try{ services = await api(`/api/projects/${pid}/services`); }
    catch(e){ toastError('服务信息加载失败：' + e.message); return; }
    if (destroyed) return; // 路由切换后在途响应丢弃，防新页面弹出旧流程
    const svc = services.find(s => s.id === sid);
    if (!svc) return;
    if (!await confirmDialog(`确认重启服务「${svc.name}」？环境：${state.currentEnv}，将重启全部实例`)) return;
    const doRestart = async (forceWindow) => await api('/api/tasks', {method:'POST', body: JSON.stringify({
      project_id: pid, type: 'restart', ref_id: sid, env: state.currentEnv, force_window: forceWindow})});
    try{
      const resp = await doRestart(false);
      toastSuccess(`任务 #${resp.id} 已创建`);
      location.hash = '#/deploy';
    }catch(e){
      if (e.message === 'window_warn' && e.data && e.data.window){
        if (!await confirmDialog(`当前处于发布窗口「${e.data.window.name}」（软警告），确认仍要继续重启？`)) return;
        try{
          const resp2 = await doRestart(true);
          toastSuccess(`任务 #${resp2.id} 已创建（已忽略窗口警告）`);
          location.hash = '#/deploy';
        }catch(e2){ toastError(e2.message); }
      } else { toastError(e.message); }
    }
  }
```

- [ ] **Step 5: 代码级检查**

Run: `python -m pytest tests/ -q`（回归确认后端无破坏）
Expected: 全部 PASS；无 JS 语法错误（用浏览器打开页面确认无 console 报错，详细实测在 Task 7）

- [ ] **Step 6: Commit**

```bash
git add webapp/static/js/pages/devices.js
git commit -m "feat: 设备总览服务卡片增加重启按钮与发布中状态匹配"
```

---

### Task 5: 前端 deploy 发布中心适配

**Files:**
- Modify: `webapp/static/js/pages/deploy.js`

- [ ] **Step 1: 操作列标签**（第 55 行）— restart 显示 tag-info「重启」：

```js
            <td>${t.action === 'rollback' ? '<span class="tag tag-warn">回滚</span>' : t.action === 'restart' ? '<span class="tag tag-info">重启</span>' : '<span class="tag tag-ok">发布</span>'}</td>
```

- [ ] **Step 2: 列表类型列**（第 56 行）：

```js
            <td>${t.type === 'pipeline' ? '流水线' : t.type === 'restart' ? '重启' : '服务'}</td>
```

- [ ] **Step 3: 详情抽屉操作/类型/发布版本**（第 79-82 行）三处：

```js
        <div><div class="dg-label">操作</div><div style="margin-top:2px">${task.action === 'rollback' ? '<span class="tag tag-warn">回滚</span>' : task.action === 'restart' ? '<span class="tag tag-info">重启</span>' : '<span class="tag tag-ok">发布</span>'}</div></div>
        <div><div class="dg-label">类型</div><div style="margin-top:2px">${task.type === 'pipeline' ? '流水线' : task.type === 'restart' ? '重启' : '服务'}</div></div>
        <div><div class="dg-label">发布版本</div><div class="num" style="margin-top:2px">${task.action === 'restart' ? '-' : task.action === 'rollback' ? esc(task.version || task.previous_version || '-') : esc(task.previous_version || '-') + ' → ' + esc(task.version || '-')}</div></div>
```

- [ ] **Step 4: 操作筛选 chips**（第 264 行）— 增加「重启」：

```js
          ${[['all','全部'],['deploy','发布'],['rollback','回滚'],['restart','重启']].map(([k, v]) => `<span class="chip ${k === actionFilter ? 'active' : ''}" data-act="${k}">${v}</span>`).join('')}
```

- [ ] **Step 5: Commit**

```bash
git add webapp/static/js/pages/deploy.js
git commit -m "feat: 发布中心适配 restart 任务（标签/类型/筛选）"
```

---

### Task 6: tag-info 样式变体

**Files:**
- Modify: `webapp/static/css/components.css`

- [ ] **Step 1: 新增 tag-info** — 在 `.tag.tag-warn`（第 69 行）后追加：

```css
.tag.tag-info{background:rgba(0,212,255,.12);color:var(--primary);border:1px solid rgba(0,212,255,.35)}
```

- [ ] **Step 2: Commit**

```bash
git add webapp/static/css/components.css
git commit -m "style: 新增 tag-info 标签变体（重启操作）"
```

---

### Task 7: 全量回归 + 浏览器实测

**Files:**
- 无代码改动（纯验证）

- [ ] **Step 1: 全量测试回归**

Run: `python -m pytest tests/ -q`
Expected: 全部 PASS（含既有 test_deploy_enhance 等，确认无回归）

- [ ] **Step 2: 启动服务**（先确认 5678 端口无旧实例占用，避免 Windows 下验证到旧代码）

```bash
netstat -ano | findstr :5678
python webapp/app.py --host 127.0.0.1 --port 5678
```

- [ ] **Step 3: 浏览器实测 — 管理员视角**（admin 登录，用 chrome-devtools MCP 操作）

验证点（严格对照规格）：
1. 项目总览 → 服务卡片 foot 出现「⭯ 重启」按钮（发布/回滚旁）
2. 点击重启 → 确认框文案含服务名与当前环境 → 确认 → toast「任务 #N 已创建」→ 自动跳转发布中心
3. 发布中心：操作筛选出现「重启」chip；新任务操作列显示青蓝色「重启」标签；类型列「重启」；版本列「-」
4. 任务详情抽屉：「发布版本」显示「-」；日志区显示每台服务器一行（`[host] 重启成功（健康检查 Xms）`）+ 汇总行；终态徽标正确
5. 重启任务 running 期间返回项目总览：该服务卡片橙灯 + 「发布中」标签 + publishing 描边；任务结束后恢复在线状态
6. 重新运行：对 failed 任务点「重新运行」→ 新 restart 任务创建成功
7. 终止：对 running restart 任务点「终止」→ 日志出现「任务已被终止」、状态 failed

- [ ] **Step 4: 浏览器实测 — 窗口警告与只读账号**

1. 在发布窗口管理建一个 warn 级窗口 → 卡片点重启 → 二次确认框（含窗口名）→ 确认后成功创建
2. viewer 只读账号登录 → 卡片无重启按钮；直接 POST /api/tasks type=restart → 403
3. 无 console 报错（重点检查 `[data-restart]` 绑定与 run 匹配扩展无异常）

- [ ] **Step 5: Commit 收尾**（若实测发现问题，修复后提交）

---

## Self-Review 记录

**规格覆盖检查：**
- §后端 1（任务创建）：Task 1 ✓（type 校验、_resolve_ref、env 归属、窗口、action/audit）
- §后端 2（执行器）：Task 2/3 ✓（build_cli 空、_run_task 分发、_run_restart 全流程、健康检查轮询、汇总判定、每台一行日志）
- §后端 3（边界）：Task 1 env 400/ref 404 ✓；Task 3 单台失败/全部失败/全部跳过/健康检查超时 ✓；停用服务允许重启（未过滤 enabled，Task 1 无 enabled 校验）✓
- §前端 1（devices）：Task 4 ✓（按钮、确认、window_warn、状态匹配扩展）
- §前端 2（deploy）：Task 5 ✓（操作列/类型列/抽屉/筛选；rerun 由后端分支天然支持，前端无需改）
- §前端 3（样式）：Task 6 ✓
- §测试计划：Task 1 路由 5 用例、Task 3 执行层 6 用例 + 分发 2 用例 ✓；日志断言覆盖每台一行 + 汇总行 ✓

**规格未明确、计划补充的点（与规格目标一致）：**
1. 运行中 restart 任务的「终止」：规格声明复用终止能力但未定义机制 → 以 `_RestartHandle` 协作取消实现（每台服务器之间检查），排队期取消沿用 Future 逻辑
2. docker restart 成败判定：规格未指定判定信号 → 以「成功时 stdout 为容器名、失败时 stderr 错误文本」判定（`_ssh_exec_multi` 返回 `out or err`）
3. 健康检查轮询参数提取为模块常量 `_RESTART_HEALTH_TIMEOUT/_INTERVAL`，测试 patch 缩短避免真实 60s 等待

**类型/命名一致性：** `_RestartHandle.cancel`（threading.Event）在 Task 2 定义、Task 3 使用、Task 4 无涉及；`_run_restart(db_path, task, log_file, handle)` 签名在 Task 3 测试与实现一致；`restartService(sid)` 与 `data-restart` 绑定一致；`tag-info` 在 Task 5 引用、Task 6 定义，类名 `.tag.tag-info` 一致。
