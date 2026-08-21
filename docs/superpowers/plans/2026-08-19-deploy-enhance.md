# 发布流程增强 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有发布骨架上增加定时发布、窗口管控、灰度自动推进、影响分析、发布对比、Webhook 触发 6 项企业级发布管控能力。

**Architecture:** 新增 `DeployScheduler` 守护线程（与 `MonitorScheduler`/`AlertEngine` 同模式），5 张新表（deploy_schedules, release_windows, service_dependencies, deployment_snapshots, canary_configs），1 个新蓝图 `deploy_enhance_bp`，5 个新前端页面模块。

**Tech Stack:** Python/Flask, SQLite, croniter (新增依赖), SVG 力导向图 (原生 JS), 现有 JWT 认证体系

**Spec:** `docs/superpowers/specs/2026-08-19-deploy-enhance-design.md`

---

## 文件结构总览

### 新增文件

| 文件 | 职责 |
|------|------|
| `webapp/deploy_scheduler.py` | DeployScheduler 守护线程 + 窗口检查 + CRON 计算 |
| `webapp/routes/deploy_enhance_routes.py` | 发布增强 API 蓝图（ schedules/windows/canary/topology/snapshots/webhook ） |
| `webapp/static/js/pages/deploy_schedules.js` | 定时计划前端 |
| `webapp/static/js/pages/deploy_windows.js` | 发布窗口前端 |
| `webapp/static/js/pages/deploy_canary.js` | 灰度策略前端 |
| `webapp/static/js/pages/deploy_topology.js` | 依赖拓扑前端 |
| `webapp/static/js/pages/deploy_compare.js` | 发布对比前端 |
| `webapp/static/css/pages/deploy-enhance.css` | 发布增强样式 |
| `tests/test_deploy_enhance.py` | 后端单元测试 |

### 修改文件

| 文件 | 修改 |
|------|------|
| `webapp/db.py` | 新增 5 张表 SCHEMA + TABLES 白名单 + JSON_FIELDS |
| `webapp/task_manager.py` | 新增 `extra` 字段中 `forced`/`schedule_id` 标记支持 |
| `webapp/app.py` | 注册新蓝图 + 启动 DeployScheduler |
| `webapp/static/js/router.js` | 添加 5 个新页面路由 |
| `webapp/static/js/layout.js` | 无需修改（deploy 子页面通过 router sub 路由） |
| `webapp/static/js/app.js` | 注册新页面模块 |
| `webapp/templates/screen.html` | 引入新 CSS + JS 文件 |
| `requirements.txt` | 添加 `croniter` |

---

## Task 1: 数据模型 + 依赖

**Files:**
- Modify: `webapp/db.py` — SCHEMA 新增 5 张表 + TABLES 白名单 + JSON_FIELDS
- Modify: `requirements.txt` — 添加 croniter
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写测试 — 验证新表可创建和查询**

```python
# tests/test_deploy_enhance.py
"""发布流程增强测试：数据模型 + 窗口检查 + 调度器 + API"""
import json
import os
import tempfile
import time
import unittest

from webapp import db as dbm


class TestDeployEnhanceSchema(unittest.TestCase):
    """新增 5 张表的建表与 CRUD"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))

    def test_deploy_schedules_crud(self):
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="test schedule",
            ref_type="project", ref_id=1, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=int(time.time()) + 3600, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=int(time.time()) + 3600,
            created_by=1, created_at=int(time.time())))
        rec = dbm.get_record(self.db, "deploy_schedules", sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["name"], "test schedule")
        self.assertEqual(rec["type"], "once")

    def test_release_windows_crud(self):
        wid = dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="春节封网",
            start_at=1700000000, end_at=1700100000,
            block_level="block", repeat_yearly=1,
            created_by=1, created_at=int(time.time())))
        rec = dbm.get_record(self.db, "release_windows", wid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["block_level"], "block")

    def test_service_dependencies_crud(self):
        did = dbm.create_record(self.db, "service_dependencies", dict(
            project_id=self.proj_id, source_service="gateway",
            target_service="auth", dep_type="dubbo",
            created_at=int(time.time())))
        rec = dbm.get_record(self.db, "service_dependencies", did)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["dep_type"], "dubbo")

    def test_deployment_snapshots_crud(self):
        sid = dbm.create_record(self.db, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=0,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="abc123", env_vars_hash="def456",
            deployed_at=int(time.time())))
        rec = dbm.get_record(self.db, "deployment_snapshots", sid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["version"], "1.0.0")

    def test_canary_configs_crud(self):
        steps = [{"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True}]
        cid = dbm.create_record(self.db, "canary_configs", dict(
            project_id=self.proj_id, name="default",
            steps=json.dumps(steps), is_default=1,
            created_at=int(time.time())))
        rec = dbm.get_record(self.db, "canary_configs", cid)
        self.assertIsNotNone(rec)
        parsed = json.loads(rec["steps"])
        self.assertEqual(len(parsed), 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceSchema -v`
Expected: FAIL — 表不存在

- [ ] **Step 3: 在 db.py SCHEMA 中添加 5 张新表**

在 `webapp/db.py` 的 `SCHEMA` 字符串末尾（`"""` 之前）追加：

```python
CREATE TABLE IF NOT EXISTS deploy_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    type TEXT NOT NULL DEFAULT 'once',
    name TEXT NOT NULL,
    ref_type TEXT DEFAULT 'project',
    ref_id INTEGER DEFAULT 0,
    env TEXT DEFAULT '',
    version TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    extra TEXT DEFAULT '[]',
    schedule_at INTEGER DEFAULT 0,
    cron_expr TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    last_run_at INTEGER DEFAULT 0,
    next_run_at INTEGER DEFAULT 0,
    created_by INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS release_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    block_level TEXT NOT NULL DEFAULT 'warn',
    repeat_yearly INTEGER DEFAULT 0,
    created_by INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS service_dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    source_service TEXT NOT NULL,
    target_service TEXT NOT NULL,
    dep_type TEXT NOT NULL DEFAULT 'manual',
    created_at INTEGER NOT NULL,
    UNIQUE(project_id, source_service, target_service)
);
CREATE TABLE IF NOT EXISTS deployment_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    env TEXT NOT NULL DEFAULT '',
    task_id INTEGER DEFAULT 0,
    version TEXT DEFAULT '',
    image_versions TEXT DEFAULT '{}',
    nacos_config_hash TEXT DEFAULT '',
    env_vars_hash TEXT DEFAULT '',
    deployed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS canary_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    name TEXT NOT NULL,
    steps TEXT NOT NULL DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at INTEGER NOT NULL
);
```

- [ ] **Step 4: 更新 TABLES 白名单和 JSON_FIELDS**

在 `db.py` 中修改 `JSON_FIELDS` 和 `TABLES`：

```python
# JSON_FIELDS 追加：
JSON_FIELDS = {
    # ... 现有条目 ...
    "deploy_schedules": {"extra"},
    "deployment_snapshots": {"image_versions"},
    "canary_configs": {"steps"},
}

# TABLES 追加：
TABLES = frozenset(("projects", "services", "environments", "servers",
                    "env_servers", "pipelines", "users", "tasks", "audit_logs",
                    "alert_rules", "alerts", "alert_events",
                    "silence_windows", "platform_settings",
                    "log_entries", "log_scan_state",
                    "deploy_schedules", "release_windows",
                    "service_dependencies", "deployment_snapshots",
                    "canary_configs"))
```

- [ ] **Step 5: 添加 croniter 到 requirements.txt**

```
croniter>=1.3.0
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceSchema -v`
Expected: 5 tests PASS

- [ ] **Step 7: 提交**

```bash
git add webapp/db.py requirements.txt tests/test_deploy_enhance.py
git commit -m "feat(deploy-enhance): add 5 new tables and croniter dependency"
```

---

## Task 2: 发布窗口检查逻辑

**Files:**
- Create: `webapp/deploy_scheduler.py` — 先实现窗口检查函数
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写窗口检查测试**

在 `tests/test_deploy_enhance.py` 追加：

```python
class TestReleaseWindowCheck(unittest.TestCase):
    """发布窗口检查逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))

    def test_no_windows_returns_none(self):
        from webapp.deploy_scheduler import check_release_window
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNone(result)

    def test_block_window_detected(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="封网", start_at=now - 100, end_at=now + 100,
            block_level="block", repeat_yearly=0, created_by=1, created_at=now))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_level"], "block")
        self.assertEqual(result["name"], "封网")

    def test_warn_window_detected(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=self.proj_id, name="警告窗口",
            start_at=now - 50, end_at=now + 50,
            block_level="warn", repeat_yearly=0,
            created_by=1, created_at=now))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["block_level"], "warn")

    def test_repeat_yearly_matches(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        # 创建去年同时间段的窗口（repeat_yearly=1）
        one_year = 365 * 24 * 3600
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="春节封网",
            start_at=now - one_year - 100, end_at=now - one_year + 100,
            block_level="block", repeat_yearly=1,
            created_by=1, created_at=now - one_year))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "春节封网")

    def test_window_not_active(self):
        from webapp.deploy_scheduler import check_release_window
        now = int(time.time())
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="过期窗口",
            start_at=now - 200, end_at=now - 100,
            block_level="block", repeat_yearly=0,
            created_by=1, created_at=now - 200))
        result = check_release_window(self.db, self.proj_id)
        self.assertIsNone(result)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestReleaseWindowCheck -v`
Expected: FAIL — `ImportError: cannot import name 'check_release_window'`

- [ ] **Step 3: 实现窗口检查函数**

创建 `webapp/deploy_scheduler.py`：

```python
"""发布调度器：定时发布 + 窗口管控 + 灰度推进"""
import sys
import threading
import time


def check_release_window(db_path: str, project_id: int) -> dict | None:
    """检查当前是否命中发布窗口。

    返回 None 表示未命中任何窗口；
    返回 {"id", "name", "block_level"} 表示命中。
    repeat_yearly=1 的窗口将年份偏移到当前年份后比较。
    优先匹配项目级窗口（project_id 匹配），其次全局窗口（project_id=0）。
    """
    now = int(time.time())
    windows = []
    # 项目级 + 全局窗口
    for w in _list_windows(db_path, project_id):
        if _window_matches(w, now):
            windows.append(w)
    if not windows:
        return None
    # block 优先于 warn
    blocks = [w for w in windows if w["block_level"] == "block"]
    return blocks[0] if blocks else windows[0]


def _list_windows(db_path: str, project_id: int) -> list:
    """查询项目级 + 全局窗口"""
    from . import db as dbm
    return dbm.list_records(
        db_path, "release_windows",
        "project_id=? OR project_id=0", (project_id,))


def _window_matches(w: dict, now: int) -> bool:
    """判断窗口在当前时刻是否生效（支持年度重复）"""
    start = w["start_at"]
    end = w["end_at"]
    if w.get("repeat_yearly"):
        # 将窗口的年份偏移到当前年份
        import time as _t
        cur_year = _t.localtime(now).tm_year
        win_year = _t.localtime(start).tm_year
        offset = (cur_year - win_year) * 365 * 24 * 3600
        # 粗略偏移（忽略闰年），精确到天级别足够
        start += offset
        end += offset
    return start <= now <= end
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestReleaseWindowCheck -v`
Expected: 5 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/deploy_scheduler.py tests/test_deploy_enhance.py
git commit -m "feat(deploy-enhance): add release window check logic"
```

---

## Task 3: DeployScheduler 调度器

**Files:**
- Modify: `webapp/deploy_scheduler.py` — 添加 DeployScheduler 类
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写调度器测试**

在 `tests/test_deploy_enhance.py` 追加：

```python
class TestDeployScheduler(unittest.TestCase):
    """DeployScheduler 调度逻辑"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        # 创建服务和环境（submit_task 需要）
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

    def test_once_schedule_becomes_disabled_after_run(self):
        from webapp.deploy_scheduler import DeployScheduler
        now = int(time.time())
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="once test",
            ref_type="project", ref_id=self.svc_id, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=now - 1, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=now - 1,
            created_by=1, created_at=now))
        scheduler = DeployScheduler(self.db)
        # 手动触发一轮扫描（不启动线程）
        due = scheduler._find_due_schedules()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["id"], sid)

    def test_cron_next_run_calculation(self):
        from webapp.deploy_scheduler import DeployScheduler
        scheduler = DeployScheduler(self.db)
        # 每天凌晨 2 点
        next_ts = scheduler._calc_next_run("cron", "0 2 * * *",
                                            int(time.time()))
        self.assertGreater(next_ts, time.time())

    def test_block_window_skips_schedule(self):
        from webapp.deploy_scheduler import DeployScheduler
        now = int(time.time())
        # 创建 block 窗口覆盖当前时间
        dbm.create_record(self.db, "release_windows", dict(
            project_id=0, name="封网期",
            start_at=now - 100, end_at=now + 3600,
            block_level="block", repeat_yearly=0,
            created_by=1, created_at=now))
        scheduler = DeployScheduler(self.db)
        # 有到期计划但命中 block 窗口
        sid = dbm.create_record(self.db, "deploy_schedules", dict(
            project_id=self.proj_id, type="once", name="blocked",
            ref_type="project", ref_id=self.svc_id, env="prod",
            version="1.0.0", previous_version="", extra="[]",
            schedule_at=now - 1, cron_expr="",
            enabled=1, last_run_at=0, next_run_at=now - 1,
            created_by=1, created_at=now))
        due = scheduler._find_due_schedules()
        self.assertEqual(len(due), 1)
        # 执行周期处理：应跳过（不创建任务）
        scheduler._process_due(due)
        # 计划仍然 enabled（block 跳过但 once 不 disable）
        rec = dbm.get_record(self.db, "deploy_schedules", sid)
        self.assertEqual(rec["enabled"], 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestDeployScheduler -v`
Expected: FAIL — `DeployScheduler` 不存在

- [ ] **Step 3: 实现 DeployScheduler 类**

在 `webapp/deploy_scheduler.py` 追加 `DeployScheduler` 类：

```python
class DeployScheduler:
    """发布调度守护线程：扫描到期计划 → 窗口检查 → 创建任务"""

    def __init__(self, db_path: str, interval: int = 30):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="deploy-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                due = self._find_due_schedules()
                if due:
                    self._process_due(due)
            except Exception as e:
                print(f"[deploy-sched] 周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _find_due_schedules(self) -> list:
        """查找所有到期且启用的发布计划"""
        from . import db as dbm
        now = int(time.time())
        return dbm.list_records(
            self._db_path, "deploy_schedules",
            "enabled=1 AND next_run_at > 0 AND next_run_at <= ?",
            (now,), "next_run_at")

    def _process_due(self, schedules: list):
        """处理到期计划：窗口检查 → 创建任务 → 更新下次执行时间"""
        from . import db as dbm
        now = int(time.time())
        for sched in schedules:
            # 窗口检查
            window = check_release_window(self._db_path, sched["project_id"])
            if window and window["block_level"] == "block":
                # block 窗口：跳过执行，更新 next_run_at
                self._update_next_run(sched, now)
                print(f"[deploy-sched] 跳过 {sched['name']}：命中封网窗口 '{window['name']}'")
                continue

            forced = 0
            if window and window["block_level"] == "warn":
                forced = 1

            # 创建任务
            try:
                extra = json.loads(sched.get("extra") or "[]")
                if forced:
                    extra.append("--force-window")
                task_id = dbm.create_task(
                    self._db_path, sched["project_id"],
                    sched["ref_type"], sched["ref_id"],
                    sched["name"], sched["env"],
                    sched["version"], sched["previous_version"],
                    sched["created_by"], extra=extra)
                # 提交任务执行
                from . import task_manager
                task = dbm.get_task(self._db_path, task_id)
                task_manager.submit_task(self._db_path, task, extra)
                print(f"[deploy-sched] 定时任务 #{task_id} 已创建: {sched['name']}")
            except Exception as e:
                print(f"[deploy-sched] 创建任务失败: {e!r}", file=sys.stderr)

            self._update_next_run(sched, now)

    def _update_next_run(self, sched: dict, now: int):
        """更新 last_run_at 和 next_run_at"""
        from . import db as dbm
        updates = {"last_run_at": now}
        if sched["type"] == "cron" and sched.get("cron_expr"):
            next_ts = self._calc_next_run("cron", sched["cron_expr"], now)
            updates["next_run_at"] = next_ts
        else:
            # once 模式：执行后禁用
            updates["enabled"] = 0
            updates["next_run_at"] = 0
        dbm.update_record(self._db_path, "deploy_schedules", sched["id"], updates)

    def _calc_next_run(self, sched_type: str, cron_expr: str,
                       base_ts: int) -> int:
        """计算下次执行时间"""
        if sched_type != "cron" or not cron_expr:
            return 0
        try:
            from croniter import croniter
            cron = croniter(cron_expr, time.localtime(base_ts))
            next_dt = cron.get_next()
            return int(next_dt)
        except (ImportError, ValueError, KeyError) as e:
            print(f"[deploy-sched] CRON 解析失败 '{cron_expr}': {e!r}",
                  file=sys.stderr)
            return 0
```

需要在文件顶部添加 `import json`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestDeployScheduler -v`
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/deploy_scheduler.py tests/test_deploy_enhance.py
git commit -m "feat(deploy-enhance): add DeployScheduler with cron support"
```

---

## Task 4: 发布增强 API 路由

**Files:**
- Create: `webapp/routes/deploy_enhance_routes.py`
- Modify: `webapp/app.py` — 注册蓝图
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写 API 测试**

在 `tests/test_deploy_enhance.py` 追加：

```python
class TestDeployEnhanceAPI(unittest.TestCase):
    """发布增强 API 端点测试"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db_path)
        from webapp import create_app
        self.app = create_app()
        self.app.config["DB_PATH"] = self.db_path
        self.app.config["TESTING"] = True
        # 创建测试数据
        with self.app.app_context():
            self.proj_id = dbm.create_record(self.db_path, "projects", dict(
                name="p1", code="p1", description="", git_org="", created_at=int(time.time())))
            self.svc_id = dbm.create_record(self.db_path, "services", dict(
                project_id=self.proj_id, name="auth", image_name="auth",
                service_name="hqb-auth", docker_context="", dockerfile="",
                build_order="[]", nacos_groups="[]", dubbo_services="[]",
                snailjob_group="", health_url="", health_port=0,
                log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
            # 创建 admin 用户并获取 token
            from werkzeug.security import generate_password_hash
            dbm.create_record(self.db_path, "users", dict(
                username="admin", password_hash=generate_password_hash("test123"),
                role="admin", must_change_password=0, enabled=1,
                created_at=int(time.time())))
        self.client = self.app.test_client()
        # 登录获取 token
        resp = self.client.post("/api/login", json={"username": "admin", "password": "test123"})
        self.token = resp.get_json()["token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_list_schedules_empty(self):
        resp = self.client.get("/api/deploy/schedules", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), [])

    def test_create_schedule(self):
        resp = self.client.post("/api/deploy/schedules", headers=self.headers, json={
            "project_id": self.proj_id, "type": "once", "name": "test",
            "ref_type": "project", "ref_id": self.svc_id, "env": "prod",
            "version": "1.0.0", "schedule_at": int(time.time()) + 3600
        })
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertIn("id", data)

    def test_create_release_window(self):
        now = int(time.time())
        resp = self.client.post("/api/deploy/windows", headers=self.headers, json={
            "project_id": 0, "name": "封网测试",
            "start_at": now, "end_at": now + 3600,
            "block_level": "block"
        })
        self.assertEqual(resp.status_code, 201)

    def test_check_window_api(self):
        now = int(time.time())
        dbm.create_record(self.db_path, "release_windows", dict(
            project_id=0, name="active", start_at=now - 10, end_at=now + 100,
            block_level="warn", repeat_yearly=0, created_by=1, created_at=now))
        resp = self.client.get("/api/deploy/windows/check?project_id=1",
                               headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsNotNone(data)
        self.assertEqual(data["block_level"], "warn")

    def test_create_canary_config(self):
        resp = self.client.post("/api/deploy/canary-configs", headers=self.headers, json={
            "project_id": self.proj_id, "name": "default",
            "steps": [{"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True}]
        })
        self.assertEqual(resp.status_code, 201)

    def test_viewer_cannot_create(self):
        # 创建 viewer 用户
        from werkzeug.security import generate_password_hash
        dbm.create_record(self.db_path, "users", dict(
            username="viewer1", password_hash=generate_password_hash("test123"),
            role="viewer", must_change_password=0, enabled=1,
            created_at=int(time.time())))
        resp = self.client.post("/api/login", json={"username": "viewer1", "password": "test123"})
        vtoken = resp.get_json()["token"]
        resp = self.client.post("/api/deploy/schedules", 
                                headers={"Authorization": f"Bearer {vtoken}"},
                                json={"project_id": self.proj_id, "type": "once", "name": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_topology_crud(self):
        # 添加依赖
        resp = self.client.post("/api/deploy/topology", headers=self.headers, json={
            "project_id": self.proj_id, "source_service": "gateway",
            "target_service": "auth", "dep_type": "dubbo"
        })
        self.assertEqual(resp.status_code, 201)
        dep_id = resp.get_json()["id"]
        # 获取拓扑
        resp = self.client.get(f"/api/deploy/topology/{self.proj_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["edges"]), 1)
        # 删除
        resp = self.client.delete(f"/api/deploy/topology/{dep_id}", headers=self.headers)
        self.assertEqual(resp.status_code, 200)

    def test_snapshots_and_compare(self):
        now = int(time.time())
        # 创建两个快照
        dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=1,
            version="1.0.0", image_versions='{"auth":"auth:1.0.0"}',
            nacos_config_hash="aaa", env_vars_hash="eee",
            deployed_at=now - 100))
        dbm.create_record(self.db_path, "deployment_snapshots", dict(
            project_id=self.proj_id, env="prod", task_id=2,
            version="1.1.0", image_versions='{"auth":"auth:1.1.0"}',
            nacos_config_hash="bbb", env_vars_hash="eee",
            deployed_at=now))
        resp = self.client.get(
            f"/api/deploy/compare?project_id={self.proj_id}&env=prod",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["old"]["version"], "1.0.0")
        self.assertEqual(data["new"]["version"], "1.1.0")

    def test_impact_analysis(self):
        # 创建服务器和环境
        env_id = dbm.create_record(self.db_path, "environments", dict(
            project_id=self.proj_id, name="prod", is_default=1,
            registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
            build="{}", deploy="{}", monitor_enabled=1))
        sid = dbm.create_record(self.db_path, "servers", dict(
            host="10.0.0.1", ssh_port=22, ssh_user="root", ssh_password="x",
            nacos_ip="", role="", remark="", enabled=1))
        dbm.create_record(self.db_path, "env_servers", dict(
            env_id=env_id, server_id=sid, order_no=0))
        # 添加依赖关系
        dbm.create_record(self.db_path, "service_dependencies", dict(
            project_id=self.proj_id, source_service="gateway",
            target_service="auth", dep_type="manual",
            created_at=int(time.time())))
        resp = self.client.get(
            f"/api/deploy/impact/{self.proj_id}?env=prod&service=auth",
            headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("colocated", data)
        self.assertIn("upstream", data)
        self.assertIn("downstream", data)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceAPI -v`
Expected: FAIL — 路由不存在

- [ ] **Step 3: 实现 API 路由**

创建 `webapp/routes/deploy_enhance_routes.py`：

```python
"""发布流程增强 API：定时计划 / 发布窗口 / 灰度策略 / 依赖拓扑 / 发布快照 / Webhook"""
import json
import time

from flask import Blueprint, request, jsonify, current_app, g

from .. import db as dbm
from ..auth import require_auth, require_role, audit
from ..deploy_scheduler import check_release_window

deploy_enhance_bp = Blueprint("deploy_enhance", __name__)


def _json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


# ========== 定时发布计划 ==========

@deploy_enhance_bp.route("/deploy/schedules")
@require_auth
def list_schedules():
    pid = request.args.get("project_id", type=int)
    where = "project_id=?" if pid else ""
    params = (pid,) if pid else ()
    return jsonify(dbm.list_records(current_app.config["DB_PATH"],
                                     "deploy_schedules", where, params, "id DESC"))


@deploy_enhance_bp.route("/deploy/schedules", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_schedule():
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    required = ("project_id", "type", "name")
    for k in required:
        if k not in body:
            return jsonify({"error": f"缺少必填字段: {k}"}), 400
    if body["type"] not in ("once", "cron"):
        return jsonify({"error": "type 必须为 once 或 cron"}), 400
    if body["type"] == "cron" and not body.get("cron_expr"):
        return jsonify({"error": "cron 模式必须指定 cron_expr"}), 400
    # 验证 cron 表达式
    if body.get("cron_expr"):
        try:
            from croniter import croniter
            croniter(body["cron_expr"])
        except (ImportError, ValueError, KeyError) as e:
            return jsonify({"error": f"CRON 表达式无效: {e}"}), 400
    # 计算 next_run_at
    now = int(time.time())
    next_run = 0
    if body["type"] == "once":
        next_run = body.get("schedule_at", 0)
    else:
        from webapp.deploy_scheduler import DeployScheduler
        sched = DeployScheduler(current_app.config["DB_PATH"])
        next_run = sched._calc_next_run("cron", body["cron_expr"], now)
    fields = dict(
        project_id=body["project_id"], type=body["type"], name=body["name"],
        ref_type=body.get("ref_type", "project"),
        ref_id=body.get("ref_id", 0), env=body.get("env", ""),
        version=body.get("version", ""),
        previous_version=body.get("previous_version", ""),
        extra=json.dumps(body.get("extra", []), ensure_ascii=False),
        schedule_at=body.get("schedule_at", 0),
        cron_expr=body.get("cron_expr", ""),
        enabled=body.get("enabled", 1),
        last_run_at=0, next_run_at=next_run,
        created_by=g.user["id"], created_at=now)
    sid = dbm.create_record(current_app.config["DB_PATH"], "deploy_schedules", fields)
    audit("create_schedule", "deploy_schedule", sid, {"name": body["name"]})
    return jsonify({"id": sid}), 201


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_schedule(sid):
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    rec = dbm.get_record(current_app.config["DB_PATH"], "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    allowed = {"name", "ref_type", "ref_id", "env", "version",
               "previous_version", "extra", "schedule_at", "cron_expr",
               "enabled", "next_run_at"}
    updates = {k: v for k, v in body.items() if k in allowed}
    dbm.update_record(current_app.config["DB_PATH"], "deploy_schedules", sid, updates)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_schedule(sid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    dbm.delete_record(current_app.config["DB_PATH"], "deploy_schedules", sid)
    audit("delete_schedule", "deploy_schedule", sid)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_schedule(sid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    new_val = 0 if rec["enabled"] else 1
    dbm.update_record(current_app.config["DB_PATH"], "deploy_schedules", sid,
                      {"enabled": new_val})
    return jsonify({"enabled": new_val})


@deploy_enhance_bp.route("/deploy/schedules/<int:sid>/run", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def run_schedule_now(sid):
    """立即执行一次定时计划"""
    rec = dbm.get_record(current_app.config["DB_PATH"], "deploy_schedules", sid)
    if not rec:
        return jsonify({"error": "计划不存在"}), 404
    # 窗口检查
    window = check_release_window(current_app.config["DB_PATH"], rec["project_id"])
    forced = 0
    if window:
        if window["block_level"] == "block" and not request.json.get("force"):
            return jsonify({"error": f"当前处于封网期: {window['name']}",
                            "window": window}), 403
        if window["block_level"] == "warn":
            forced = 1
    extra = json.loads(rec.get("extra") or "[]")
    if forced:
        extra.append("--force-window")
    task_id = dbm.create_task(
        current_app.config["DB_PATH"], rec["project_id"],
        rec["ref_type"], rec["ref_id"], rec["name"],
        rec["env"], rec["version"], rec["previous_version"],
        g.user["id"], extra=extra)
    from ..task_manager import submit_task
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    submit_task(current_app.config["DB_PATH"], task, extra)
    audit("run_schedule", "deploy_schedule", sid, {"task_id": task_id})
    return jsonify({"id": task_id, "status": "pending"}), 201


# ========== 发布窗口 ==========

@deploy_enhance_bp.route("/deploy/windows")
@require_auth
def list_windows():
    pid = request.args.get("project_id", type=int)
    where = ""
    params = ()
    if pid is not None:
        where = "project_id=? OR project_id=0"
        params = (pid,)
    return jsonify(dbm.list_records(current_app.config["DB_PATH"],
                                     "release_windows", where, params, "start_at DESC"))


@deploy_enhance_bp.route("/deploy/windows", methods=["POST"])
@require_auth
@require_role("admin")
def create_window():
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    for k in ("name", "start_at", "end_at"):
        if k not in body:
            return jsonify({"error": f"缺少必填字段: {k}"}), 400
    if body["block_level"] not in ("warn", "block"):
        return jsonify({"error": "block_level 必须为 warn 或 block"}), 400
    wid = dbm.create_record(current_app.config["DB_PATH"], "release_windows", dict(
        project_id=body.get("project_id", 0), name=body["name"],
        start_at=body["start_at"], end_at=body["end_at"],
        block_level=body["block_level"],
        repeat_yearly=1 if body.get("repeat_yearly") else 0,
        created_by=g.user["id"], created_at=int(time.time())))
    audit("create_window", "release_window", wid, {"name": body["name"]})
    return jsonify({"id": wid}), 201


@deploy_enhance_bp.route("/deploy/windows/<int:wid>", methods=["PUT"])
@require_auth
@require_role("admin")
def update_window(wid):
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    rec = dbm.get_record(current_app.config["DB_PATH"], "release_windows", wid)
    if not rec:
        return jsonify({"error": "窗口不存在"}), 404
    allowed = {"name", "start_at", "end_at", "block_level", "repeat_yearly"}
    updates = {k: v for k, v in body.items() if k in allowed}
    dbm.update_record(current_app.config["DB_PATH"], "release_windows", wid, updates)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/windows/<int:wid>", methods=["DELETE"])
@require_auth
@require_role("admin")
def delete_window(wid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "release_windows", wid)
    if not rec:
        return jsonify({"error": "窗口不存在"}), 404
    dbm.delete_record(current_app.config["DB_PATH"], "release_windows", wid)
    audit("delete_window", "release_window", wid)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/windows/check")
@require_auth
def check_window():
    pid = request.args.get("project_id", 0, type=int)
    result = check_release_window(current_app.config["DB_PATH"], pid)
    return jsonify(result)


# ========== 灰度策略 ==========

@deploy_enhance_bp.route("/deploy/canary-configs")
@require_auth
def list_canary_configs():
    pid = request.args.get("project_id", type=int)
    where = "project_id=?" if pid else ""
    params = (pid,) if pid else ()
    return jsonify(dbm.list_records(current_app.config["DB_PATH"],
                                     "canary_configs", where, params, "id"))


@deploy_enhance_bp.route("/deploy/canary-configs", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_canary_config():
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    for k in ("project_id", "name", "steps"):
        if k not in body:
            return jsonify({"error": f"缺少必填字段: {k}"}), 400
    steps = body["steps"]
    if not isinstance(steps, list) or not steps:
        return jsonify({"error": "steps 必须为非空数组"}), 400
    cid = dbm.create_record(current_app.config["DB_PATH"], "canary_configs", dict(
        project_id=body["project_id"], name=body["name"],
        steps=json.dumps(steps, ensure_ascii=False),
        is_default=1 if body.get("is_default") else 0,
        created_at=int(time.time())))
    return jsonify({"id": cid}), 201


@deploy_enhance_bp.route("/deploy/canary-configs/<int:cid>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_canary_config(cid):
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    rec = dbm.get_record(current_app.config["DB_PATH"], "canary_configs", cid)
    if not rec:
        return jsonify({"error": "策略不存在"}), 404
    updates = {}
    if "name" in body:
        updates["name"] = body["name"]
    if "steps" in body:
        updates["steps"] = json.dumps(body["steps"], ensure_ascii=False)
    if "is_default" in body:
        updates["is_default"] = 1 if body["is_default"] else 0
    if updates:
        dbm.update_record(current_app.config["DB_PATH"], "canary_configs", cid, updates)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/canary-configs/<int:cid>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_canary_config(cid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "canary_configs", cid)
    if not rec:
        return jsonify({"error": "策略不存在"}), 404
    dbm.delete_record(current_app.config["DB_PATH"], "canary_configs", cid)
    return jsonify({"ok": True})


# ========== 依赖拓扑 & 影响分析 ==========

@deploy_enhance_bp.route("/deploy/topology/<int:project_id>")
@require_auth
def get_topology(project_id):
    deps = dbm.list_records(current_app.config["DB_PATH"],
                            "service_dependencies",
                            "project_id=?", (project_id,))
    # 也从 services.dubbo_services 推断依赖
    services = dbm.list_services(current_app.config["DB_PATH"], project_id)
    edges = []
    seen = set()
    for d in deps:
        key = (d["source_service"], d["target_service"])
        if key not in seen:
            edges.append({"source": d["source_service"], "target": d["target_service"],
                          "type": d["dep_type"], "id": d["id"]})
            seen.add(key)
    for svc in services:
        dubbo = svc.get("dubbo_services") or []
        if isinstance(dubbo, str):
            try:
                dubbo = json.loads(dubbo)
            except (ValueError, TypeError):
                dubbo = []
        for target in dubbo:
            key = (svc["name"], target)
            if key not in seen:
                edges.append({"source": svc["name"], "target": target,
                              "type": "dubbo", "id": None})
                seen.add(key)
    nodes = list({e["source"] for e in edges} | {e["target"] for e in edges})
    return jsonify({"nodes": [{"name": n} for n in nodes], "edges": edges})


@deploy_enhance_bp.route("/deploy/topology", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def add_dependency():
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    for k in ("project_id", "source_service", "target_service"):
        if k not in body:
            return jsonify({"error": f"缺少必填字段: {k}"}), 400
    if body["source_service"] == body["target_service"]:
        return jsonify({"error": "不能依赖自身"}), 400
    # 循环依赖检测
    if _has_cycle(current_app.config["DB_PATH"], body["project_id"],
                  body["source_service"], body["target_service"]):
        return jsonify({"error": "添加此依赖会产生循环依赖"}), 400
    did = dbm.create_record(current_app.config["DB_PATH"], "service_dependencies", dict(
        project_id=body["project_id"],
        source_service=body["source_service"],
        target_service=body["target_service"],
        dep_type=body.get("dep_type", "manual"),
        created_at=int(time.time())))
    return jsonify({"id": did}), 201


@deploy_enhance_bp.route("/deploy/topology/<int:did>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_dependency(did):
    rec = dbm.get_record(current_app.config["DB_PATH"], "service_dependencies", did)
    if not rec:
        return jsonify({"error": "依赖关系不存在"}), 404
    dbm.delete_record(current_app.config["DB_PATH"], "service_dependencies", did)
    return jsonify({"ok": True})


@deploy_enhance_bp.route("/deploy/impact/<int:project_id>")
@require_auth
def get_impact(project_id):
    """发布影响分析：共机 + 上下游依赖"""
    env = request.args.get("env", "")
    service = request.args.get("service", "")
    result = {"colocated": [], "upstream": [], "downstream": []}
    # 共机分析
    if env and service:
        envs = dbm.list_records(current_app.config["DB_PATH"], "environments",
                                "project_id=? AND name=?", (project_id, env))
        if envs:
            env_id = envs[0]["id"]
            servers = dbm.list_env_servers(current_app.config["DB_PATH"], env_id)
            all_services = dbm.list_services(current_app.config["DB_PATH"], project_id)
            svc_names = {s["name"] for s in all_services}
            # 找出部署了目标服务的服务器上的其他服务
            for srv in servers:
                result["colocated"].append({
                    "host": srv["host"],
                    "services": list(svc_names - {service})
                })
    # 上下游依赖
    deps = dbm.list_records(current_app.config["DB_PATH"], "service_dependencies",
                            "project_id=?", (project_id,))
    if service:
        result["upstream"] = [d["source_service"] for d in deps
                              if d["target_service"] == service]
        result["downstream"] = [d["target_service"] for d in deps
                                if d["source_service"] == service]
    return jsonify(result)


def _has_cycle(db_path: str, project_id: int, source: str, target: str) -> bool:
    """检测添加 source→target 是否会形成环"""
    deps = dbm.list_records(db_path, "service_dependencies",
                            "project_id=?", (project_id,))
    # BFS: 从 target 出发，看能否到达 source
    adj = {}
    for d in deps:
        adj.setdefault(d["source_service"], []).append(d["target_service"])
    visited = set()
    queue = [target]
    while queue:
        node = queue.pop(0)
        if node == source:
            return True
        if node in visited:
            continue
        visited.add(node)
        queue.extend(adj.get(node, []))
    return False


# ========== 发布快照 & 对比 ==========

@deploy_enhance_bp.route("/deploy/snapshots/<int:project_id>")
@require_auth
def list_snapshots(project_id):
    env = request.args.get("env", "")
    where = "project_id=?"
    params = [project_id]
    if env:
        where += " AND env=?"
        params.append(env)
    return jsonify(dbm.list_records(current_app.config["DB_PATH"],
                                     "deployment_snapshots", where, tuple(params),
                                     "deployed_at DESC"))


@deploy_enhance_bp.route("/deploy/compare")
@require_auth
def compare_snapshots():
    pid = request.args.get("project_id", type=int)
    env = request.args.get("env", "")
    if not pid:
        return jsonify({"error": "project_id 必填"}), 400
    where = "project_id=?"
    params = [pid]
    if env:
        where += " AND env=?"
        params.append(env)
    snaps = dbm.list_records(current_app.config["DB_PATH"],
                             "deployment_snapshots", where, tuple(params),
                             "deployed_at DESC")
    if len(snaps) < 2:
        return jsonify({"error": "至少需要 2 条发布记录才能对比"}), 400
    old, new = snaps[1], snaps[0]
    # 解析镜像版本
    old_images = json.loads(old.get("image_versions") or "{}")
    new_images = json.loads(new.get("image_versions") or "{}")
    all_services = set(old_images.keys()) | set(new_images.keys())
    diff = []
    for svc in sorted(all_services):
        old_v = old_images.get(svc, "")
        new_v = new_images.get(svc, "")
        if old_v == new_v:
            change = "unchanged"
        elif new_v > old_v:
            change = "upgraded"
        else:
            change = "downgraded"
        diff.append({"service": svc, "old": old_v, "new": new_v, "change": change})
    return jsonify({
        "old": {"id": old["id"], "version": old["version"],
                "deployed_at": old["deployed_at"]},
        "new": {"id": new["id"], "version": new["version"],
                "deployed_at": new["deployed_at"]},
        "diff": diff,
        "nacos_changed": old.get("nacos_config_hash") != new.get("nacos_config_hash"),
        "env_changed": old.get("env_vars_hash") != new.get("env_vars_hash"),
    })


# ========== Webhook ==========

@deploy_enhance_bp.route("/deploy/webhook", methods=["POST"])
def webhook_deploy():
    """外部 CI/CD 系统触发发布"""
    import yaml
    token = request.headers.get("X-Webhook-Token", "")
    if not token:
        return jsonify({"error": "缺少 X-Webhook-Token 头"}), 401
    # 从 deploy.yaml 读取 webhook_token
    config_path = current_app.config.get("DEPLOY_CONFIG_PATH",
                                          "config/deploy.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except OSError:
        cfg = {}
    expected = (cfg.get("deploy") or {}).get("webhook_token", "")
    if not expected or token != expected:
        return jsonify({"error": "Webhook token 无效"}), 401
    body = _json_body()
    if not body:
        return jsonify({"error": "请求体必须为 JSON 对象"}), 400
    for k in ("project_id", "env", "version"):
        if k not in body:
            return jsonify({"error": f"缺少必填字段: {k}"}), 400
    # 窗口检查
    window = check_release_window(current_app.config["DB_PATH"], body["project_id"])
    if window and window["block_level"] == "block":
        return jsonify({"error": f"当前处于封网期: {window['name']}",
                        "window": window}), 403
    # 查找项目下的默认服务
    services = dbm.list_services(current_app.config["DB_PATH"], body["project_id"])
    if not services:
        return jsonify({"error": "项目下无服务"}), 404
    svc = services[0]
    task_id = dbm.create_task(
        current_app.config["DB_PATH"], body["project_id"],
        "project", svc["id"], svc["name"],
        body["env"], body["version"], body.get("previous_version", ""),
        0, extra=body.get("extra", []))
    from ..task_manager import submit_task
    task = dbm.get_task(current_app.config["DB_PATH"], task_id)
    submit_task(current_app.config["DB_PATH"], task, body.get("extra", []))
    return jsonify({"id": task_id, "status": "pending"}), 201
```

- [ ] **Step 4: 在 app.py 注册新蓝图**

在 `webapp/app.py` 的 `create_app()` 函数中，在现有 `register_blueprint` 之后追加：

```python
    from .routes.deploy_enhance_routes import deploy_enhance_bp
    app.register_blueprint(deploy_enhance_bp, url_prefix="/api")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestDeployEnhanceAPI -v`
Expected: 10 tests PASS

- [ ] **Step 6: 提交**

```bash
git add webapp/routes/deploy_enhance_routes.py webapp/app.py tests/test_deploy_enhance.py
git commit -m "feat(deploy-enhance): add all API routes for deploy enhancement"
```

---

## Task 5: 应用集成 — 启动 DeployScheduler

**Files:**
- Modify: `webapp/app.py`

- [ ] **Step 1: 在 app.py 中启动 DeployScheduler**

在 `webapp/app.py` 的 `main()` 函数中，在 `init_engine(app)` 之后追加：

```python
        from webapp.deploy_scheduler import DeployScheduler
        deploy_sched = DeployScheduler(app.config["DB_PATH"],
                                        args.monitor_interval)
        deploy_sched.start()
        print(f"[deploy-sched] 发布调度器已启动，周期 {args.monitor_interval}s")
```

- [ ] **Step 2: 验证应用启动不报错**

Run: `python -c "from webapp import create_app; app = create_app(); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 3: 提交**

```bash
git add webapp/app.py
git commit -m "feat(deploy-enhance): integrate DeployScheduler into app startup"
```

---

## Task 5.5: Deployer 灰度自动推进扩展

**Files:**
- Modify: `deploy.py` — Deployer.run() 方法扩展分批发布逻辑
- Test: `tests/test_deploy_enhance.py`

- [ ] **Step 1: 编写灰度自动推进测试**

在 `tests/test_deploy_enhance.py` 追加：

```python
class TestCanaryAutoAdvance(unittest.TestCase):
    """灰度分批逻辑测试（不实际 SSH，仅验证批次划分）"""

    def test_batch_split(self):
        """验证 canary steps 正确划分服务器批次"""
        from webapp.deploy_scheduler import compute_canary_batches
        steps = [
            {"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": True},
            {"batch": 2, "servers": 2, "observe_minutes": 10, "auto_advance": True},
            {"batch": 3, "servers": "all", "observe_minutes": 0, "auto_advance": False},
        ]
        total_servers = 5
        batches = compute_canary_batches(steps, total_servers)
        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0]["server_count"], 1)
        self.assertEqual(batches[1]["server_count"], 2)
        self.assertEqual(batches[2]["server_count"], 2)  # 5 - 1 - 2 = 2

    def test_batch_split_fewer_servers(self):
        """服务器数少于批次配置时，合并到最后批次"""
        from webapp.deploy_scheduler import compute_canary_batches
        steps = [
            {"batch": 1, "servers": 2, "observe_minutes": 5, "auto_advance": True},
            {"batch": 2, "servers": "all", "observe_minutes": 0, "auto_advance": False},
        ]
        batches = compute_canary_batches(steps, 1)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["server_count"], 1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_deploy_enhance.py::TestCanaryAutoAdvance -v`
Expected: FAIL — `compute_canary_batches` 不存在

- [ ] **Step 3: 实现批次计算函数**

在 `webapp/deploy_scheduler.py` 追加：

```python
def compute_canary_batches(steps: list, total_servers: int) -> list:
    """根据 canary_config.steps 和服务器总数，计算每批次的服务器数量。

    steps 中 servers 字段：
      - 整数：该批次的服务器数量
      - "all"：剩余所有服务器
    返回 [{"batch": int, "server_count": int, "observe_minutes": int,
           "auto_advance": bool, "server_indices": [int, ...]}]
    """
    batches = []
    assigned = 0
    for step in steps:
        srv_count = step["servers"]
        if srv_count == "all":
            count = total_servers - assigned
        else:
            count = min(int(srv_count), total_servers - assigned)
        if count <= 0:
            continue
        indices = list(range(assigned, assigned + count))
        batches.append({
            "batch": step["batch"],
            "server_count": count,
            "observe_minutes": step.get("observe_minutes", 0),
            "auto_advance": step.get("auto_advance", False),
            "server_indices": indices,
        })
        assigned += count
        if assigned >= total_servers:
            break
    return batches
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_deploy_enhance.py::TestCanaryAutoAdvance -v`
Expected: 2 tests PASS

- [ ] **Step 5: 扩展 Deployer.run() 支持分批发布**

在 `deploy.py` 的 `Deployer.run()` 方法中，在滚动发布循环之前，添加从 DB 读取 canary config 并分批的逻辑：

在 `log(">>> 步骤 3/5: 滚动发布"...)` 之后，替换原有的逐服务器循环为分批循环：

```python
        # 灰度分批：从 DB 读取 canary config（db 模式下）
        canary_batches = None
        if self.canary and args_config_source == "db":
            try:
                from webapp.deploy_scheduler import compute_canary_batches
                import sqlite3, json as _json
                db_path = Path(__file__).resolve().parent / "data" / "platform.db"
                conn = sqlite3.connect(str(db_path))
                conn.row_factory = sqlite3.Row
                try:
                    proj_id = conn.execute(
                        "SELECT id FROM projects WHERE code=?", (self.project,)
                    ).fetchone()
                    if proj_id:
                        cfg = conn.execute(
                            "SELECT steps FROM canary_configs "
                            "WHERE project_id=? AND is_default=1 LIMIT 1",
                            (proj_id["id"],)
                        ).fetchone()
                        if cfg:
                            steps = _json.loads(cfg["steps"])
                            canary_batches = compute_canary_batches(steps, len(servers))
                finally:
                    conn.close()
            except Exception as e:
                log(f"[canary] 读取灰度配置失败，回退默认金丝雀: {e}")

        if canary_batches:
            log(f">>> 灰度分批: {len(canary_batches)} 批")
            for bi, batch in enumerate(canary_batches):
                log(f"\n--- 批次 {batch['batch']}/{len(canary_batches)}: "
                    f"{batch['server_count']} 台服务器 ---")
                for si in batch["server_indices"]:
                    srv = servers[si]
                    host = srv["ip"]
                    ok = self.deploy_server(host, srv, is_canary=(bi == 0 and si == 0))
                    if not ok:
                        success_all = False
                        log(f"批次 {batch['batch']} 中 {host} 失败，停止后续批次")
                        break
                if not success_all:
                    break
                # 观察期（非最后一批）
                if batch["observe_minutes"] > 0 and bi < len(canary_batches) - 1:
                    observe_sec = batch["observe_minutes"] * 60
                    log(f"观察期 {batch['observe_minutes']} 分钟，"
                        f"自动推进: {'是' if batch['auto_advance'] else '否'}")
                    if batch["auto_advance"]:
                        # 自动推进：等待观察期（期间可被中断）
                        time.sleep(observe_sec)
                    else:
                        # 人工确认
                        decision = self._canary_prompt(servers[batch["server_indices"][0]]["ip"])
                        if decision != "continue":
                            success_all = False
                            break
        else:
            # 原有逐服务器循环（无 canary config 或非 db 模式）
            for idx, srv in enumerate(servers):
                host = srv["ip"]
                is_canary = (idx == 0)
                ok = self.deploy_server(host, srv, is_canary=is_canary)
                if not ok:
                    success_all = False
                    log("首台或中间服务器失败，停止继续滚动，请人工排查。")
                    break
                if self.canary and is_canary and idx < len(servers) - 1:
                    decision = self._canary_prompt(host)
                    if decision == "rollback":
                        # ... 保持现有回滚逻辑 ...
```

注意：实际实现时需要将 `run()` 方法签名添加 `args_config_source` 参数（从 deploy_pipeline.py 传入），或者通过 `self.config` 间接判断。具体实现时根据 deploy.py 的调用链确定。

- [ ] **Step 6: 提交**

```bash
git add deploy.py webapp/deploy_scheduler.py tests/test_deploy_enhance.py
git commit -m "feat(deploy-enhance): extend Deployer with canary batch auto-advance"
```

---

## Task 6: 前端 — 路由 + 导航 + CSS

**Files:**
- Modify: `webapp/static/js/router.js` — 添加子路由
- Modify: `webapp/static/js/app.js` — 注册页面模块
- Create: `webapp/static/css/pages/deploy-enhance.css`
- Modify: `webapp/templates/screen.html` — 引入 CSS

- [ ] **Step 1: 在 router.js 中添加子路由**

当前 deploy 路由是 `{ path:'deploy', ... }`，需要添加 sub 路由支持。在 ROUTES 数组的 deploy 条目之后追加 5 个子路由：

```javascript
// 在 ROUTES 数组中 deploy 条目后追加：
{ path:'deploy',    group:'运维操作', icon:'🚀', name:'发布中心', page:'deploy',    crumbs:['运维操作','发布中心'] },
{ path:'deploy/schedules', group:'运维操作', icon:'📅', name:'定时计划', page:'deploy_schedules', crumbs:['运维操作','发布中心','定时计划'], sub:true, parent:'deploy' },
{ path:'deploy/windows',   group:'运维操作', icon:'🔒', name:'发布窗口', page:'deploy_windows',   crumbs:['运维操作','发布中心','发布窗口'], sub:true, parent:'deploy' },
{ path:'deploy/canary',    group:'运维操作', icon:'🐤', name:'灰度策略', page:'deploy_canary',    crumbs:['运维操作','发布中心','灰度策略'], sub:true, parent:'deploy' },
{ path:'deploy/topology',  group:'运维操作', icon:'🕸️', name:'依赖拓扑', page:'deploy_topology',  crumbs:['运维操作','发布中心','依赖拓扑'], sub:true, parent:'deploy' },
{ path:'deploy/compare',   group:'运维操作', icon:'📊', name:'发布对比', page:'deploy_compare',   crumbs:['运维操作','发布中心','发布对比'], sub:true, parent:'deploy' },
```

同时修改 `getRoute()` 函数以支持多级路径匹配：

```javascript
function getRoute(){
  const hash = location.hash.replace(/^#\/?/, '').split('?')[0] || 'dashboard';
  // 先尝试完整匹配（如 deploy/schedules）
  const fullMatch = ROUTES.find(r => r.path === hash);
  if (fullMatch) return fullMatch;
  // 回退到基础路径匹配
  const base = hash.split('/')[0];
  return ROUTES.find(r => r.path === base) || ROUTES[0];
}
```

- [ ] **Step 2: 在 deploy.js 页面添加子导航 Tab**

在 `deploy.js` 的 `render` 函数中，在 toolbar 之前添加子页面导航：

```javascript
// 在 root.innerHTML 的最前面插入子导航
const subTabs = `
  <div class="sub-tabs" style="display:flex;gap:4px;margin-bottom:12px">
    <a class="chip ${location.hash === '#/deploy' ? 'active' : ''}" href="#/deploy">任务列表</a>
    <a class="chip ${location.hash.startsWith('#/deploy/schedules') ? 'active' : ''}" href="#/deploy/schedules">📅 定时计划</a>
    <a class="chip ${location.hash.startsWith('#/deploy/windows') ? 'active' : ''}" href="#/deploy/windows">🔒 发布窗口</a>
    <a class="chip ${location.hash.startsWith('#/deploy/canary') ? 'active' : ''}" href="#/deploy/canary">🐤 灰度策略</a>
    <a class="chip ${location.hash.startsWith('#/deploy/topology') ? 'active' : ''}" href="#/deploy/topology">🕸️ 依赖拓扑</a>
    <a class="chip ${location.hash.startsWith('#/deploy/compare') ? 'active' : ''}" href="#/deploy/compare">📊 发布对比</a>
  </div>`;
```

- [ ] **Step 3: 创建 deploy-enhance.css**

```css
/* deploy-enhance.css — 发布流程增强页面样式 */

/* 子导航 tabs */
.sub-tabs .chip { cursor:pointer; padding:4px 12px; border-radius:12px; font-size:12px;
  background:var(--card); color:var(--text-secondary); border:1px solid var(--border);
  text-decoration:none; transition: all .15s; }
.sub-tabs .chip:hover { border-color:var(--accent); color:var(--accent); }
.sub-tabs .chip.active { background:var(--accent); color:#fff; border-color:var(--accent); }

/* 日历视图（发布窗口） */
.cal-grid { display:grid; grid-template-columns:repeat(7,1fr); gap:2px; }
.cal-cell { aspect-ratio:1; padding:4px; border-radius:4px; font-size:11px;
  background:var(--card); border:1px solid var(--border); position:relative; }
.cal-cell.today { border-color:var(--accent); font-weight:600; }
.cal-cell .cal-day { position:absolute; top:2px; left:4px; font-size:10px; color:var(--text-secondary); }
.cal-cell.cal-block { background:rgba(239,68,68,.15); border-color:rgba(239,68,68,.3); }
.cal-cell.cal-warn { background:rgba(245,158,11,.15); border-color:rgba(245,158,11,.3); }

/* 拓扑图 */
.topo-svg { width:100%; height:400px; background:var(--card); border-radius:8px; border:1px solid var(--border); }
.topo-node { cursor:pointer; }
.topo-node circle { fill:var(--accent); stroke:var(--accent-light); stroke-width:2; }
.topo-node text { font-size:11px; fill:var(--text); text-anchor:middle; dominant-baseline:central; }
.topo-edge line { stroke:var(--border); stroke-width:1.5; }
.topo-node.highlight circle { fill:var(--warn); stroke:var(--warn); }

/* 灰度策略可视化 */
.canary-steps { display:flex; gap:8px; align-items:flex-end; padding:16px 0; }
.canary-step { display:flex; flex-direction:column; align-items:center; gap:4px;
  padding:8px 16px; border-radius:8px; background:var(--card); border:1px solid var(--border);
  min-width:80px; }
.canary-step .step-num { font-size:20px; font-weight:700; color:var(--accent); }
.canary-step .step-label { font-size:11px; color:var(--text-secondary); }
.canary-arrow { font-size:18px; color:var(--text-secondary); padding:0 4px; }

/* 对比表格 */
.compare-tbl td.changed { background:rgba(245,158,11,.08); }
.compare-tbl .tag-upgrade { color:#22c55e; }
.compare-tbl .tag-downgrade { color:#ef4444; }
.compare-tbl .tag-unchanged { color:var(--text-secondary); }
```

- [ ] **Step 4: 在 screen.html 引入 CSS**

在 `webapp/templates/screen.html` 的 `<head>` 中追加：

```html
<link rel="stylesheet" href="/static/css/pages/deploy-enhance.css">
```

在 `</body>` 之前追加新 JS 文件引用：

```html
<script src="/static/js/pages/deploy_schedules.js"></script>
<script src="/static/js/pages/deploy_windows.js"></script>
<script src="/static/js/pages/deploy_canary.js"></script>
<script src="/static/js/pages/deploy_topology.js"></script>
<script src="/static/js/pages/deploy_compare.js"></script>
```

- [ ] **Step 5: 提交**

```bash
git add webapp/static/js/router.js webapp/static/js/pages/deploy.js webapp/static/css/pages/deploy-enhance.css webapp/templates/screen.html
git commit -m "feat(deploy-enhance): add frontend routing, navigation and CSS"
```

---

## Task 7: 前端 — 定时计划页面

**Files:**
- Create: `webapp/static/js/pages/deploy_schedules.js`

- [ ] **Step 1: 实现定时计划页面模块**

创建 `webapp/static/js/pages/deploy_schedules.js`，遵循现有页面模块模式（IIFE + `window.xxx = { render, destroy }`）：

```javascript
// pages/deploy_schedules.js — 定时发布计划管理
(function(){
  let timer = null;
  let destroyed = false;
  let schedules = [];
  let projects = [];

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    schedules = await api('/api/deploy/schedules').catch(() => []);
  }

  function renderTable(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#schedBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    box.innerHTML = schedules.length ? `
      <div class="panel" style="padding:0"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>名称</th><th>类型</th><th>项目</th><th>环境</th><th>版本</th><th>下次执行</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${schedules.map(s => `<tr>
          <td class="num">${s.id}</td>
          <td>${esc(s.name)}</td>
          <td>${s.type === 'cron' ? '🔄 周期' : '⏰ 一次'}</td>
          <td>${esc(projMap[s.project_id] || '-')}</td>
          <td>${esc(s.env || '-')}</td>
          <td class="num">${esc(s.version || '-')}</td>
          <td class="num muted">${s.next_run_at ? fmtTime(s.next_run_at) : (s.type === 'cron' ? esc(s.cron_expr) : '-')}</td>
          <td>${s.enabled ? '<span class="tag tag-ok">启用</span>' : '<span class="tag">停用</span>'}</td>
          <td>
            <button class="btn ghost sm" data-toggle="${s.id}">${s.enabled ? '停用' : '启用'}</button>
            <button class="btn ghost sm" data-run="${s.id}">执行</button>
            <button class="btn danger ghost sm" data-del="${s.id}">删除</button>
          </td>
        </tr>`).join('')}</tbody></table></div></div>`
      : `<div class="panel">${emptyState('📅', '暂无定时计划')}</div>`;
    // 事件绑定
    box.querySelectorAll('[data-toggle]').forEach(btn => btn.onclick = async () => {
      await api(`/api/deploy/schedules/${btn.dataset.toggle}/toggle`, {method:'POST'});
      await loadData(); renderTable();
    });
    box.querySelectorAll('[data-run]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认立即执行此计划？')) return;
      try {
        const r = await api(`/api/deploy/schedules/${btn.dataset.run}/run`, {method:'POST'});
        toastSuccess(`任务 #${r.id} 已创建`);
      } catch(e) { toastError(e.message); }
    });
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此计划？')) return;
      await api(`/api/deploy/schedules/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderTable();
      toastSuccess('已删除');
    });
  }

  function openCreateForm(){
    openDrawer({ title: '新建定时计划', body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">名称</label><input id="schedName" class="inp" placeholder="如：每日凌晨发布"></div>
        <div><label class="dg-label">类型</label>
          <select id="schedType" class="inp"><option value="once">一次性</option><option value="cron">周期性</option></select></div>
        <div><label class="dg-label">项目</label>
          <select id="schedProj" class="inp">${projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></div>
        <div><label class="dg-label">环境</label><input id="schedEnv" class="inp" placeholder="prod"></div>
        <div><label class="dg-label">版本</label><input id="schedVer" class="inp" placeholder="1.0.0"></div>
        <div id="schedTimeWrap">
          <label class="dg-label">执行时间</label>
          <input id="schedAt" type="datetime-local" class="inp">
        </div>
        <div id="schedCronWrap" style="display:none">
          <label class="dg-label">CRON 表达式</label>
          <input id="schedCron" class="inp" placeholder="0 2 * * *（每天凌晨2点）">
        </div>
        <button class="btn primary" id="schedSave">保存</button>
      </div>`});
    document.getElementById('schedType').onchange = (e) => {
      document.getElementById('schedTimeWrap').style.display = e.target.value === 'once' ? '' : 'none';
      document.getElementById('schedCronWrap').style.display = e.target.value === 'cron' ? '' : 'none';
    };
    document.getElementById('schedSave').onclick = async () => {
      const type = document.getElementById('schedType').value;
      const body = {
        project_id: Number(document.getElementById('schedProj').value),
        type, name: document.getElementById('schedName').value,
        env: document.getElementById('schedEnv').value,
        version: document.getElementById('schedVer').value,
        ref_type: 'project', ref_id: Number(document.getElementById('schedProj').value),
      };
      if (type === 'once') {
        const dt = document.getElementById('schedAt').value;
        if (!dt) { toastError('请选择执行时间'); return; }
        body.schedule_at = Math.floor(new Date(dt).getTime() / 1000);
      } else {
        body.cron_expr = document.getElementById('schedCron').value;
        if (!body.cron_expr) { toastError('请输入 CRON 表达式'); return; }
      }
      try {
        await api('/api/deploy/schedules', {method:'POST', body:JSON.stringify(body)});
        toastSuccess('计划已创建');
        document.getElementById('dwClose').click();
        await loadData(); renderTable();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_schedules = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newSchedBtn">+ 新建计划</button>' : ''}
        </div>
        <div id="schedBox"></div>`;
      const nb = document.getElementById('newSchedBtn');
      if (nb) nb.onclick = openCreateForm;
      renderTable();
      timer = setInterval(async () => { await loadData(); if (!destroyed) renderTable(); }, 10000);
      return [timer];
    },
    destroy: function(){ destroyed = true; if (timer) clearInterval(timer); timer = null; }
  };
})();
```

- [ ] **Step 2: 提交**

```bash
git add webapp/static/js/pages/deploy_schedules.js
git commit -m "feat(deploy-enhance): add deploy schedules frontend page"
```

---

## Task 8: 前端 — 发布窗口页面

**Files:**
- Create: `webapp/static/js/pages/deploy_windows.js`

- [ ] **Step 1: 实现发布窗口页面模块**

创建 `webapp/static/js/pages/deploy_windows.js`：

```javascript
// pages/deploy_windows.js — 发布窗口/封网期管理
(function(){
  let timer = null;
  let destroyed = false;
  let windows = [];

  async function loadWindows(){
    windows = await api('/api/deploy/windows').catch(() => []);
  }

  function renderCalendar(){
    const now = new Date();
    const year = now.getFullYear(), month = now.getMonth();
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const today = now.getDate();
    let cells = '';
    // 周标题
    ['日','一','二','三','四','五','六'].forEach(d =>
      cells += `<div style="text-align:center;font-size:11px;color:var(--text-secondary);padding:4px">${d}</div>`);
    // 空白填充
    for (let i = 0; i < firstDay; i++)
      cells += '<div class="cal-cell" style="visibility:hidden"></div>';
    // 日期格
    for (let d = 1; d <= daysInMonth; d++) {
      const dayStart = new Date(year, month, d).getTime() / 1000;
      const dayEnd = dayStart + 86400;
      let cls = 'cal-cell' + (d === today ? ' today' : '');
      let title = '';
      for (const w of windows) {
        let ws = w.start_at, we = w.end_at;
        if (w.repeat_yearly) {
          const wy = new Date(ws * 1000).getFullYear();
          const offset = (year - wy) * 365.25 * 86400;
          ws += offset; we += offset;
        }
        if (ws < dayEnd && we > dayStart) {
          cls += w.block_level === 'block' ? ' cal-block' : ' cal-warn';
          title = w.name;
          break;
        }
      }
      cells += `<div class="${cls}" title="${esc(title)}"><span class="cal-day">${d}</span></div>`;
    }
    return cells;
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#winBox');
    if (!box) return;
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="font-weight:600;margin-bottom:8px">${new Date().toLocaleDateString('zh-CN', {year:'numeric',month:'long'})}</div>
        <div class="cal-grid">${renderCalendar()}</div>
      </div>
      <div class="panel" style="padding:0;margin-top:12px"><div class="tbl-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>名称</th><th>开始</th><th>结束</th><th>级别</th><th>每年重复</th><th>操作</th></tr></thead>
        <tbody>${windows.map(w => `<tr>
          <td class="num">${w.id}</td>
          <td>${esc(w.name)}</td>
          <td class="num muted">${fmtTime(w.start_at)}</td>
          <td class="num muted">${fmtTime(w.end_at)}</td>
          <td>${w.block_level === 'block' ? '<span class="tag tag-warn">硬拦截</span>' : '<span class="tag">软警告</span>'}</td>
          <td>${w.repeat_yearly ? '✓' : '-'}</td>
          <td>
            <button class="btn danger ghost sm" data-del="${w.id}">删除</button>
          </td>
        </tr>`).join('')}</tbody></table></div></div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此窗口？')) return;
      await api(`/api/deploy/windows/${btn.dataset.del}`, {method:'DELETE'});
      await loadWindows(); renderList();
      toastSuccess('已删除');
    });
  }

  function openCreateForm(){
    openDrawer({ title: '新建发布窗口', body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">名称</label><input id="winName" class="inp" placeholder="如：春节封网"></div>
        <div><label class="dg-label">开始时间</label><input id="winStart" type="datetime-local" class="inp"></div>
        <div><label class="dg-label">结束时间</label><input id="winEnd" type="datetime-local" class="inp"></div>
        <div><label class="dg-label">拦截级别</label>
          <select id="winLevel" class="inp"><option value="block">硬拦截（禁止发布）</option><option value="warn">软警告（确认后放行）</option></select></div>
        <div><label><input type="checkbox" id="winRepeat"> 每年重复（节假日适用）</label></div>
        <button class="btn primary" id="winSave">保存</button>
      </div>`});
    document.getElementById('winSave').onclick = async () => {
      const startDt = document.getElementById('winStart').value;
      const endDt = document.getElementById('winEnd').value;
      if (!startDt || !endDt) { toastError('请选择时间范围'); return; }
      try {
        await api('/api/deploy/windows', {method:'POST', body:JSON.stringify({
          name: document.getElementById('winName').value,
          start_at: Math.floor(new Date(startDt).getTime() / 1000),
          end_at: Math.floor(new Date(endDt).getTime() / 1000),
          block_level: document.getElementById('winLevel').value,
          repeat_yearly: document.getElementById('winRepeat').checked,
          project_id: 0
        })});
        toastSuccess('窗口已创建');
        document.getElementById('dwClose').click();
        await loadWindows(); renderList();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_windows = {
    render: async function(root){
      destroyed = false;
      await loadWindows();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${isAdmin() ? '<button class="btn primary" id="newWinBtn">+ 新建窗口</button>' : ''}
        </div>
        <div id="winBox"></div>`;
      const nb = document.getElementById('newWinBtn');
      if (nb) nb.onclick = openCreateForm;
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
```

- [ ] **Step 2: 提交**

```bash
git add webapp/static/js/pages/deploy_windows.js
git commit -m "feat(deploy-enhance): add release windows frontend page"
```

---

## Task 9: 前端 — 灰度策略 + 依赖拓扑 + 发布对比

**Files:**
- Create: `webapp/static/js/pages/deploy_canary.js`
- Create: `webapp/static/js/pages/deploy_topology.js`
- Create: `webapp/static/js/pages/deploy_compare.js`

- [ ] **Step 1: 实现灰度策略页面**

创建 `webapp/static/js/pages/deploy_canary.js`：

```javascript
// pages/deploy_canary.js — 灰度策略配置
(function(){
  let destroyed = false;
  let configs = [];
  let projects = [];

  async function loadData(){
    projects = await api('/api/projects').catch(() => []);
    configs = await api('/api/deploy/canary-configs').catch(() => []);
  }

  function renderSteps(steps){
    if (!Array.isArray(steps)) return '-';
    return steps.map((s, i) => `
      ${i > 0 ? '<span class="canary-arrow">→</span>' : ''}
      <div class="canary-step">
        <div class="step-num">${s.servers === 'all' ? '全量' : s.servers + '台'}</div>
        <div class="step-label">观察 ${s.observe_minutes}min</div>
        <div class="step-label">${s.auto_advance ? '自动推进' : '人工确认'}</div>
      </div>`).join('');
  }

  function renderList(){
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#canaryBox');
    if (!box) return;
    const projMap = {};
    projects.forEach(p => projMap[p.id] = p.name);
    box.innerHTML = configs.length ? configs.map(c => `
      <div class="panel" style="padding:16px;margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <div><strong>${esc(c.name)}</strong> <span class="muted">· ${esc(projMap[c.project_id] || '全局')}</span>
          ${c.is_default ? ' <span class="tag tag-ok">默认</span>' : ''}</div>
          <button class="btn danger ghost sm" data-del="${c.id}">删除</button>
        </div>
        <div class="canary-steps">${renderSteps(c.steps)}</div>
      </div>`).join('')
      : `<div class="panel">${emptyState('🐤', '暂无灰度策略')}</div>`;
    box.querySelectorAll('[data-del]').forEach(btn => btn.onclick = async () => {
      if (!await confirmDialog('确认删除此策略？')) return;
      await api(`/api/deploy/canary-configs/${btn.dataset.del}`, {method:'DELETE'});
      await loadData(); renderList();
    });
  }

  function openCreateForm(){
    openDrawer({ title: '新建灰度策略', wide: true, body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">策略名称</label><input id="canaryName" class="inp" placeholder="如：标准三批"></div>
        <div><label class="dg-label">所属项目</label>
          <select id="canaryProj" class="inp">${projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></div>
        <div><label class="dg-label">批次配置 (JSON)</label>
          <textarea id="canarySteps" class="inp" rows="6" style="font-family:monospace">[
  {"batch":1, "servers":1, "observe_minutes":5, "auto_advance":true},
  {"batch":2, "servers":2, "observe_minutes":10, "auto_advance":true},
  {"batch":3, "servers":"all", "observe_minutes":0, "auto_advance":false}
]</textarea></div>
        <label><input type="checkbox" id="canaryDefault"> 设为默认策略</label>
        <button class="btn primary" id="canarySave">保存</button>
      </div>`});
    document.getElementById('canarySave').onclick = async () => {
      let steps;
      try { steps = JSON.parse(document.getElementById('canarySteps').value); }
      catch(e) { toastError('JSON 格式错误'); return; }
      try {
        await api('/api/deploy/canary-configs', {method:'POST', body:JSON.stringify({
          project_id: Number(document.getElementById('canaryProj').value),
          name: document.getElementById('canaryName').value,
          steps, is_default: document.getElementById('canaryDefault').checked
        })});
        toastSuccess('策略已创建');
        document.getElementById('dwClose').click();
        await loadData(); renderList();
      } catch(e) { toastError(e.message); }
    };
  }

  window.deploy_canary = {
    render: async function(root){
      destroyed = false;
      await loadData();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="newCanaryBtn">+ 新建策略</button>' : ''}
        </div>
        <div id="canaryBox"></div>`;
      const nb = document.getElementById('newCanaryBtn');
      if (nb) nb.onclick = openCreateForm;
      renderList();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
```

- [ ] **Step 2: 实现依赖拓扑页面**

创建 `webapp/static/js/pages/deploy_topology.js`：

```javascript
// pages/deploy_topology.js — 服务依赖拓扑 + 影响分析
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let topologyData = { nodes: [], edges: [] };

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadTopology(){
    if (!selectedProject) return;
    topologyData = await api(`/api/deploy/topology/${selectedProject}`).catch(() => ({ nodes:[], edges:[] }));
  }

  function renderTopology(){
    const svg = document.getElementById('topoSvg');
    if (!svg) return;
    const W = svg.clientWidth || 600, H = svg.clientHeight || 400;
    const nodes = topologyData.nodes || [];
    const edges = topologyData.edges || [];
    if (!nodes.length) {
      svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="var(--text-secondary)">暂无依赖关系数据</text>';
      return;
    }
    // 简易力导向布局（固定迭代）
    const pos = {};
    nodes.forEach((n, i) => {
      const angle = (2 * Math.PI * i) / nodes.length;
      pos[n.name] = { x: W/2 + Math.cos(angle) * 150, y: H/2 + Math.sin(angle) * 150 };
    });
    // 简单弹簧模拟（10 次迭代）
    for (let iter = 0; iter < 10; iter++) {
      for (const e of edges) {
        const a = pos[e.source], b = pos[e.target];
        if (!a || !b) continue;
        const dx = b.x - a.x, dy = b.y - a.y;
        const dist = Math.sqrt(dx*dx + dy*dy) || 1;
        const force = (dist - 120) * 0.05;
        a.x += dx/dist * force; a.y += dy/dist * force;
        b.x -= dx/dist * force; b.y -= dy/dist * force;
      }
      // 边界约束
      for (const n of nodes) {
        const p = pos[n.name];
        p.x = Math.max(40, Math.min(W-40, p.x));
        p.y = Math.max(40, Math.min(H-40, p.y));
      }
    }
    // 渲染
    let html = '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--border)"/></marker></defs>';
    // 边
    for (const e of edges) {
      const a = pos[e.source], b = pos[e.target];
      if (!a || !b) continue;
      html += `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="var(--border)" stroke-width="1.5" marker-end="url(#arrow)"/>`;
    }
    // 节点
    for (const n of nodes) {
      const p = pos[n.name];
      html += `<g class="topo-node" data-name="${esc(n.name)}"><circle cx="${p.x}" cy="${p.y}" r="20"/><text x="${p.x}" y="${p.y+30}" font-size="11">${esc(n.name)}</text></g>`;
    }
    svg.innerHTML = html;
  }

  window.deploy_topology = {
    render: async function(root){
      destroyed = false;
      await loadProjects();
      await loadTopology();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <select class="filter-sel" id="topoProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          ${canDeploy() ? '<button class="btn primary" id="addDepBtn">+ 添加依赖</button>' : ''}
        </div>
        <svg id="topoSvg" class="topo-svg"></svg>
        <div id="impactPanel" style="margin-top:12px"></div>`;
      document.getElementById('topoProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        await loadTopology();
        renderTopology();
      };
      const addBtn = document.getElementById('addDepBtn');
      if (addBtn) addBtn.onclick = openAddDepForm;
      renderTopology();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };

  function openAddDepForm(){
    openDrawer({ title: '添加服务依赖', body: `
      <div style="display:flex;flex-direction:column;gap:12px">
        <div><label class="dg-label">调用方</label><input id="depSrc" class="inp" placeholder="gateway"></div>
        <div><label class="dg-label">被调用方</label><input id="depTgt" class="inp" placeholder="auth"></div>
        <div><label class="dg-label">类型</label>
          <select id="depType" class="inp"><option value="dubbo">Dubbo</option><option value="http">HTTP</option><option value="manual">手动</option></select></div>
        <button class="btn primary" id="depSave">保存</button>
      </div>`});
    document.getElementById('depSave').onclick = async () => {
      try {
        await api('/api/deploy/topology', {method:'POST', body:JSON.stringify({
          project_id: selectedProject,
          source_service: document.getElementById('depSrc').value,
          target_service: document.getElementById('depTgt').value,
          dep_type: document.getElementById('depType').value
        })});
        toastSuccess('依赖已添加');
        document.getElementById('dwClose').click();
        await loadTopology(); renderTopology();
      } catch(e) { toastError(e.message); }
    };
  }
})();
```

- [ ] **Step 3: 实现发布对比页面**

创建 `webapp/static/js/pages/deploy_compare.js`：

```javascript
// pages/deploy_compare.js — 发布版本对比
(function(){
  let destroyed = false;
  let projects = [];
  let selectedProject = null;
  let snapshots = [];

  async function loadProjects(){
    projects = await api('/api/projects').catch(() => []);
    if (projects.length && !selectedProject) selectedProject = projects[0].id;
  }

  async function loadSnapshots(){
    if (!selectedProject) return;
    snapshots = await api(`/api/deploy/snapshots/${selectedProject}`).catch(() => []);
  }

  async function doCompare(){
    if (snapshots.length < 2) return;
    const data = await api(`/api/deploy/compare?project_id=${selectedProject}`).catch(() => null);
    if (!data) return;
    const root = document.getElementById('pageRoot');
    const box = root.querySelector('#compareBox');
    if (!box) return;
    const changeTag = (c) => {
      if (c === 'upgraded') return '<span class="tag-upgrade">↑ 升级</span>';
      if (c === 'downgraded') return '<span class="tag-downgrade">↓ 回滚</span>';
      return '<span class="tag-unchanged">= 未变</span>';
    };
    box.innerHTML = `
      <div class="panel" style="padding:16px">
        <div style="display:flex;gap:24px;margin-bottom:16px">
          <div><span class="dg-label">旧版本</span> <strong>${esc(data.old.version)}</strong>
            <span class="muted"> · ${fmtTime(data.old.deployed_at)}</span></div>
          <div><span class="dg-label">新版本</span> <strong>${esc(data.new.version)}</strong>
            <span class="muted"> · ${fmtTime(data.new.deployed_at)}</span></div>
        </div>
        <div class="tbl-wrap"><table class="tbl compare-tbl">
          <thead><tr><th>服务</th><th>旧版本</th><th>新版本</th><th>变化</th></tr></thead>
          <tbody>${data.diff.map(d => `<tr class="${d.change !== 'unchanged' ? 'changed' : ''}">
            <td>${esc(d.service)}</td>
            <td class="num">${esc(d.old || '-')}</td>
            <td class="num">${esc(d.new || '-')}</td>
            <td>${changeTag(d.change)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
        <div style="margin-top:12px;display:flex;gap:16px">
          <span>${data.nacos_changed ? '⚠️ Nacos 配置已变更' : '✓ Nacos 配置未变'}</span>
          <span>${data.env_changed ? '⚠️ 环境变量已变更' : '✓ 环境变量未变'}</span>
        </div>
      </div>`;
  }

  window.deploy_compare = {
    render: async function(root){
      destroyed = false;
      await loadProjects();
      await loadSnapshots();
      if (destroyed) return [];
      root.innerHTML = `
        <div class="deploy-toolbar">
          <select class="filter-sel" id="cmpProjSel">${projects.map(p =>
            `<option value="${p.id}" ${p.id === selectedProject ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>
          <span style="flex:1"></span>
          <button class="btn primary" id="cmpBtn" ${snapshots.length < 2 ? 'disabled' : ''}>对比最近两次发布</button>
        </div>
        <div id="compareBox">
          ${snapshots.length < 2 ? `<div class="panel">${emptyState('📊', '至少需要 2 条发布记录')}</div>` : ''}
        </div>`;
      document.getElementById('cmpProjSel').onchange = async (e) => {
        selectedProject = Number(e.target.value);
        await loadSnapshots();
        const btn = document.getElementById('cmpBtn');
        if (btn) btn.disabled = snapshots.length < 2;
        if (snapshots.length >= 2) await doCompare();
      };
      const btn = document.getElementById('cmpBtn');
      if (btn) btn.onclick = doCompare;
      if (snapshots.length >= 2) await doCompare();
      return [];
    },
    destroy: function(){ destroyed = true; }
  };
})();
```

- [ ] **Step 4: 提交**

```bash
git add webapp/static/js/pages/deploy_canary.js webapp/static/js/pages/deploy_topology.js webapp/static/js/pages/deploy_compare.js
git commit -m "feat(deploy-enhance): add canary, topology, and compare frontend pages"
```

---

## Task 10: 安装依赖 + 全量测试 + 最终提交

- [ ] **Step 1: 安装 croniter**

```bash
pip install croniter
```

- [ ] **Step 2: 运行全量测试**

```bash
pytest tests/test_deploy_enhance.py -v
```

Expected: 所有测试通过

- [ ] **Step 3: 启动应用验证**

```bash
python webapp/app.py --port 5678
```

验证：
- 应用启动日志显示 `[deploy-sched] 发布调度器已启动`
- 访问 `http://localhost:5678`，登录后导航到发布中心
- 子页面 Tab（定时计划/发布窗口/灰度策略/依赖拓扑/发布对比）可正常切换

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "feat(deploy-enhance): complete deploy enhancement implementation"
```
