# 多运行时发布（首期：Java JAR + systemd）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让平台在现有 Docker / 静态站点之外，支持把 Maven 构建出的可执行 JAR 以 Linux systemd 单元方式发布，并具备不可变版本留档、原子切换、就绪判定、失败回滚与单元接入审计。

**Architecture:** 复用既有「通用编排 + 发布策略」结构：新增 `deploy_strategies/jar_service.py` 实现五阶段契约（预检/传输/切换/验证/回滚），编排层 `Deployer` 只做按能力标志调度。制品按 `{app_home}/releases/{version}/app.jar` 不可变存放，`{app_home}/CURRENT` 符号链接原子切换，systemd unit 的 `ExecStart` 固定引用 `CURRENT/app.jar`，因此回滚只换链接 + 重启。

**Tech Stack:** Python 3 标准库、SQLite、paramiko（既有 `RemoteServer` 封装）、Flask（路由）、vanilla JS（前端表单）、pytest/unittest。

**Spec:** `docs/superpowers/specs/2026-10-24-multi-runtime-deploy-design.md`

## Global Constraints

- 只动源文件与测试文件。**禁止**修改 `dist/`、`dist-portable/`（构建产物）与 `data/`（用户生产数据）。
- 测试全程离线：SSH/SFTP/docker/systemctl 一律用桩替换，只用临时库与临时目录；遵循 `tests/conftest.py` 的会话级目录守卫与 `create_app(data_dir=...)` 显式注入。
- 不新增第三方依赖，`requirements.txt` 不变。
- 不改变 `deploy_mode=container` / `static_archive` 的既有行为；全量基线当前为 `644 passed`，每个任务结束时该数字不得下降。
- 注释使用中文，解释「为什么」而非复述「做了什么」；与现有文件风格保持一致。
- 每个任务结束都单独 commit，不 `git push`。
- 单实例 Jar 发布必然有停机窗口，任何日志/文案不得表述为「零停机」。
- 单测运行方式：`python -m pytest tests/<file>.py -q`；全量：`python -m pytest -q`。

## 文件结构

**新建**

- `deploy_strategies/jar_config.py` — 运行时配置解析、校验、产物命名、sha256（纯标准库，CLI 与 Web 共用）
- `deploy_strategies/jar_service.py` — `jar_service` 策略五阶段实现
- `service_unit.py` — systemd 单元内容生成与接入（仓库根，与 `deploy.py` 同级）
- `tests/test_jar_db.py` — `runtime_config` 列与 `service_host_state` 读写
- `tests/test_jar_config.py` — 配置解析/校验单测
- `tests/test_jar_service.py` — 策略五阶段与端到端测试
- `tests/test_build_jar_artifact.py` — 构建产物选择与校验和
- `tests/test_service_unit.py` — 单元内容生成与接入
- `tests/test_config_roundtrip_jar.py` — YAML 导入导出往返
- `tests/test_config_routes_jar.py` — 服务配置路由的 jar 校验与单元接口契约
- `tests/js/config_service_form_visibility_test.js` — 服务表单三种发布方式的字段显隐

**修改**

- `webapp/db.py` — `services.runtime_config` 列、`service_host_state` 表与读写函数
- `deploy_strategies/base.py` — 公开 `registered_modes()`、新增 `rollback_source_missing()` 默认钩子
- `deploy_strategies/__init__.py` — 注册新策略
- `deploy.py` — `target_modified` 语义、回滚源预检接入、主机版本状态回写
- `scripts/build.py` — `jar_service` 产物分支与 `build_type=none`
- `deploy_pipeline.py` — `--auto-version` 按策略分发
- `webapp/routes/config_routes.py` — 校验改为注册表驱动、支持 `runtime_config`、单元预览/接入接口
- `webapp/importer.py` / `webapp/exporter.py` — `runtime_config` 往返
- `webapp/task_manager.py` — 重启任务支持 systemd 单元
- `webapp/static/js/pages/config/config-service-form.js` — 新增「纯 Java 进程（systemd）」表单
- `webapp/static/js/pages/devices.js` — 运行时方式徽标；jar 的实时日志入口在 M5 前隐藏
  （`deploy_schedules.js` 无需改动：jar 与容器同为 semver，现有版本号提示已正确）
- `README.md`、`docs/usage.md` — 新发布方式说明

**不在本计划内（后续 M5 独立计划）**：`webapp/monitor.py` 进程指标采集、`webapp/log_collector.py` 的 journalctl 日志采集、`service_host_state` 的前端版本视图。原因：它们不阻塞发布链路上线，且各自可独立验收；先把发布能力做通，再补观测面。

---

### Task 1: 数据层（runtime_config 列 + service_host_state 表）

**Files:**
- Modify: `webapp/db.py`（`SCHEMA` 中 `services` 定义、新增表、`JSON_FIELDS`、`TABLES`、`_migrate` 末尾、文件末尾新增读写函数）
- Test: `tests/test_jar_db.py`（新建）

**Interfaces:**
- Produces:
  - `services.runtime_config`：读写为 `dict`（`JSON_FIELDS` 自动序列化）
  - `upsert_host_state(db_path: str, project_id: int, env: str, service: str, host: str, current_version: str, previous_version: str, last_task_id: int = 0) -> None`
  - `list_host_state(db_path: str, project_id: int, env: str = None, service: str = None) -> list[dict]`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jar_db.py
"""jar_service 数据层：runtime_config JSON 列与 service_host_state 幂等写入"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm


class JarDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_db_")
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runtime_config_roundtrip_as_dict(self):
        sid = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="auth", image_name="auth",
            service_name="auth", runtime_config={"app_home": "/opt/auth"}))
        rec = dbm.get_record(self.db, "services", sid)
        self.assertEqual(rec["runtime_config"], {"app_home": "/opt/auth"})

    def test_runtime_config_defaults_to_empty_object(self):
        sid = dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="web", image_name="web",
            service_name="web"))
        self.assertEqual(dbm.get_record(self.db, "services", sid)["runtime_config"], {})

    def test_host_state_upsert_is_single_row(self):
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.1", "1.0.0", 7)
        dbm.upsert_host_state(self.db, self.pid, "prod", "auth", "10.0.0.1",
                              "1.0.2", "1.0.1", 8)
        rows = dbm.list_host_state(self.db, self.pid, "prod", "auth")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_version"], "1.0.2")
        self.assertEqual(rows[0]["last_task_id"], 8)

    def test_migrate_adds_column_to_legacy_db(self):
        """存量库（无该列）init_db 后必须自动补列，且旧行读到 {}"""
        import sqlite3
        legacy = os.path.join(self.tmp, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("CREATE TABLE services (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "project_id INTEGER, name TEXT, image_name TEXT, service_name TEXT)")
        conn.execute("INSERT INTO services (project_id, name, image_name, service_name) "
                     "VALUES (1, 'old', 'old', 'old')")
        conn.commit()
        conn.close()
        dbm.init_db(legacy)
        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT runtime_config FROM services WHERE name='old'").fetchone()
        conn.close()
        self.assertEqual(row["runtime_config"], "{}")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_db.py -q`
Expected: FAIL（`runtime_config` 未序列化为 dict / `upsert_host_state` 不存在）

- [ ] **Step 3: 实现**

在 `webapp/db.py` 的 `SCHEMA` 中，`services` 表 `enabled` 之前加一列（新库）：

```sql
    -- 运行时专属配置（jar_service 等新策略使用；静态发布的标量列保持原样不做迁移）
    runtime_config TEXT DEFAULT '{}',
```

在 `SCHEMA` 的 `deployment_snapshots` 之后新增表：

```sql
-- 每主机当前版本缓存：权威状态在目标机的 CURRENT 链接与 .deploy_version，
-- 本表只用于页面展示与回滚目标选择，不作为发布决策的唯一依据
CREATE TABLE IF NOT EXISTS service_host_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    env TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL,
    host TEXT NOT NULL,
    current_version TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    last_task_id INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL,
    UNIQUE(project_id, env, service, host)
);
```

`JSON_FIELDS["services"]` 改为：

```python
    "services": {"build_order", "nacos_groups", "dubbo_services",
                 "log_error_keywords", "runtime_config"},
```

`TABLES` 中加入 `"service_host_state"`。

在 `_migrate()` 末尾（`agent_runs` 补列之后）追加：

```python
    # services 补 runtime_config 列：新发布方式（jar_service）的策略专属配置收敛为一个
    # JSON 列，避免继续为每种方式加标量列；存量行补默认空对象，策略侧按缺省值兜底
    srv_cols_now = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
    if "runtime_config" not in srv_cols_now:
        conn.execute("ALTER TABLE services ADD COLUMN runtime_config TEXT DEFAULT '{}'")
```

在 `webapp/db.py` 末尾新增：

```python
def upsert_host_state(db_path: str, project_id: int, env: str, service: str,
                      host: str, current_version: str,
                      previous_version: str, last_task_id: int = 0) -> None:
    """回写单台主机的当前/上一版本（发布或回滚成功后调用，失败不抛给调用方）"""
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO service_host_state "
            "(project_id, env, service, host, current_version, previous_version, "
            " last_task_id, updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id, env, service, host) DO UPDATE SET "
            "current_version=excluded.current_version, "
            "previous_version=excluded.previous_version, "
            "last_task_id=excluded.last_task_id, updated_at=excluded.updated_at",
            (project_id, env or "", service, host, current_version or "",
             previous_version or "", int(last_task_id or 0), int(time.time())))
        conn.commit()
    finally:
        conn.close()


def list_host_state(db_path: str, project_id: int, env: str = None,
                    service: str = None) -> list:
    """按项目/环境/服务查询主机版本缓存（参数为空表示不过滤）"""
    sql = "SELECT * FROM service_host_state WHERE project_id=?"
    params = [project_id]
    if env is not None:
        sql += " AND env=?"
        params.append(env)
    if service is not None:
        sql += " AND service=?"
        params.append(service)
    sql += " ORDER BY host"
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        conn.close()
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_db.py -q`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add webapp/db.py tests/test_jar_db.py
git commit -m "feat(db): services 增加 runtime_config JSON 列与 service_host_state 表"
```

---

### Task 2: 运行时配置解析/校验 + 路由校验改注册表驱动

**Files:**
- Create: `deploy_strategies/jar_config.py`
- Modify: `deploy_strategies/base.py`（文件末尾新增 `registered_modes()`）
- Modify: `webapp/routes/config_routes.py:231-246`（`create_service`）、`:292-315`（`update_service`）
- Test: `tests/test_jar_config.py`

**Interfaces:**
- Produces:
  - `parse_runtime_config(raw) -> dict`，键固定为：`app_home, java_bin, jvm_opts, app_args, run_user, work_dir, unit_name, port, readiness{type,path,timeout,interval}, stop_timeout, start_timeout, artifact_pattern, env_files`
  - `validate_runtime_config(raw, deploy_mode: str) -> list[str]`，空列表表示合法
  - `artifact_filename(service: str, version: str) -> str` → `"{service}-{version}.jar"`
  - `sha256_file(path) -> str`
  - `registered_modes() -> list[str]`（`base.py`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jar_config.py
"""jar_service 配置契约：解析兜底、校验规则、产物命名与校验和"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy_strategies.base import registered_modes
from deploy_strategies.jar_config import (
    artifact_filename, parse_runtime_config, sha256_file, validate_runtime_config)


class ParseTest(unittest.TestCase):
    def test_defaults(self):
        cfg = parse_runtime_config({})
        self.assertEqual(cfg["java_bin"], "/usr/bin/java")
        self.assertEqual(cfg["readiness"]["type"], "http")
        self.assertEqual(cfg["readiness"]["path"], "/actuator/health")
        self.assertEqual(cfg["readiness"]["timeout"], 90)
        self.assertEqual(cfg["readiness"]["interval"], 5)
        self.assertEqual(cfg["stop_timeout"], 60)
        self.assertEqual(cfg["start_timeout"], 120)
        self.assertEqual(cfg["artifact_pattern"], "target/*.jar")
        self.assertEqual(cfg["env_files"], [])

    def test_accepts_json_string_and_dict(self):
        raw = {"app_home": "/opt/a/", "port": 8080}
        self.assertEqual(parse_runtime_config(raw)["app_home"], "/opt/a")
        self.assertEqual(parse_runtime_config(json.dumps(raw))["port"], 8080)

    def test_broken_json_falls_back_to_empty(self):
        self.assertEqual(parse_runtime_config("{not json")["app_home"], "")


class ValidateTest(unittest.TestCase):
    def test_non_jar_mode_is_not_validated(self):
        self.assertEqual(validate_runtime_config({}, "container"), [])

    def test_valid_config(self):
        cfg = {"app_home": "/opt/a", "port": 8080,
               "readiness": {"type": "http", "timeout": 30, "interval": 2}}
        self.assertEqual(validate_runtime_config(cfg, "jar_service"), [])

    def test_app_home_required_and_absolute(self):
        errs = validate_runtime_config({"port": 8080}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))
        errs = validate_runtime_config({"app_home": "opt/a", "port": 8080}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))

    def test_port_required_for_network_readiness(self):
        errs = validate_runtime_config({"app_home": "/opt/a"}, "jar_service")
        self.assertTrue(any("port" in e for e in errs))

    def test_process_readiness_needs_no_port(self):
        cfg = {"app_home": "/opt/a", "readiness": {"type": "process"}}
        self.assertEqual(validate_runtime_config(cfg, "jar_service"), [])

    def test_bad_unit_name_rejected(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "unit_name": "bad name;rm"}
        self.assertTrue(any("unit_name" in e for e in validate_runtime_config(cfg, "jar_service")))

    def test_bad_timeouts_rejected(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "stop_timeout": 0}
        self.assertTrue(any("stop_timeout" in e for e in validate_runtime_config(cfg, "jar_service")))

    def test_env_files_must_be_absolute(self):
        cfg = {"app_home": "/opt/a", "port": 8080, "env_files": ["rel.env"]}
        self.assertTrue(any("env_files" in e for e in validate_runtime_config(cfg, "jar_service")))


class ArtifactTest(unittest.TestCase):
    def test_name(self):
        self.assertEqual(artifact_filename("hqb-auth", "1.0.31"), "hqb-auth-1.0.31.jar")

    def test_sha256_file(self):
        tmp = tempfile.mkdtemp()
        try:
            p = Path(tmp) / "a.jar"
            p.write_bytes(b"abc")
            self.assertEqual(
                sha256_file(p),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RegistryTest(unittest.TestCase):
    def test_registered_modes_is_public(self):
        modes = registered_modes()
        self.assertIn("container", modes)
        self.assertIn("static_archive", modes)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_config.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# deploy_strategies/jar_config.py
"""jar_service 运行时配置契约：解析、校验、产物命名与校验和

纯标准库实现，CLI 子进程（策略层/构建）与 Web 路由共用同一份规则——
校验口径只允许有一个来源，避免「前端允许、后端拒绝」或反之。
"""
import hashlib
import json
import re

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")

DEFAULTS = {
    "java_bin": "/usr/bin/java",
    "stop_timeout": 60,
    "start_timeout": 120,
    "artifact_pattern": "target/*.jar",
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
}


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_runtime_config(raw) -> dict:
    """把 DB/YAML 中的 JSON（dict 或字符串）归一为策略可直接消费的完整结构"""
    cfg = _as_dict(raw)
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    return {
        "app_home": str(cfg.get("app_home") or "").rstrip("/"),
        "java_bin": str(cfg.get("java_bin") or DEFAULTS["java_bin"]),
        "jvm_opts": str(cfg.get("jvm_opts") or ""),
        "app_args": str(cfg.get("app_args") or ""),
        "run_user": str(cfg.get("run_user") or ""),
        "work_dir": str(cfg.get("work_dir") or ""),
        "unit_name": str(cfg.get("unit_name") or ""),
        "port": _as_int(cfg.get("port"), 0),
        "readiness": {
            "type": str(rt.get("type") or DEFAULTS["readiness"]["type"]),
            "path": str(rt.get("path") or DEFAULTS["readiness"]["path"]),
            "timeout": _as_int(rt.get("timeout"), DEFAULTS["readiness"]["timeout"]),
            "interval": _as_int(rt.get("interval"), DEFAULTS["readiness"]["interval"]),
        },
        "stop_timeout": _as_int(cfg.get("stop_timeout"), DEFAULTS["stop_timeout"]),
        "start_timeout": _as_int(cfg.get("start_timeout"), DEFAULTS["start_timeout"]),
        "artifact_pattern": str(cfg.get("artifact_pattern") or DEFAULTS["artifact_pattern"]),
        "env_files": [str(p) for p in (cfg.get("env_files") or [])
                      if isinstance(p, str) and p],
    }


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """返回错误消息列表；仅 jar_service 需要校验（其它方式配置原样忽略）"""
    if (deploy_mode or "container") != "jar_service":
        return []
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return ["deploy_mode=jar_service 必须配置 runtime_config.app_home"]
    errors = []
    app_home = cfg.get("app_home")
    if not isinstance(app_home, str) or not app_home.startswith("/"):
        errors.append("runtime_config.app_home 必填，且必须是绝对路径")
    java_bin = cfg.get("java_bin", DEFAULTS["java_bin"])
    if not isinstance(java_bin, str) or not java_bin.startswith("/"):
        errors.append("runtime_config.java_bin 必须是绝对路径")
    unit_name = cfg.get("unit_name") or ""
    if unit_name and (not isinstance(unit_name, str) or not _UNIT_RE.match(unit_name)):
        errors.append("runtime_config.unit_name 只能包含字母、数字与 _ . @ -")
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rtype = rt.get("type") or DEFAULTS["readiness"]["type"]
    if rtype not in ("http", "tcp", "process"):
        errors.append("runtime_config.readiness.type 仅支持 http / tcp / process")
    port = cfg.get("port", 0)
    if rtype in ("http", "tcp"):
        if not isinstance(port, int) or isinstance(port, bool) or not (0 < port < 65536):
            errors.append("runtime_config.port 必填，且必须是 1-65535 的整数")
    elif port not in (0, None) and (not isinstance(port, int) or isinstance(port, bool)
                                    or not (0 < port < 65536)):
        errors.append("runtime_config.port 必须是 1-65535 的整数")
    for key in ("timeout", "interval"):
        value = rt.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value <= 0):
            errors.append(f"runtime_config.readiness.{key} 必须为正整数")
    for key in ("stop_timeout", "start_timeout"):
        value = cfg.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)
                                  or value <= 0):
            errors.append(f"runtime_config.{key} 必须为正整数")
    pattern = cfg.get("artifact_pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        errors.append("runtime_config.artifact_pattern 不能为空")
    env_files = cfg.get("env_files") or []
    if not isinstance(env_files, list) or any(
            not isinstance(p, str) or not p.startswith("/") for p in env_files):
        errors.append("runtime_config.env_files 必须是绝对路径数组")
    return errors


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
```

在 `deploy_strategies/base.py` 末尾加：

```python
def registered_modes() -> list:
    """已注册的发布方式（路由校验与前端选项读取同一来源，新增策略无需改两处）"""
    return sorted(_REGISTRY)
```

`config_routes.py` 的 `create_service` 中替换第 233-245 段：

```python
    from deploy_strategies.base import registered_modes
    from deploy_strategies.jar_config import validate_runtime_config

    deploy_mode = body.get("deploy_mode") or "container"
    if deploy_mode not in registered_modes():
        return _err(f"deploy_mode 仅支持 {' / '.join(registered_modes())}")
    is_static = deploy_mode == "static_archive"
    is_jar = deploy_mode == "jar_service"
    # 镜像/ compose 字段仅容器发布必需；静态与进程发布无镜像概念，留空时回退 name
    if not is_static and not is_jar:
        for k in ("image_name", "service_name"):
            if not isinstance(body.get(k), str) or not body.get(k).strip():
                return _err(f"{k} 必填")
    elif is_static:
        site_root = (body.get("site_root") or "").strip()
        if not site_root.startswith("/"):
            return _err("静态发布必须配置 site_root（站点根绝对路径，如 /data/1panel/www/sites/example.com/index）")
    else:
        errs = validate_runtime_config(body.get("runtime_config"), deploy_mode)
        if errs:
            return _err("；".join(errs))
```

同函数 `fields` 字典中 `deploy_mode=deploy_mode,` 之后加：

```python
                  runtime_config=body.get("runtime_config") or {},
```

`update_service` 中把 `allowed` 元组追加 `"runtime_config"`，并把第 314-315 行替换为：

```python
    mode = fields.get("deploy_mode") or rec.get("deploy_mode") or "container"
    if "deploy_mode" in fields:
        from deploy_strategies.base import registered_modes
        if fields["deploy_mode"] not in registered_modes():
            return _err(f"deploy_mode 仅支持 {' / '.join(registered_modes())}")
    # 切到 jar_service 或修改运行时配置都要校验：用「即将生效的值」而不是只看本次提交，
    # 否则从 container 切 jar 且不传 runtime_config 时会绕过 app_home 必填检查
    if mode == "jar_service":
        from deploy_strategies.jar_config import validate_runtime_config
        errs = validate_runtime_config(
            fields.get("runtime_config", rec.get("runtime_config")), mode)
        if errs:
            return _err("；".join(errs))
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_config.py -q`
Expected: 14 passed（Parse 3 + Validate 8 + Artifact 2 + Registry 1）

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/jar_config.py deploy_strategies/base.py webapp/routes/config_routes.py tests/test_jar_config.py
git commit -m "feat(config): jar_service 运行时配置单一校验源与注册表驱动的 deploy_mode 白名单"
```

---

### Task 3: YAML 导入导出往返

**Files:**
- Modify: `webapp/importer.py:58-84`
- Modify: `webapp/exporter.py:56-66`
- Test: `tests/test_config_roundtrip_jar.py`

**Interfaces:**
- Consumes: `services.runtime_config`（Task 1）
- Produces: YAML 中 `runtime_config` 键的往返；导入保持既有 insert-only 语义（同名服务整条跳过）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config_roundtrip_jar.py
"""jar_service 配置导出→导入往返：runtime_config 不丢失、不误伤 container"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import db as dbm
from webapp.exporter import export_projects_yaml
from webapp.importer import _import_projects


class RoundtripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_roundtrip_")
        self.src = os.path.join(self.tmp, "src.db")
        self.dst = os.path.join(self.tmp, "dst.db")
        dbm.init_db(self.src)
        dbm.init_db(self.dst)
        pid = dbm.create_record(self.src, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_record(self.src, "services", dict(
            project_id=pid, name="auth", image_name="auth", service_name="auth",
            deploy_mode="jar_service", runtime_config={
                "app_home": "/opt/auth", "port": 8080,
                "jvm_opts": "-Xmx1g", "env_files": ["/opt/auth/.env"]}))
        dbm.create_record(self.src, "services", dict(
            project_id=pid, name="web", image_name="web", service_name="web",
            deploy_mode="container", runtime_config={}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_then_import_preserves_runtime_config(self):
        pid = dbm.list_records(self.src, "projects")[0]["id"]
        text = export_projects_yaml(self.src, pid)
        path = Path(self.tmp) / "projects.yaml"
        path.write_text(text, encoding="utf-8")
        data = yaml.safe_load(text)
        self.assertEqual(data["projects"]["auth"]["runtime_config"]["app_home"], "/opt/auth")
        self.assertNotIn("runtime_config", data["projects"]["web"])
        ok, _ = _import_projects(self.dst, path)
        self.assertTrue(ok)
        rec = dbm.get_service_by_name(self.dst, 1, "auth")
        self.assertEqual(rec["runtime_config"]["app_home"], "/opt/auth")
        self.assertEqual(rec["deploy_mode"], "jar_service")

    def test_import_is_insert_only_and_does_not_clear_existing(self):
        # 既有服务同名 → 整条跳过，不能把线上 runtime_config 冲成 {}
        pid = dbm.create_record(self.dst, "projects", dict(
            name="平台", code="plat", description="", git_org="",
            created_at=int(time.time())))
        dbm.create_record(self.dst, "services", dict(
            project_id=pid, name="auth", image_name="auth", service_name="auth",
            deploy_mode="jar_service", runtime_config={"app_home": "/srv/keep"}))
        path = Path(self.tmp) / "empty.yaml"
        path.write_text("projects:\n  auth:\n    image_name: auth\n", encoding="utf-8")
        ok, _ = _import_projects(self.dst, path)
        self.assertTrue(ok)
        rec = dbm.get_service_by_name(self.dst, pid, "auth")
        self.assertEqual(rec["runtime_config"], {"app_home": "/srv/keep"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_config_roundtrip_jar.py -q`
Expected: FAIL（导出无 `runtime_config`；导入不写该字段）

- [ ] **Step 3: 实现**

`webapp/importer.py` 的 `create_record(..., "services", dict(...))` 中，`deploy_mode=` 之后加：

```python
            runtime_config=cfg.get("runtime_config") or {},
```

`webapp/exporter.py` 第 59-66 行改为：

```python
        # 发布方式与方式专属字段仅非默认时输出（container 项目导出不新增字段，向后兼容）
        deploy_mode = s.get("deploy_mode") or "container"
        if deploy_mode != "container":
            cfg["deploy_mode"] = deploy_mode
            for key in ("site_root", "backup_dir", "static_dist_dir",
                        "zip_prefix", "entry_file", "file_mode"):
                if s.get(key):
                    cfg[key] = s[key]
        if deploy_mode == "jar_service" and s.get("runtime_config"):
            # 进程发布没有镜像/站点字段，JVM 与单元参数必须完整导出，
            # 否则从 YAML 重建环境时会静默退回缺省值
            cfg["runtime_config"] = s["runtime_config"]
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_config_roundtrip_jar.py -q`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add webapp/importer.py webapp/exporter.py tests/test_config_roundtrip_jar.py
git commit -m "feat(config): jar_service runtime_config 的 YAML 导入导出往返"
```

---

### Task 4: 构建产物契约（build.py）

**Files:**
- Modify: `scripts/build.py:364-372`（`_run_build_steps`）、`:450-505`（新增产物函数、`build_project` 分支）
- Test: `tests/test_build_jar_artifact.py`

**Interfaces:**
- Consumes: `runtime_config.artifact_pattern`（Task 2 的解析规则）
- Produces:
  - `_select_jar(proj: dict, project_root: Path) -> Path`
  - `_make_jar_artifact(proj: dict, project_root: Path, version: str, config: dict) -> Path`
  - 产物：`{dist_dir}/{service}-{version}.jar` 与同名 `.sha256`（内容为十六进制摘要 + 换行）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_build_jar_artifact.py
"""构建产物契约：可执行 jar 选择规则、校验和生成、docker 零调用"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build as build_mod


class SelectJarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jar_build_")
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel, size):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    def test_picks_largest_and_skips_noise(self):
        self._touch("target/app-1.0.jar", 10)
        self._touch("target/app-1.0-sources.jar", 999)
        self._touch("target/app-1.0-original.jar", 888)
        self._touch("target/app-1.0-exec.jar", 500)
        proj = {"runtime_config": {"artifact_pattern": "target/*.jar"}}
        picked = build_mod._select_jar(proj, self.root)
        self.assertEqual(picked.name, "app-1.0-exec.jar")

    def test_falls_back_to_build_step_paths(self):
        self._touch("svc-a/target/svc-a.jar", 100)
        proj = {"build_order": [{"path": "svc-a"}], "runtime_config": {}}
        picked = build_mod._select_jar(proj, self.root)
        self.assertEqual(picked.name, "svc-a.jar")

    def test_no_match_fails_with_listing(self):
        self._touch("target/only-sources.jar", 10)
        proj = {"runtime_config": {}}
        with self.assertRaises(SystemExit):
            build_mod._select_jar(proj, self.root)

    def test_make_artifact_and_sha256(self):
        jar = self._touch("target/app.jar", 123)
        proj = {"runtime_config": {}, "image_name": "hqb-auth"}
        with mock.patch.object(build_mod, "log"):
            out = build_mod._make_jar_artifact(proj, self.root, "1.0.31",
                                               {"deploy": {"dist_dir": str(self.root / "out")}})
        self.assertEqual(out.name, "hqb-auth-1.0.31.jar")
        self.assertEqual(out.stat().st_size, 123)
        side = out.with_name(out.name + ".sha256")
        self.assertTrue(side.exists())
        self.assertEqual(len(side.read_text(encoding="utf-8").strip().split()[0]), 64)


class BuildProjectBranchTest(unittest.TestCase):
    def test_jar_service_never_calls_docker(self):
        tmp = tempfile.mkdtemp(prefix="jar_build_project_")
        try:
            root = Path(tmp)
            (root / "target").mkdir()
            (root / "target" / "app.jar").write_bytes(b"x" * 10)
            proj = {"deploy_mode": "jar_service", "build_type": "none",
                    "image_name": "hqb-auth", "runtime_config": {},
                    "build_order": []}
            config = {"deploy": {"project_root": str(root), "dist_dir": str(root / "out"),
                                 "docker_compose_path": "", "remote_tmp_dir": "/tmp/x"},
                      "registry": {"host": "h", "namespace": "n"},
                      "deploy_mode": "direct"}
            with mock.patch.object(build_mod, "load_config", return_value=config), \
                    mock.patch.object(build_mod, "get_project_config", return_value=proj), \
                    mock.patch.object(build_mod, "get_registry_auth", return_value={}), \
                    mock.patch.object(build_mod, "get_registry_mode", return_value="direct"), \
                    mock.patch.object(build_mod, "git_prepare"), \
                    mock.patch.object(build_mod, "docker_login") as login, \
                    mock.patch.object(build_mod.subprocess, "run") as sp:
                build_mod.build_project("auth", "1.0.31", env="test")
            login.assert_not_called()
            sp.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_build_jar_artifact.py -q`
Expected: FAIL（`_select_jar` / `_make_jar_artifact` 不存在；jar_service 仍走 docker build）

- [ ] **Step 3: 实现**

`scripts/build.py` 顶部导入区加 `import hashlib` 与 `import shutil`（若已有则不重复）。

`_run_build_steps` 在现有 `build_type = proj.get("build_type", "maven")`（第 367 行）之后加：

```python
    if build_type == "none":
        # 产物由外部投入（build_type=none）：不执行任何构建命令，只做产物校验
        log("构建类型 none：跳过构建，直接使用现成产物")
        return
```

在 `_make_static_zip` 之后新增：

```python
_JAR_NOISE_SUFFIXES = ("-sources.jar", "-javadoc.jar", "-original.jar")


def _jar_search_dirs(proj: dict, project_root: Path) -> list:
    """产物搜索目录：项目根优先，其次各构建模块目录（多模块项目 jar 在子模块 target/）"""
    dirs = [project_root]
    for step in proj.get("build_order", []) or []:
        candidate = project_root / (step.get("path") or ".")
        if candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _select_jar(proj: dict, project_root: Path) -> Path:
    """按 artifact_pattern 选择可执行 jar

    命中多个时的裁决规则（写入设计文档，避免依赖 readdir 顺序）：
    排除测试/源码/原始包后取体积最大者——Spring Boot repackaged fat jar 必然大于 thin jar。
    """
    pattern = (proj.get("runtime_config") or {}).get("artifact_pattern") or "target/*.jar"
    first_listing = ""
    for base in _jar_search_dirs(proj, project_root):
        matches = [p for p in base.glob(pattern)
                   if p.is_file() and not p.name.endswith(_JAR_NOISE_SUFFIXES)]
        if matches:
            picked = max(matches, key=lambda p: p.stat().st_size)
            log(f"选中构建产物: {picked}（{picked.stat().st_size / 1024 / 1024:.1f} MB，"
                f"候选 {len(matches)} 个）")
            return picked
        if not first_listing and base.exists():
            names = sorted(p.name for p in base.glob("**/*.jar"))
            first_listing = "、".join(names) or "（无 jar）"
    fail(f"未找到可执行 jar（pattern={pattern}，基准目录 {project_root}）；"
         f"现有 jar: {first_listing or '（无）'}")


def _make_jar_artifact(proj: dict, project_root: Path, version: str, config: dict) -> Path:
    """把选中的 jar 拷为 dist/{service}-{version}.jar 并生成 .sha256 旁文件

    命名与 deploy_strategies.jar_service 的约定一致；不重命名原始 target 产物，
    保留构建工具自身的校验能力（IDE/CI 仍可复用）。
    """
    src = _select_jar(proj, project_root)
    service = proj.get("image_name") or proj.get("name") or "app"
    dist_dir = Path(config["deploy"].get("dist_dir", "dist"))
    if not dist_dir.is_absolute():
        dist_dir = Path(__file__).resolve().parent.parent / dist_dir
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / f"{service}-{version}.jar"
    shutil.copy2(src, out)
    h = hashlib.sha256()
    with open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    out.with_name(out.name + ".sha256").write_text(h.hexdigest() + "\n", encoding="utf-8")
    log(f"jar 产物已生成: {out}（{out.stat().st_size / 1024 / 1024:.1f} MB，sha256={h.hexdigest()[:12]}…）")
    return out
```

`build_project` 改为按产物形态分支（替换第 478-505 段）：

```python
    deploy_mode = proj.get("deploy_mode") or "container"
    artifact = {"static_archive": "archive", "jar_service": "jar"}.get(deploy_mode, "image")
    # 只有 maven 构建才需要 profile；frontend 与 none（外部投入产物）都不该因环境名
    # 不在 local/dev/test/prod 而被 get_maven_profile 拦下
    profile = get_maven_profile(config, env) if build_type == "maven" else ""
    project_root = Path(config["deploy"]["project_root"]).expanduser()
    if not project_root.exists():
        log(f"警告: 项目根目录不存在: {project_root}（Docker 部署时请勾选「跳过构建」，或在宿主机上确认路径存在）")
        return

    git_prepare(proj, project_root)

    if artifact != "image":
        log(f"发布方式 {deploy_mode}（产物 {artifact}）: 跳过 docker login")
    elif mode == "push":
        docker_login(registry)
    else:
        log(f"直传模式（{mode}）: 跳过 docker login / docker push")

    env_vars = os.environ.copy()
    _run_build_steps(proj, project_root, version, profile, env_vars, config)

    if artifact == "archive":
        _make_static_zip(proj, project_root, version, config)
        return
    if artifact == "jar":
        _make_jar_artifact(proj, project_root, version, config)
        return
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_build_jar_artifact.py tests/test_build_step_env.py tests/test_build_clean_retry.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add scripts/build.py tests/test_build_jar_artifact.py
git commit -m "feat(build): jar_service 产物分支与 build_type=none，构建链路不触 docker"
```

---

### Task 5: 策略骨架、注册与版本查询

**Files:**
- Create: `deploy_strategies/jar_service.py`
- Modify: `deploy_strategies/__init__.py:4-5`
- Test: `tests/test_jar_service.py`（新建，后续 Task 6/7/8 在同一文件追加）

**Interfaces:**
- Consumes: `parse_runtime_config`、`artifact_filename`、`sha256_file`（Task 2）
- Produces: `JarServiceStrategy`（`name="jar_service"`），方法：
  - `jar_name(version=None) -> str`、`local_jar(version=None) -> Path`
  - `check_local()`、`query_current_version()`、`next_version(current)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_jar_service.py
"""jar_service 策略探针：Fake SSH 断言命令序列，不触真实服务器"""
import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import deploy as deploy_mod
from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY
from deploy_strategies.jar_config import sha256_file
from deploy_strategies.jar_service import JarServiceStrategy

APP_HOME = "/opt/apps/hqb-auth"


class FakeRemote:
    """模拟 RemoteServer：exec_calls 记录命令；rules 按子串匹配返回值/失败"""

    def __init__(self, host="10.0.0.1", rules=None, try_exec_ok=None):
        self.host = host
        self.exec_calls = []
        self.uploaded = []
        self.closed = False
        self.rules = rules or {}
        self.try_exec_ok = try_exec_ok or {}

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.exec_calls.append(cmd)
        for key, val in self.rules.items():
            if key in cmd:
                if isinstance(val, Exception):
                    raise val
                return val
        return ""

    def try_exec(self, cmd, timeout=60):
        self.exec_calls.append(cmd)
        for key, val in self.try_exec_ok.items():
            if key in cmd:
                return val
        return True

    def sftp_put_with_progress(self, local_path, remote_path, label="", streams=1):
        self.uploaded.append((local_path, remote_path, label))

    def close(self):
        self.closed = True


def make_jar_deployer(tmp_dir: str, version="1.0.31", previous_version=None,
                      runtime_config=None) -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "auth"
    d.version = version
    d.previous_version = previous_version
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0, "health_timeout": "1",
                           "health_interval": "0"}}
    d.ssh_cfg = {}
    d.proj_cfg = {
        "deploy_mode": "jar_service",
        "image_name": "hqb-auth",
        "runtime_config": runtime_config or {
            "app_home": APP_HOME, "java_bin": "/usr/bin/java", "port": 8080,
            "readiness": {"type": "process"},
        },
    }
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_jar(deployer: Deployer, version=None, payload=b"jar-bytes"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"hqb-auth-{version}.jar"
    p.write_bytes(payload)
    (p.with_name(p.name + ".sha256")).write_text(sha256_file(p) + "\n", encoding="utf-8")
    return p


class TestRegistry(unittest.TestCase):
    def test_registered_and_routed(self):
        self.assertIn("jar_service", _REGISTRY)
        d = make_jar_deployer(tempfile.mkdtemp())
        self.assertIsInstance(d.strategy, JarServiceStrategy)

    def test_capability_flags(self):
        s = create_strategy("jar_service", make_jar_deployer(tempfile.mkdtemp()))
        self.assertTrue(s.supports_nacos)
        self.assertTrue(s.supports_canary)
        self.assertTrue(s.supports_prefetch)
        self.assertTrue(s.supports_restart)
        self.assertEqual(s.version_scheme, "semver")

    def test_app_home_required(self):
        d = make_jar_deployer(tempfile.mkdtemp(), runtime_config={})
        d.proj_cfg["runtime_config"] = {}
        with self.assertRaises(ValueError) as ctx:
            create_strategy("jar_service", d)
        self.assertIn("app_home", str(ctx.exception))

    def test_defaults(self):
        s = create_strategy("jar_service", make_jar_deployer(tempfile.mkdtemp()))
        self.assertEqual(s.app_home, APP_HOME)
        self.assertEqual(s.unit, "hqb-auth")
        self.assertEqual(s.jar_name("1.0.31"), "hqb-auth-1.0.31.jar")


class TestCheckLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_jar_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("本地 jar 不存在", str(ctx.exception))

    def test_missing_sidecar_raises(self):
        p = Path(self.tmp) / "hqb-auth-1.0.31.jar"
        p.write_bytes(b"x")
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("校验文件", str(ctx.exception))

    def test_checksum_mismatch_raises(self):
        p = make_jar(self.d)
        p.write_bytes(b"tampered")
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check_local()
        self.assertIn("校验和不一致", str(ctx.exception))

    def test_ok(self):
        make_jar(self.d)
        self.s.check_local()


class TestVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_next_version_bumps_patch(self):
        self.assertEqual(self.d.strategy.next_version("1.0.29"), "1.0.30")

    def test_next_version_invalid_raises(self):
        with self.assertRaises(ValueError):
            self.d.strategy.next_version("20261012")

    def _query(self, rules):
        fake = FakeRemote(rules=rules)
        with mock.patch("deploy_strategies.jar_service.default_servers_file",
                        return_value="servers.test.yaml"), \
                mock.patch("deploy_strategies.jar_service.load_yaml",
                           return_value={"servers": [{"ip": "10.0.0.1"}]}), \
                mock.patch("deploy_strategies.jar_service.get_ssh_config",
                           return_value={}), \
                mock.patch("deploy_strategies.jar_service.merge_server_ssh",
                           return_value={}), \
                mock.patch.object(deploy_mod, "RemoteServer", return_value=fake):
            return self.d.strategy.query_current_version(), fake

    def test_query_reads_marker(self):
        ver, _ = self._query({"cat": "1.0.30\n"})
        self.assertEqual(ver, "1.0.30")

    def test_query_none_when_no_marker(self):
        ver, _ = self._query({"cat": ""})
        self.assertIsNone(ver)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# deploy_strategies/jar_service.py
"""纯 Java 进程发布策略（deploy_mode: jar_service，Linux + systemd）

产物按版本不可变存放，CURRENT 符号链接原子切换，systemd unit 的 ExecStart 固定
引用 {app_home}/CURRENT/app.jar：发布与回滚都只是「换链接 + 重启单元」，回滚不需要重传。
权威版本状态在目标机（.deploy_version 标记 + CURRENT 链接），平台库只做展示缓存。

与容器模式的根本差异：单实例重启必然有停机窗口，切换语义是「停机→换链接→启动」，
不承诺零停机；Nacos 上下线仍由编排层按能力标志调度，与运行时方式无关。
"""
import shlex
import time
from pathlib import Path

from .base import DeployStrategy, register
from .jar_config import (artifact_filename, parse_runtime_config,
                         sha256_file)
from common import (log, load_yaml, default_servers_file, merge_server_ssh,
                    get_ssh_config)


@register
class JarServiceStrategy(DeployStrategy):
    name = "jar_service"
    supports_nacos = True
    supports_canary = True
    supports_prefetch = True
    supports_restart = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        self.cfg = parse_runtime_config(proj.get("runtime_config"))
        if not self.cfg["app_home"]:
            raise ValueError(
                "deploy_mode=jar_service 必须配置 runtime_config.app_home"
                "（如 /opt/apps/hqb-auth）")
        self.app_home = self.cfg["app_home"]
        self.service = (proj.get("image_name") or
                        getattr(deployer, "project", "app"))
        self.unit = self.cfg["unit_name"] or self.service
        self.jars_dir = f"{self.app_home}/releases"
        self.curr_link = f"{self.app_home}/CURRENT"

    # ---------- 产物定位 ----------

    def jar_name(self, version: str = None) -> str:
        return artifact_filename(self.service, version or self.d.version)

    def local_jar(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / self.jar_name(version)

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        p = self.local_jar()
        if not p.exists():
            raise RuntimeError(
                f"本地 jar 不存在: {p}（先执行构建，或把产物与 .sha256 放入 dist 目录）")
        side = p.with_name(p.name + ".sha256")
        if not side.exists():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        if expected != sha256_file(p):
            raise RuntimeError(
                f"jar 校验和不一致: {p}（产物被改动或旁文件过期，请重新构建）")

    def query_current_version(self):
        """SSH 首台读 {app_home}/.deploy_version；无标记视为首次发布（返回 None）"""
        import deploy as deploy_mod
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get("servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        ssh_cfg = merge_server_ssh(get_ssh_config(self.d.config), first)
        server = deploy_mod.RemoteServer(first["ip"], ssh_cfg)
        try:
            out = server.exec(
                f"cat {shlex.quote(self.app_home + '/.deploy_version')} 2>/dev/null || true",
                timeout=15).strip()
            return out or None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)
```

`deploy_strategies/__init__.py` 加一行：

```python
from . import jar_service  # noqa: F401 — 触发 @register JarServiceStrategy
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: 12 passed（Registry 4 + CheckLocal 4 + Version 4）

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/jar_service.py deploy_strategies/__init__.py tests/test_jar_service.py
git commit -m "feat(deploy): jar_service 策略注册、产物预检与 semver 版本推导"
```

---

### Task 6: 预检、单元就绪、传输与并行预传

**Files:**
- Modify: `deploy_strategies/jar_service.py`（追加四阶段）
- Test: `tests/test_jar_service.py`（追加 `TestCheck/TestTransfer/TestPrefetch`）

**Interfaces:**
- Consumes: `self.cfg`、`self.jars_dir`、`self.local_jar()`
- Produces: `check(server)`、`prepare(server)`、`transfer(server, version=None)`、`prefetch(servers) -> list[str]`

- [ ] **Step 1: 写失败测试（追加到 tests/test_jar_service.py）**

```python
class TestCheckPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy
        make_jar(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_commands(self):
        server = FakeRemote(rules={"df -Pk": "1048576\n"})
        self.s.check(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn("command -v /usr/bin/java", joined)
        self.assertIn("command -v systemctl", joined)
        self.assertIn(f"mkdir -p {APP_HOME}/releases", joined)

    def test_check_disk_insufficient_raises(self):
        # 默认测试 jar 仅 9 字节 → 需要 1KB；df 返回 0 必然不足
        server = FakeRemote(rules={"df -Pk": "0\n"})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.check(server)
        self.assertIn("磁盘空间不足", str(ctx.exception))

    def test_prepare_requires_installed_unit(self):
        server = FakeRemote(try_exec_ok={"systemctl cat": False})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.prepare(server)
        self.assertIn("未接入", str(ctx.exception))

    def test_prepare_rejects_foreign_unit(self):
        server = FakeRemote(rules={
            "systemctl show": "ExecStart={ path=/usr/local/bin/java ; argv[]=/usr/local/bin/java -jar /srv/other.jar }",
        })
        with self.assertRaises(RuntimeError) as ctx:
            self.s.prepare(server)
        self.assertIn("拒绝接管", str(ctx.exception))

    def test_prepare_accepts_platform_unit(self):
        server = FakeRemote(rules={
            "systemctl show": f"ExecStart={{ path=/usr/bin/java ; argv[]=/usr/bin/java -jar {APP_HOME}/CURRENT/app.jar }}",
        })
        self.s.prepare(server)


class TestTransferPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy
        self.jar = make_jar(self.d)
        self.sha = sha256_file(self.jar)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_uploads_to_version_dir_and_verifies_sha(self):
        server = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": self.sha + "\n"})
        self.assertTrue(self.s.transfer(server))
        local, remote, _ = server.uploaded[0]
        self.assertTrue(local.endswith("hqb-auth-1.0.31.jar"))
        self.assertEqual(remote, f"{APP_HOME}/releases/1.0.31/app.jar")
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"mkdir -p {APP_HOME}/releases/1.0.31", joined)

    def test_skips_when_remote_sha_matches(self):
        server = FakeRemote(try_exec_ok={"test -f": True},
                            rules={"sha256sum": self.sha + "\n"})
        self.s.transfer(server)
        self.assertEqual(server.uploaded, [])

    def test_remote_sha_mismatch_after_upload_raises(self):
        server = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": "deadbeef\n"})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.transfer(server)
        self.assertIn("校验和不一致", str(ctx.exception))

    def test_missing_local_jar_raises(self):
        self.jar.unlink()
        with self.assertRaises(RuntimeError):
            self.s.transfer(FakeRemote())


class TestPrefetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        make_jar(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefetch_returns_failed_hosts(self):
        ok = FakeRemote(host="10.0.0.1", try_exec_ok={"test -f": False},
                        rules={"sha256sum": sha256_file(
                            Path(self.tmp) / "hqb-auth-1.0.31.jar") + "\n"})
        bad = FakeRemote(host="10.0.0.2",
                         rules={"sftp": RuntimeError("disk full")})
        bad.sftp_put_with_progress = mock.Mock(side_effect=RuntimeError("disk full"))
        with mock.patch.object(deploy_mod, "RemoteServer",
                               side_effect=[ok, bad]), \
                mock.patch("deploy_strategies.jar_service.log"):
            failures = self.d.strategy.prefetch([{"ip": "10.0.0.1"},
                                                 {"ip": "10.0.0.2"}])
        self.assertEqual(failures, ["10.0.0.2"])
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: FAIL（`check`/`prepare`/`transfer`/`prefetch` 未实现，抛 NotImplementedError）

- [ ] **Step 3: 实现（追加到 `jar_service.py`）**

```python
    # ---------- 五阶段 ----------

    def check(self, server):
        """预检：Java、systemd、目录可写、磁盘余量——全部在造成变更前完成"""
        server.exec(f"command -v {shlex.quote(self.cfg['java_bin'])} >/dev/null", timeout=15)
        server.exec("command -v systemctl >/dev/null", timeout=15)
        server.exec(
            f"mkdir -p {shlex.quote(self.jars_dir)} && test -w {shlex.quote(self.app_home)}",
            timeout=15)
        self._check_disk(server)

    def _check_disk(self, server):
        """按包大小的 1.5 倍检查可用空间：留出解压/临时文件与一次失败重传的余量"""
        p = self.local_jar()
        if not p.exists():
            return  # 本地产物缺失由 check_local/transfer 报错，这里不重复报
        need_kb = int(p.stat().st_size * 1.5 / 1024) + 1
        out = server.exec(
            f"df -Pk {shlex.quote(self.app_home)} | awk 'NR==2 {{print $4}}'",
            timeout=15).strip()
        try:
            avail_kb = int(out)
        except ValueError:
            log(f"[{server.host}] 无法解析磁盘余量（{out!r}），跳过空间预检")
            return
        if avail_kb < need_kb:
            raise RuntimeError(
                f"[{server.host}] 磁盘空间不足: {self.app_home} 剩余 {avail_kb}KB，"
                f"需要约 {need_kb}KB（含 1.5 倍余量）")

    def prepare(self, server):
        """确认单元已由平台接入，且不会被误当成别人创建的服务接管

        平台对 unit 的承诺是「只管理自己创建的」：ExecStart 不指向 CURRENT/app.jar
        的单元一律拒绝，避免把人工维护的服务改坏。
        """
        installed = server.try_exec(
            f"systemctl cat {shlex.quote(self.unit)}.service >/dev/null 2>&1", timeout=15)
        if not installed:
            raise RuntimeError(
                f"[{server.host}] systemd 单元 {self.unit}.service 未接入"
                f"（先在服务配置页执行「生成并接入单元」，需管理员确认）")
        exec_start = server.exec(
            f"systemctl show -p ExecStart --value {shlex.quote(self.unit)}.service",
            timeout=15)
        expected = f"{self.curr_link}/app.jar"
        if expected not in exec_start:
            raise RuntimeError(
                f"[{server.host}] 拒绝接管非平台创建的单元 {self.unit}.service："
                f"ExecStart 未指向 {expected}")

    def transfer(self, server, version: str = None):
        """上传到 releases/{version}/app.jar（幂等：远端 sha256 相同则跳过）"""
        ver = version or self.d.version
        local = self.local_jar(ver)
        if not local.exists():
            raise RuntimeError(f"本地 jar 不存在: {local}")
        local_sha = sha256_file(local)
        remote_dir = f"{self.jars_dir}/{ver}"
        remote = f"{remote_dir}/app.jar"
        server.exec(f"mkdir -p {shlex.quote(remote_dir)}", timeout=15)
        if server.try_exec(f"test -f {remote}", timeout=15):
            current = server.exec(
                f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
            if current == local_sha:
                log(f"[{server.host}] 远端已有相同产物，跳过上传: {ver}")
                return True
        log(f"[{server.host}] 上传 jar {self.jar_name(ver)}"
            f"（{local.stat().st_size / 1024 / 1024:.1f} MB）-> {remote}")
        server.sftp_put_with_progress(
            str(local), remote, label=self.jar_name(ver),
            streams=getattr(self.d, "upload_streams", 1))
        # 传输后必须复核：SFTP 中断可能留下半包，宁可在这里失败也不要切到坏包
        remote_sha = server.exec(
            f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
        if remote_sha != local_sha:
            raise RuntimeError(
                f"[{server.host}] 上传后校验和不一致（本地 {local_sha[:12]}…，"
                f"远端 {remote_sha[:12]}…）")
        return True

    def prefetch(self, servers: list) -> list:
        """并行预传（失败不中断，滚动阶段自动重试），与静态策略同构"""
        try:
            self.check_local()
        except Exception as e:
            log(f"jar 预传输跳过: {e}")
            return [s["ip"] for s in servers]
        import deploy as deploy_mod
        from concurrent.futures import ThreadPoolExecutor
        log(f">>> 并行预上传 jar 到 {len(servers)} 台服务器")

        def _one(srv: dict) -> tuple:
            host = srv["ip"]
            server = deploy_mod.RemoteServer(host, merge_server_ssh(self.d.ssh_cfg, srv))
            try:
                self.transfer(server)
                log(f"[{host}] jar 预传输完成")
                return host, True
            except Exception as e:
                log(f"[{host}] jar 预传输失败: {e}")
                return host, False
            finally:
                server.close()

        with ThreadPoolExecutor(max_workers=max(1, min(len(servers), 8))) as pool:
            results = list(pool.map(_one, servers))
        failures = [host for host, ok in results if not ok]
        log(f"jar 预传输完成：成功 {len(servers) - len(failures)} 台" +
            (f"，失败 {len(failures)} 台: {failures}（滚动阶段自动重试）" if failures else "，全部就绪"))
        return failures
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/jar_service.py tests/test_jar_service.py
git commit -m "feat(deploy): jar_service 预检/单元就绪/幂等传输/并行预传"
```

---

### Task 7: 切换与就绪验证

**Files:**
- Modify: `deploy_strategies/jar_service.py`（追加 `switch`/`verify`/`_dump_journal`）
- Test: `tests/test_jar_service.py`（追加 `TestSwitch/TestVerify`）

**Interfaces:**
- Consumes: `health_check.check_http`、`health_check.check_tcp_port`
- Produces: `switch(server)`（置位 `self.d.container_modified`，Task 8 起由 `target_modified` 别名接管）、`verify(server, server_entry=None)`

- [ ] **Step 1: 写失败测试（追加）**

```python
class TestSwitchPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy
        make_jar(self.d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_stops_relinks_starts_and_marks_modified(self):
        server = FakeRemote()
        self.s.switch(server)
        joined = "\n".join(server.exec_calls)
        # Task 8 统一改名为 target_modified（此处先用既有属性名，保证本任务独立可过）
        self.assertTrue(self.d.container_modified)
        self.assertIn("systemctl stop hqb-auth.service", joined)
        self.assertIn(f"ln -sfn {APP_HOME}/releases/1.0.31 {APP_HOME}/.CURRENT.tmp", joined)
        self.assertIn(f"mv -Tf {APP_HOME}/.CURRENT.tmp {APP_HOME}/CURRENT", joined)
        self.assertIn("systemctl start hqb-auth.service", joined)
        self.assertIn(f"echo 1.0.31 > {APP_HOME}/.deploy_version", joined)
        self.assertNotIn("docker", joined)

    def test_switch_failure_after_link_keeps_flag_set(self):
        server = FakeRemote(rules={"systemctl start": RuntimeError("start failed")})
        with self.assertRaises(RuntimeError):
            self.s.switch(server)
        self.assertTrue(self.d.container_modified)


class TestVerifyPhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp)
        self.s = self.d.strategy

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_process_mode_only_checks_active(self):
        server = FakeRemote()
        self.s.verify(server)
        self.assertIn("systemctl is-active --quiet hqb-auth.service",
                      server.exec_calls[0])

    def test_inactive_dumps_journal_and_raises(self):
        server = FakeRemote(rules={"systemctl is-active": RuntimeError("inactive")})
        with self.assertRaises(RuntimeError) as ctx:
            self.s.verify(server)
        self.assertIn("active", str(ctx.exception))
        # verify 失败时必须在抛错前抓取 journal 尾部（否则线上只剩一句「未 active」）
        self.assertTrue(any("journalctl" in c for c in server.exec_calls))

    def test_http_readiness_success(self):
        self.d.proj_cfg["runtime_config"] = {
            "app_home": APP_HOME, "port": 8080,
            "readiness": {"type": "http", "path": "/actuator/health",
                          "timeout": 5, "interval": 1}}
        s = self.d.strategy
        with mock.patch("health_check.check_http", return_value=True) as ch:
            s.verify(FakeRemote())
        self.assertEqual(ch.call_args[0][0], "http://10.0.0.1:8080/actuator/health")
        self.assertTrue(ch.call_args.kwargs["require_health_body"])

    def test_http_readiness_timeout_raises(self):
        self.d.proj_cfg["runtime_config"] = {
            "app_home": APP_HOME, "port": 8080,
            "readiness": {"type": "http", "path": "/health",
                          "timeout": 1, "interval": 1}}
        s = self.d.strategy
        with mock.patch("health_check.check_http", return_value=False), \
                mock.patch("deploy_strategies.jar_service.time.sleep"), \
                mock.patch("deploy_strategies.jar_service.time.time",
                           side_effect=[0, 0, 2]), \
                mock.patch("deploy_strategies.jar_service.log"):
            with self.assertRaises(RuntimeError) as ctx:
                s.verify(FakeRemote())
        self.assertIn("健康检查超时", str(ctx.exception))

    def test_tcp_readiness(self):
        self.d.proj_cfg["runtime_config"] = {
            "app_home": APP_HOME, "port": 9000,
            "readiness": {"type": "tcp", "timeout": 5, "interval": 1}}
        s = self.d.strategy
        # 优先在目标机内部探测回环端口（云安全组常挡外部直连），探测机制不可用时才直连
        with mock.patch("health_check.check_tcp_port_via_ssh",
                        return_value=None) as via, \
                mock.patch("health_check.check_tcp_port", return_value=True) as ch:
            s.verify(FakeRemote())
        via.assert_called_once()
        ch.assert_called_once_with("10.0.0.1", 9000)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: FAIL（`switch`/`verify` 未实现）

- [ ] **Step 3: 实现（追加）**

```python
    def switch(self, server):
        """停机 → 原子换链接 → 启动 → 写版本标记

        换链接开始即视为目标状态已变更（编排层据此在后续失败时回滚）；
        链接用「临时名 + mv -Tf」替换，避免 ln -sfn 在目录已存在时的嵌套行为。
        """
        self.d.container_modified = True  # Task 8 统一改名为 target_modified
        ver = self.d.version
        target = f"{self.jars_dir}/{ver}"
        tmp = f"{self.app_home}/.CURRENT.tmp"
        server.exec(f"systemctl stop {shlex.quote(self.unit)}.service || true",
                    timeout=self.cfg["stop_timeout"] + 30)
        server.exec(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(tmp)} && "
            f"mv -Tf {shlex.quote(tmp)} {shlex.quote(self.curr_link)}",
            timeout=60)
        server.exec(f"systemctl start {shlex.quote(self.unit)}.service",
                    timeout=self.cfg["start_timeout"] + 30)
        server.exec(
            f"echo {shlex.quote(ver)} > {shlex.quote(self.app_home + '/.deploy_version')} && "
            f"chmod 0644 {shlex.quote(self.app_home + '/.deploy_version')}",
            timeout=15)
        log(f"[{server.host}] 已切换 CURRENT -> releases/{ver} 并启动 {self.unit}.service")

    def verify(self, server, server_entry: dict = None):
        """进程存活 + 就绪探测；失败先抓 journal 尾部再抛错，避免只剩一句「超时」"""
        try:
            server.exec(f"systemctl is-active --quiet {shlex.quote(self.unit)}.service",
                        timeout=15)
        except Exception as e:
            self._dump_journal(server)
            raise RuntimeError(
                f"[{server.host}] 单元 {self.unit}.service 未处于 active 状态") from e
        rt = self.cfg["readiness"]
        if rt["type"] == "process":
            return
        deadline = time.time() + rt["timeout"]
        if rt["type"] == "http":
            from health_check import check_http
            url = f"http://{server.host}:{self.cfg['port']}{rt['path']}"
            while time.time() < deadline:
                if check_http(url, require_health_body=True):
                    log(f"[{server.host}] 健康检查通过: {url}")
                    return
                time.sleep(rt["interval"])
            self._dump_journal(server)
            raise RuntimeError(f"[{server.host}] 健康检查超时: {url}")
        from health_check import check_tcp_port, check_tcp_port_via_ssh
        while time.time() < deadline:
            # 与容器发布同口径：优先经 SSH 在目标机内部探 127.0.0.1（业务端口常不对公网
            # 放行，平台机直连会假超时）；探测机制不可用（None）时才回退直连
            verdict = check_tcp_port_via_ssh(
                server.host, self.cfg["port"],
                getattr(server, "ssh_cfg", {}) or {})
            if verdict is True or (verdict is None and
                                   check_tcp_port(server.host, self.cfg["port"])):
                log(f"[{server.host}] TCP {self.cfg['port']} 已就绪")
                return
            time.sleep(rt["interval"])
        self._dump_journal(server)
        raise RuntimeError(
            f"[{server.host}] TCP 端口 {self.cfg['port']} 在 {rt['timeout']}s 内未就绪")

    def _dump_journal(self, server, lines: int = 80):
        """失败现场取证：journal 尾部进任务日志，便于直接定位启动异常"""
        try:
            out = server.exec(
                f"journalctl -u {shlex.quote(self.unit)}.service -n {lines} --no-pager "
                f"2>/dev/null || true", timeout=30)
            if out.strip():
                log(f"[{server.host}] {self.unit}.service 最近日志:\n{out.rstrip()}")
        except Exception as e:
            log(f"[{server.host}] 读取 journal 失败: {e}")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_service.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/jar_service.py tests/test_jar_service.py
git commit -m "feat(deploy): jar_service 原子换链接切换与进程/HTTP/TCP 就绪验证"
```

---

### Task 8: 回滚、编排层语义统一与主机状态回写

**Files:**
- Modify: `deploy_strategies/base.py`（新增默认钩子）
- Modify: `deploy_strategies/jar_service.py`（追加 `rollback`、`rollback_source_missing`）
- Modify: `deploy.py:485-552`（`target_modified` 属性）、`:666`、`:888-933`、`:1146-1160`
- Test: `tests/test_jar_service.py`（追加回滚）、`tests/test_deploy_result_semantics.py`（追加语义回归）

**Interfaces:**
- Consumes: `dbm.upsert_host_state`（Task 1）
- Produces:
  - `Deployer.target_modified`（`container_modified` 作为兼容别名）
  - `DeployStrategy.rollback_source_missing(server) -> bool`（默认 `False`）
  - 发布成功 / 自动回滚成功后写 `service_host_state`

- [ ] **Step 1: 写失败测试（追加到 tests/test_jar_service.py）**

```python
class TestRollback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = make_jar_deployer(self.tmp, previous_version="1.0.30")
        self.s = self.d.strategy

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relinks_previous_version_and_restarts(self):
        server = FakeRemote(try_exec_ok={"test -d": True})
        self.s.rollback(server)
        joined = "\n".join(server.exec_calls)
        self.assertIn(f"ln -sfn {APP_HOME}/releases/1.0.30 {APP_HOME}/.CURRENT.tmp", joined)
        self.assertIn("systemctl restart", joined)
        self.assertIn(f"echo 1.0.30 > {APP_HOME}/.deploy_version", joined)

    def test_missing_previous_release_skips_without_touching(self):
        server = FakeRemote(try_exec_ok={"test -d": False})
        with mock.patch("deploy_strategies.jar_service.log"):
            self.s.rollback(server)
        self.assertNotIn("systemctl", "\n".join(server.exec_calls))

    def test_without_previous_version_skips(self):
        self.d.previous_version = None
        server = FakeRemote()
        self.s.rollback(server)
        self.assertEqual(server.exec_calls, [])

    def test_source_missing_probe(self):
        self.assertTrue(self.s.rollback_source_missing(
            FakeRemote(try_exec_ok={"test -d": False})))
        self.assertFalse(self.s.rollback_source_missing(
            FakeRemote(try_exec_ok={"test -d": True})))


class TestNacosOrchestration(unittest.TestCase):
    """jar_service 发布必须与容器一样经过 Nacos 下线/上线（阶段方法打桩，只验编排）"""

    def test_jar_publish_offlines_then_onlines(self):
        d = make_jar_deployer(tempfile.mkdtemp())
        d.nacos.configured = True
        d.nacos.group = "DEFAULT_GROUP"
        d.proj_cfg["nacos_groups"] = ["DEFAULT_GROUP"]
        s = d.strategy
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=FakeRemote()), \
                mock.patch.object(deploy_mod.Deployer, "nacos_offline") as off, \
                mock.patch.object(deploy_mod.Deployer, "nacos_online") as on, \
                mock.patch.object(d, "_record_host_state"), \
                mock.patch.object(s, "check"), mock.patch.object(s, "prepare"), \
                mock.patch.object(s, "transfer"), mock.patch.object(s, "switch"), \
                mock.patch.object(s, "verify"):
            ok = d.deploy_server("10.0.0.1", {"ip": "10.0.0.1"})
        self.assertTrue(ok)
        off.assert_called_once_with("10.0.0.1")
        on.assert_called_once_with("10.0.0.1", weight=1.0)


class HostStateWritebackTest(unittest.TestCase):
    """发布成功后回写每主机版本：显式 project_id 时不依赖任务记录"""

    def test_writes_current_and_previous_version(self):
        from webapp import db as dbm
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db_path = os.path.join(tmp, "t.db")
        dbm.init_db(db_path)
        pid = dbm.create_record(db_path, "projects", dict(
            name="平台", code="plat", description="", git_org="", created_at=1))
        d = make_jar_deployer(tempfile.mkdtemp())
        d.project_id = pid
        with mock.patch.object(deploy_mod, "platform_db_path", return_value=db_path):
            d._record_host_state("10.0.0.1", "1.0.31", "1.0.30")
        rows = dbm.list_host_state(db_path, pid, "test", "auth")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_version"], "1.0.31")
        self.assertEqual(rows[0]["previous_version"], "1.0.30")
```

追加到 `tests/test_deploy_result_semantics.py`：

```python
class TargetModifiedAliasTest(unittest.TestCase):
    def test_alias_is_bidirectional(self):
        from deploy import Deployer
        d = Deployer.__new__(Deployer)
        d.target_modified = True
        self.assertTrue(d.container_modified)
        d.container_modified = False
        self.assertFalse(d.target_modified)

    def test_default_false_for_bare_instance(self):
        from deploy import Deployer
        d = Deployer.__new__(Deployer)
        self.assertFalse(d.container_modified)
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_jar_service.py tests/test_deploy_result_semantics.py -q`
Expected: FAIL（`rollback` 未实现；`target_modified` 属性不存在）

- [ ] **Step 3: 实现**

`base.py` 在 `rollback()` 之后加：

```python
    def rollback_source_missing(self, server) -> bool:
        """回滚源预检：True = 该主机没有目标版本产物，显式回滚应直接判失败

        默认 False（容器/静态由各自 rollback 内部处理缺源）；进程模式产物是本机目录，
        缺目录时「跳过回滚但任务成功」会误导操作者，因此在编排层提前拦截。
        """
        return False
```

`jar_service.py` 追加：

```python
    def rollback(self, server):
        """链接指回 previous_version 并重启；本机没有该版本目录时告警跳过"""
        ver = self.d.previous_version
        if not ver:
            log(f"[{server.host}] 未指定回滚版本（--previous-version），跳过回滚，服务保持现状")
            return
        target = f"{self.jars_dir}/{ver}"
        if not server.try_exec(f"test -d {shlex.quote(target)}", timeout=15):
            log(f"[{server.host}] 无回滚源（{target} 不存在），跳过回滚")
            return
        tmp = f"{self.app_home}/.CURRENT.tmp"
        self.d.target_modified = True
        server.exec(f"systemctl stop {shlex.quote(self.unit)}.service || true",
                    timeout=self.cfg["stop_timeout"] + 30)
        server.exec(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(tmp)} && "
            f"mv -Tf {shlex.quote(tmp)} {shlex.quote(self.curr_link)}",
            timeout=60)
        server.exec(f"systemctl restart {shlex.quote(self.unit)}.service",
                    timeout=self.cfg["start_timeout"] + 30)
        server.exec(
            f"echo {shlex.quote(ver)} > {shlex.quote(self.app_home + '/.deploy_version')}",
            timeout=15)
        log(f"[{server.host}] 已回滚 CURRENT -> releases/{ver}")

    def rollback_source_missing(self, server) -> bool:
        ver = self.d.previous_version
        if not ver:
            return False  # 无目标版本属于「跳过回滚」，由 rollback 自己说明
        return not server.try_exec(
            f"test -d {shlex.quote(self.jars_dir + '/' + ver)}", timeout=15)
```

`deploy.py` 在 `Deployer.__init__` 之前（类体内）加属性：

```python
class Deployer:
    ...
    @property
    def target_modified(self):
        """目标状态是否已被修改：失败时编排层据此决定是否回滚

        原字段名 container_modified 在 Jar/静态场景语义误导，统一为 target_modified；
        container_modified 保留为读写别名，既有策略与测试无需同步改名。
        """
        return getattr(self, "_target_modified", False)

    @target_modified.setter
    def target_modified(self, value):
        self._target_modified = bool(value)

    @property
    def container_modified(self):
        return self.target_modified

    @container_modified.setter
    def container_modified(self, value):
        self.target_modified = value
```

把 `deploy.py` 内其余 6 处 `self.container_modified` 读写改为 `self.target_modified`
（666 的置位，888 的初始化，908/914/926/928 的判定）。策略层同步改名：
`deploy_strategies/static_archive.py:207` 与 `deploy_strategies/jar_service.py` 的置位改成
`self.d.target_modified = True`（Task 7 的 jar `switch` 为保持中间提交行为一致暂用旧名，
本任务一并切换）；`deploy_strategies/base.py` 的类/方法注释同步更新为新名字。

`deploy_server` 成功返回前加：

```python
            self._record_host_state(host, self.version, self.previous_version)
```

新增方法：

```python
    def _record_host_state(self, host: str, current: str, previous: str = ""):
        """把每主机当前版本写入缓存表；失败只告警，绝不影响发布终态

        注意 self.project 是「服务名」（projects.yaml 的 key 即服务名），不是项目 code。
        项目 ID 优先取 CLI 的 --project-id；流水线在进程内直接 new Deployer，不会走
        deploy.py main()，此时用平台注入的 PLATFORM_TASK_ID 回查任务记录拿 project_id。
        两者都没有（手工 yaml 模式）时静默跳过。
        """
        task_id = int(os.environ.get("PLATFORM_TASK_ID") or 0)
        project_id = int(getattr(self, "project_id", 0) or 0)
        try:
            from webapp import db as dbm
            db_path = str(platform_db_path())
            if not project_id and task_id:
                task = dbm.get_record(db_path, "tasks", task_id)
                project_id = int((task or {}).get("project_id") or 0)
            if not project_id:
                return
            dbm.upsert_host_state(
                db_path, project_id, self.env or "",
                self.project, host, current, previous or "", task_id)
        except Exception as e:
            log(f"[{host}] 主机版本状态回写失败（不影响发布）: {e}")
```

`deploy.py` 的 `main()` 在 `deployer = Deployer(...)` 之后补一行（db 模式下 `args.project_id`
已在导出运行时配置时确定；yaml 模式为 None）：

```python
    deployer.project_id = args.project_id or 0
```

在显式回滚循环（`rollback_all` 内的 `_rollback_one`，约第 1138-1147 行）取 server 后、
Nacos 下线前加：

```python
                if self.strategy.rollback_source_missing(server):
                    log(f"[{host}] 回滚源缺失（releases/{self.previous_version} 不存在），"
                        f"该主机回滚失败；请先重传该版本产物或显式跳过此主机")
                    return host, False
```

并在回滚成功分支调用 `self._record_host_state(host, self.previous_version, self.version)`。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_jar_service.py tests/test_deploy_result_semantics.py tests/test_static_archive.py -q`
Expected: 全部通过（静态策略行为不变）

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/base.py deploy_strategies/jar_service.py deploy.py tests/test_jar_service.py tests/test_deploy_result_semantics.py
git commit -m "feat(deploy): jar_service 回滚与 target_modified 语义统一，回写每主机版本状态"
```

---

### Task 9: 流水线 --auto-version 按策略分发

**Files:**
- Modify: `deploy_pipeline.py:122-138`
- Test: `tests/test_deploy_pipeline.py`（追加）

**Interfaces:**
- Consumes: `Deployer.strategy.query_current_version()` / `next_version()`（Task 5）
- Produces: 混合流水线（container + jar_service）各自使用正确的版本语义

- [ ] **Step 1: 写失败测试（追加到 tests/test_deploy_pipeline.py）**

```python
class AutoFillVersionsStrategyTest(unittest.TestCase):
    def test_mixed_pipeline_uses_each_strategy_semantics(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [
            {"services": [{"name": "web-api"}, {"name": "creator-web"}]},
        ]}
        probes = {
            "web-api": mock.Mock(current="1.0.29", next="1.0.30"),
            "creator-web": mock.Mock(current="20261012", next="20261013"),
        }

        def fake_deployer(name, *a, **kw):
            d = mock.Mock()
            d.strategy.query_current_version.return_value = probes[name].current
            d.strategy.next_version.side_effect = lambda cur, n=name: probes[n].next
            return d

        with mock.patch.object(dp, "Deployer", side_effect=fake_deployer), \
                mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")

        svcs = {s["name"]: s for s in pipeline["stages"][0]["services"]}
        self.assertEqual(svcs["web-api"]["version"], "1.0.30")
        self.assertEqual(svcs["web-api"]["previous_version"], "1.0.29")
        self.assertEqual(svcs["creator-web"]["version"], "20261013")
        self.assertEqual(svcs["creator-web"]["previous_version"], "20261012")

    def test_explicit_service_version_wins(self):
        import deploy_pipeline as dp
        pipeline = {"stages": [{"services": [
            {"name": "svc", "version": "9.9.9", "previous_version": "9.9.8"}]}]}
        with mock.patch.object(dp, "Deployer") as dep, mock.patch.object(dp, "log"):
            dp.auto_fill_versions(pipeline, {}, env="test")
        dep.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_deploy_pipeline.py -q`
Expected: FAIL（仍调用 `get_current_remote_version`，mock 的 Deployer 未被使用）

- [ ] **Step 3: 实现**

`deploy_pipeline.py` 第 34 行导入改为（去掉不再使用的两个函数）：

```python
    Deployer, platform_db_path, runtime_config_dir,
```

`auto_fill_versions` 循环体替换为：

```python
    for svc in all_pipeline_services(pipeline_cfg):
        name = svc["name"]
        if svc.get("version") or svc.get("previous_version"):
            continue  # 服务级显式版本优先
        # 版本语义下沉策略层：容器=semver patch，静态=日期号，Jar=semver；
        # 流水线混布多种发布方式时不能统一按 docker ps 推断
        probe = Deployer(name, None, None, config, env=env)
        current = probe.strategy.query_current_version()
        if current is None:
            fail(f"[{name}] 服务器上未发现运行中的容器/已发布产物（首次发版），"
                 f"请在 pipelines.yaml 中为该服务显式配置 version")
        svc["version"] = probe.strategy.next_version(current)
        svc["previous_version"] = current
        log(f"[{name}] 自动版本: 当前线上 {current} → 新版本 {svc['version']}")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_deploy_pipeline.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add deploy_pipeline.py tests/test_deploy_pipeline.py
git commit -m "fix(pipeline): --auto-version 按发布策略分发，兼容混合运行时流水线"
```

---

### Task 10: 重启任务支持 systemd 单元

**Files:**
- Modify: `webapp/task_manager.py:340-401`
- Test: `tests/test_restart_task.py`（追加）

**Interfaces:**
- Consumes: `parse_runtime_config`（Task 2）、`monitor_mod._ssh_exec_multi`
- Produces: `restart` 任务对 `deploy_mode=jar_service` 执行 `systemctl restart` 而非 `docker restart`

- [ ] **Step 1: 写失败测试（追加到 tests/test_restart_task.py）**

```python
class JarRestartTest(unittest.TestCase):
    """重启任务按 deploy_mode 分流：进程服务走 systemctl，不触 docker"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = dbm.create_record(self.db, "projects", dict(
            name="p1", code="p1", description="", git_org="", created_at=1))
        self.svc_id = dbm.create_record(self.db, "services", dict(
            project_id=self.proj_id, name="auth", image_name="auth",
            service_name="", deploy_mode="jar_service",
            runtime_config={"app_home": "/opt/auth", "unit_name": "hqb-auth",
                            "readiness": {"type": "process"}},
            health_port=0, health_url="", enabled=1))
        self.env_id = _make_env(self.db, self.proj_id)
        sid = dbm.create_record(self.db, "servers", dict(
            host="10.0.0.1", ssh_port=22, ssh_user="", ssh_password="",
            nacos_ip="", remark="", enabled=1))
        dbm.create_record(self.db, "env_servers", dict(
            env_id=self.env_id, server_id=sid, order_no=0))
        self.log_file = os.path.join(self.tmp, "task_1.log")
        from webapp.task_manager import _RestartHandle
        self.handle = _RestartHandle()
        self.task = dict(id=1, project_id=self.proj_id, ref_id=self.svc_id,
                         env="prod", type="restart")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_job_uses_systemctl(self):
        from webapp.task_manager import _run_restart
        calls = []

        def fake_multi(host, port, ssh_cfg, cmds, timeout=10):
            calls.append((host, list(cmds)))
            return ["active\n"] * len(cmds)

        with mock.patch("webapp.monitor._ssh_exec_multi", fake_multi):
            status = _run_restart(self.db, self.task, self.log_file, self.handle)
        self.assertEqual(status, "success")
        joined = "\n".join(c for _, cmds in calls for c in cmds)
        self.assertIn("systemctl restart hqb-auth", joined)
        self.assertNotIn("docker restart", joined)
        with open(self.log_file, encoding="utf-8") as f:
            self.assertIn("单元 hqb-auth", f.read())
```

> 文件顶部补 `import shutil`（现有用例未用）。复用文件内已有的 `_make_env` 夹具；`_run_restart`
> 内部以 `from . import monitor as monitor_mod` 取模块，patch 目标只能是
> `webapp.monitor._ssh_exec_multi`（模块对象上的属性），不要写 `tm.monitor_mod`。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_restart_task.py -q`
Expected: FAIL（命令里是 `docker restart`）

- [ ] **Step 3: 实现**

在 `_run_restart` 取到 `rec` 之后、校验容器名之前加分支：

```python
        deploy_mode = rec.get("deploy_mode") or "container"
        if deploy_mode == "jar_service":
            from deploy_strategies.jar_config import parse_runtime_config
            cfg = parse_runtime_config(rec.get("runtime_config"))
            if not cfg["app_home"]:
                f.write("[platform] jar_service 未配置 runtime_config.app_home，重启中止\n")
                return "failed"
            unit = cfg["unit_name"] or rec.get("image_name") or rec["name"]
            svc_name = ""
        else:
            unit = ""
            svc_name = rec.get("service_name") or ""
```

把原先 `if not svc_name:` 的校验改为仅容器分支：

```python
        if deploy_mode != "jar_service" and not svc_name:
            f.write(f"[platform] 服务「{rec['name']}」未配置容器名（service_name），重启中止\n")
            return "failed"
```

`restart_one` 中替换拿到输出的那段（保留健康检查后半段）：

```python
            if deploy_mode == "jar_service":
                out = monitor_mod._ssh_exec_multi(host, port, ssh_cfg, [
                    f"systemctl restart {shlex.quote(unit)} 2>&1; "
                    f"systemctl is-active {shlex.quote(unit)}",
                ], timeout=60)[0].strip().splitlines()
                if not out or out[-1].strip() != "active":
                    return host, "failed", f"systemctl 重启失败: {' | '.join(out[-2:])}"
            else:
                outs = monitor_mod._ssh_exec_multi(host, port, ssh_cfg, [
                    "docker ps -a --format '{{.Names}}' | grep -Fx " + shlex.quote(svc_name),
                    "docker restart " + shlex.quote(svc_name),
                ], timeout=60)
                if not outs[0].strip():
                    return host, "skipped", "容器不存在，跳过"
                if outs[1].strip() != svc_name:
                    return host, "failed", f"docker restart 失败: {outs[1].strip() or '无输出'}"
```

日志首行文案加运行时方式区分：

```python
        target_desc = (f"单元 {unit}" if deploy_mode == "jar_service"
                       else f"容器名 {svc_name}")
        f.write(f"[platform] 重启服务「{rec['name']}」（{target_desc}），"
                f"环境「{task['env']}」，共 {len(servers)} 台服务器\n")
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_restart_task.py -q`
Expected: 全部通过

- [ ] **Step 5: 提交**

```bash
git add webapp/task_manager.py tests/test_restart_task.py
git commit -m "feat(restart): 重启任务按运行时方式分流，jar_service 走 systemctl"
```

---

### Task 11: systemd 单元生成与接入（管理员确认 + 审计）

**Files:**
- Create: `service_unit.py`
- Modify: `webapp/routes/config_routes.py`（文件末尾新增两个接口）
- Test: `tests/test_service_unit.py`
- Test: `tests/test_config_routes_jar.py`（新建，路由级契约）

**Interfaces:**
- Consumes: `parse_runtime_config`、`deploy.RemoteServer`
- Produces:
  - `render_unit(runtime_config, service: str) -> str`
  - `onboard_unit(server, unit: str, content: str, app_home: str) -> None`
  - `GET /api/projects/<pid>/services/<sid>/unit` → `{unit_name, content}`
  - `POST /api/projects/<pid>/services/<sid>/unit`（admin）→ `{hosts: [...], failed: [...]}`，写审计

- [ ] **Step 1: 写失败测试**

```python
# tests/test_service_unit.py
"""systemd 单元生成与接入：只写 unit + daemon-reload + enable"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import service_unit


class FakeRemote:
    def __init__(self, rules=None, try_exec_ok=None):
        self.exec_calls = []
        self.rules = rules or {}
        self.try_exec_ok = try_exec_ok or {}

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.exec_calls.append(cmd)
        for key, val in self.rules.items():
            if key in cmd:
                if isinstance(val, Exception):
                    raise val
                return val
        return ""

    def try_exec(self, cmd, timeout=60):
        self.exec_calls.append(cmd)
        return self.try_exec_ok.get("systemctl show", True)


CFG = {
    "app_home": "/opt/auth", "java_bin": "/usr/bin/java",
    "jvm_opts": "-Xms512m -Xmx1g", "app_args": "--spring.profiles.active=prod",
    "run_user": "appuser", "work_dir": "", "unit_name": "hqb-auth",
    "stop_timeout": 45, "start_timeout": 180,
    "env_files": ["/opt/auth/.env"],
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
    "port": 8080, "artifact_pattern": "target/*.jar",
}


class RenderTest(unittest.TestCase):
    def test_core_directives(self):
        content = service_unit.render_unit(CFG, "hqb-auth")
        self.assertIn("[Unit]", content)
        self.assertIn("User=appuser", content)
        self.assertIn("WorkingDirectory=/opt/auth/CURRENT", content)
        self.assertIn(
            "ExecStart=/usr/bin/java -Xms512m -Xmx1g -jar /opt/auth/CURRENT/app.jar "
            "--spring.profiles.active=prod", content)
        self.assertIn("SuccessExitStatus=143", content)
        self.assertIn("TimeoutStopSec=45", content)
        self.assertIn("TimeoutStartSec=180", content)
        self.assertIn("EnvironmentFile=/opt/auth/.env", content)
        self.assertIn("WantedBy=multi-user.target", content)

    def test_custom_work_dir(self):
        cfg = dict(CFG, work_dir="/srv/work")
        self.assertIn("WorkingDirectory=/srv/work", service_unit.render_unit(cfg, "a"))

    def test_no_run_user_means_no_directive(self):
        cfg = dict(CFG, run_user="")
        self.assertNotIn("User=", service_unit.render_unit(cfg, "a"))


class OnboardTest(unittest.TestCase):
    def test_writes_reloads_and_enables_only(self):
        server = FakeRemote(try_exec_ok={"systemctl show": True})
        with mock.patch("service_unit.log"):
            service_unit.onboard_unit(server, "hqb-auth", "[Unit]\n", "/opt/auth")
        joined = "\n".join(server.exec_calls)
        self.assertIn("/etc/systemd/system/hqb-auth.service", joined)
        self.assertIn("systemctl daemon-reload", joined)
        self.assertIn("systemctl enable hqb-auth.service", joined)
        self.assertNotIn("systemctl start", joined)
        self.assertNotIn("apt", joined)
        self.assertNotIn("java -version", joined)

    def test_refuses_foreign_existing_unit(self):
        server = FakeRemote(rules={
            "systemctl show": "ExecStart={ path=/usr/bin/java ; argv[]=java -jar /srv/x.jar }"})
        with mock.patch("service_unit.log"):
            with self.assertRaises(RuntimeError) as ctx:
                service_unit.onboard_unit(server, "hqb-auth", "[Unit]\n", "/opt/auth")
        self.assertIn("拒绝接管", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
```

第二个测试文件（路由契约，覆盖后端校验与 `/unit` 两个接口）：

```python
# tests/test_config_routes_jar.py
"""服务配置路由对 jar_service 的校验与单元预览接口"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="jar_routes_")

from webapp import create_app
from webapp import db as dbm


class JarConfigRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(data_dir=Path(_TMP))
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.get_data(as_text=True)
        cls.h = {"Authorization": f"Bearer {cls._login(cls.c, 'admin', 'test1234')}"}
        r = cls.c.post("/api/projects", json={"name": "jar-test", "code": "jartest"},
                       headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.pid = r.get_json()["id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _create(self, **kw):
        body = dict(name="auth", build_type="maven", deploy_mode="jar_service",
                    image_name="", service_name="",
                    runtime_config={"app_home": "/opt/auth", "port": 8080})
        body.update(kw)
        return self.c.post(f"/api/projects/{self.pid}/services",
                           json=body, headers=self.h)

    def test_unknown_deploy_mode_rejected(self):
        r = self._create(deploy_mode="k8s")
        self.assertEqual(r.status_code, 400)
        self.assertIn("deploy_mode", r.get_json()["error"])

    def test_jar_requires_app_home(self):
        r = self._create(runtime_config={"port": 8080})
        self.assertEqual(r.status_code, 400)
        self.assertIn("app_home", r.get_json()["error"])

    def test_jar_created_without_image_required(self):
        r = self._create(name="auth2")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        svcs = self.c.get(f"/api/projects/{self.pid}/services",
                          headers=self.h).get_json()
        rec = next(x for x in svcs if x["name"] == "auth2")
        # runtime_config 必须按 JSON 对象往返（写库序列化 / 读取反序列化）
        self.assertEqual(rec["runtime_config"]["app_home"], "/opt/auth")

    def test_unit_preview_returns_content(self):
        r = self._create(name="auth3")
        sid = r.get_json()["id"]
        r = self.c.get(f"/api/projects/{self.pid}/services/{sid}/unit",
                       headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["unit_name"], "auth3")  # 未配置 unit_name 时回退服务名
        self.assertIn("CURRENT/app.jar", data["content"])

    def test_unit_onboard_writes_audit(self):
        """管理员逐次接入：调用 unit 写入并对每个主机留审计（SSH 全桩）"""
        r = self._create(name="auth4")
        sid = r.get_json()["id"]
        r = self.c.post(f"/api/projects/{self.pid}/environments",
                        json={"name": "prod"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        # 新建服务器会自动绑定到已启用监控的环境，无需再手工 bind
        r = self.c.post("/api/servers", json={
            "host": "10.0.0.9", "ssh_port": 22, "ssh_user": "root"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        with mock.patch("deploy.RemoteServer"), \
                mock.patch("service_unit.onboard_unit") as onboard:
            r = self.c.post(f"/api/projects/{self.pid}/services/{sid}/unit",
                            json={"env": "prod"}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["hosts"], ["10.0.0.9"])
        onboard.assert_called_once()
        logs = dbm.list_audit_logs(self.app.config["DB_PATH"], limit=10)
        self.assertTrue(any(x["action"] == "onboard_service_unit" for x in logs))


if __name__ == "__main__":
    unittest.main()
```

> 响应结构已按实际代码核对：`_ok(data)` 原样返回 `data`（无 `{"data": ...}` 包装），
> `_err(msg)` 返回 `{"error": msg}`；配置蓝图统一挂在 `/api` 前缀下（`webapp/__init__.py:124`）。

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_service_unit.py tests/test_config_routes_jar.py -q`
Expected: `test_service_unit.py` 收集失败（模块不存在）；`test_config_routes_jar.py` 3 passed + 2 failed（路由级校验在 Task 2 已落地，这里固化为回归用例；`/unit` 两个接口 404）

- [ ] **Step 3: 实现**

```python
# service_unit.py
"""systemd 单元生成与接入

平台对目标机的系统写入被刻意限制为三件事：写 unit 文件、daemon-reload、enable。
不安装 JDK、不改内核参数、不动防火墙；对已存在且 ExecStart 不指向
{app_home}/CURRENT/app.jar 的单元一律拒绝接管。
"""
import base64
import shlex
import sys
from pathlib import Path

# 仓库根模块（与 deploy.py 同级）需自行把 scripts/ 加入搜索路径：
# 不能依赖「先 import 了 deploy_strategies 才顺带生效」这种隐式顺序
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from deploy_strategies.jar_config import parse_runtime_config
from common import log


def render_unit(runtime_config, service: str) -> str:
    """按运行时配置渲染 unit：ExecStart 固定引用 CURRENT，发布只换链接不改 unit"""
    cfg = parse_runtime_config(runtime_config)
    work_dir = cfg["work_dir"] or f"{cfg['app_home']}/CURRENT"
    exec_start = " ".join(x for x in (
        cfg["java_bin"], cfg["jvm_opts"],
        "-jar", f"{cfg['app_home']}/CURRENT/app.jar", cfg["app_args"]) if x)
    lines = [
        "[Unit]",
        f"Description={service}",
        "After=network.target",
        "",
        "[Service]",
        "Type=simple",
        f"User={cfg['run_user']}" if cfg["run_user"] else "",
        f"WorkingDirectory={work_dir}",
        f"ExecStart={exec_start}",
        # Java 收到 SIGTERM 正常退出码 143：不声明会被 systemd 判为失败并触发重启
        "SuccessExitStatus=143",
        "Restart=on-failure",
        "RestartSec=5",
        f"TimeoutStopSec={cfg['stop_timeout']}",
        f"TimeoutStartSec={cfg['start_timeout']}",
        *[f"EnvironmentFile={p}" for p in cfg["env_files"]],
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(line for line in lines if line)


def onboard_unit(server, unit: str, content: str, app_home: str) -> None:
    """在单台服务器接入 unit（幂等）：拒绝接管、写文件、reload、enable"""
    expected = f"{app_home.rstrip('/')}/CURRENT/app.jar"
    existing = server.exec(
        f"systemctl show -p ExecStart --value {shlex.quote(unit)}.service "
        f"2>/dev/null || true", timeout=15)
    if existing.strip() and expected not in existing:
        raise RuntimeError(
            f"拒绝接管非平台创建的单元 {unit}.service（ExecStart 未指向 {expected}）")
    path = f"/etc/systemd/system/{unit}.service"
    # base64 传输避免 heredoc/引号在多行 unit 内容中的转义问题
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    server.exec(
        f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)} && "
        f"systemctl daemon-reload && systemctl enable {shlex.quote(unit)}.service",
        timeout=60)
    log(f"[{server.host}] 单元已接入: {unit}.service")
```

`config_routes.py` 末尾新增（注意 `@require_auth` / `@require_role("admin")` 与文件内既有写法一致）：

```python
def _jar_service_or_404(pid, sid):
    from deploy_strategies.jar_config import validate_runtime_config
    rec, err = _owned_or_404("services", sid, pid)
    if err:
        return None, err
    if (rec.get("deploy_mode") or "container") != "jar_service":
        return None, _err("该服务不是 jar_service 发布方式")
    errs = validate_runtime_config(rec.get("runtime_config"), "jar_service")
    if errs:
        return None, _err("；".join(errs))
    return rec, None


def _unit_target(rec) -> tuple:
    from deploy_strategies.jar_config import parse_runtime_config
    import service_unit
    cfg = parse_runtime_config(rec.get("runtime_config"))
    unit = cfg["unit_name"] or rec.get("image_name") or rec["name"]
    return unit, service_unit.render_unit(cfg, unit)


@config_bp.route("/projects/<int:pid>/services/<int:sid>/unit", methods=["GET"])
@require_auth
def preview_service_unit(pid, sid):
    rec, err = _jar_service_or_404(pid, sid)
    if err:
        return err
    unit, content = _unit_target(rec)
    return _ok({"unit_name": unit, "content": content})


@config_bp.route("/projects/<int:pid>/services/<int:sid>/unit", methods=["POST"])
@require_auth
@require_role("admin")
def onboard_service_unit(pid, sid):
    """逐次确认后接入 unit：只写 unit/reload/enable，不启动、不装依赖"""
    import service_unit
    import deploy as deploy_mod
    from deploy_strategies.jar_config import parse_runtime_config
    rec, err = _jar_service_or_404(pid, sid)
    if err:
        return err
    env_name = (_json_body() or {}).get("env") or ""
    db_path = current_app.config["DB_PATH"]
    env_obj = (dbm.get_env_by_name(db_path, pid, env_name) if env_name
               else dbm.get_default_env(db_path, pid))
    if not env_obj:
        return _err("目标环境不存在")
    servers = dbm.list_env_servers(db_path, env_obj["id"])
    if not servers:
        return _err("环境未绑定服务器")
    cfg = parse_runtime_config(rec.get("runtime_config"))
    unit, content = _unit_target(rec)
    done, failed = [], []
    for srv in servers:
        ssh_cfg = deploy_mod.merge_server_ssh(env_obj.get("ssh") or {}, srv)
        server = deploy_mod.RemoteServer(srv["host"], ssh_cfg)
        try:
            service_unit.onboard_unit(server, unit, content, cfg["app_home"])
            done.append(srv["host"])
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    audit("onboard_service_unit", "service", sid,
          {"unit": unit, "env": env_obj["name"], "ok": done, "failed": failed})
    return _ok({"hosts": done, "failed": failed}) if not failed else \
        (_ok({"hosts": done, "failed": failed}), 207)
```

> `deploy.py` 顶部第 23 行已从 `common` 导入 `merge_server_ssh`，`deploy.merge_server_ssh`
> 可直接作为模块属性使用（无需再导入）。

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_service_unit.py tests/test_config_routes_jar.py -q`
Expected: 10 passed（单元 5 + 路由契约 5）

- [ ] **Step 5: 提交**

```bash
git add service_unit.py webapp/routes/config_routes.py tests/test_service_unit.py tests/test_config_routes_jar.py
git commit -m "feat(unit): systemd 单元生成、管理员逐次接入与审计"
```

---

### Task 12: 前端服务表单与页面文案

**Files:**
- Modify: `webapp/static/js/pages/config/config-service-form.js`
- Modify: `webapp/static/js/pages/devices.js:89-134`
- Test: `tests/js/config_service_form_visibility_test.js`（新建，三态字段显隐）

**Interfaces:**
- Consumes: Task 11 的 `GET/POST /api/projects/<pid>/services/<sid>/unit`、`deploy_mode=jar_service` 校验
- Produces: 表单可创建/编辑 `jar_service` 服务并保存 `runtime_config`；设备页显示 Java 进程徽标与最近发布版本

- [ ] **Step 1: 写失败测试（前端显隐）**

```javascript
// config-service-form.js 显隐回归：三种 deploy_mode 的字段集互斥，切换时回填 Maven
// 运行：node tests/js/config_service_form_visibility_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const noop = () => {};
const elements = {};
function el(id) {
  if (!elements[id]) {
    elements[id] = {
      id, innerHTML: '', value: '', checked: false, disabled: false,
      style: {}, textContent: '', onclick: null, onchange: null,
      classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      addEventListener: noop, removeEventListener: noop, focus: noop,
    };
  }
  return elements[id];
}

// 只记录字段元信息：显隐/必填的断言不依赖真实 DOM，也不复制表单实现
const fields = [];
const H = {
  field: o => {
    fields.push({ label: o.label, required: !!o.required });
    return `<div class="field" data-label="${o.label}">${o.input || ''}</div>`;
  },
  section: o => ({ html: `<section id="sec-${o.id}">${o.body}</section>`, bind: noop }),
  fieldErr: noop,
  cmdList: () => ({ html: '', bind: noop }),
  rowList: () => ({ html: '', bind: noop }),
  tagInput: () => ({ bind: noop }),
};

const ctx = {
  console, setTimeout, clearTimeout,
  document: {
    getElementById: el,
    querySelectorAll: () => [],
    createElement: () => el('__created' + Object.keys(elements).length),
  },
  window: {},
  openDrawer: noop, skeletonHTML: () => '', errorState: () => '',
  esc: v => String(v == null ? '' : v),
  api: async () => [],
  toastError: noop, toastSuccess: noop,
};
ctx.window.cfgHelpers = H;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(
  __dirname, '../../webapp/static/js/pages/config/config-service-form.js'), 'utf8'), ctx);

const failures = [];
function check(name, cond) {
  if (!cond) failures.push(name);
}

const BASE = {
  id: 1, name: 'hqb-auth', image_name: '', service_name: '', git_repo: '',
  build_type: 'maven', deploy_mode: 'container', build_order: [],
  nacos_groups: [], dubbo_services: [], log_error_keywords: ['ERROR'],
  runtime_config: { app_home: '/opt/hqb-auth', port: 8080 },
};
const requiredOf = label => {
  const f = fields.find(x => x.label === label);
  return f ? f.required : null;
};

async function openWith(mode, patch) {
  Object.keys(elements).forEach(k => delete elements[k]);
  fields.length = 0;
  ctx.window.cfgServiceForm.open(
    el('container'), { id: 7 },
    Object.assign({}, BASE, { deploy_mode: mode }, patch || {}), noop);
  await new Promise(r => setTimeout(r, 0));  // open() 内先 await api 才 render
  return el('dwBody').innerHTML;
}

(async () => {
  let html = await openWith('container');
  check('容器：显示 Docker 字段',
        html.includes('id="sDockerCtx"') && html.includes('id="sDockerfile"'));
  check('容器：不渲染 Java 字段', !html.includes('id="jAppHome"'));
  check('容器：镜像短名必填', requiredOf('镜像短名') === true);

  html = await openWith('static_archive');
  check('静态：显示站点根', html.includes('id="sSiteRoot"'));
  check('静态：隐藏 Docker 与 Java 字段',
        !html.includes('id="sDockerCtx"') && !html.includes('id="jAppHome"'));

  html = await openWith('jar_service');
  check('jar：显示 app_home / 端口 / 单元名',
        html.includes('id="jAppHome"') && html.includes('id="jPort"')
        && html.includes('id="jUnit"'));
  check('jar：隐藏 Docker 字段', !html.includes('id="sDockerCtx"'));
  check('jar：镜像短名不必填', requiredOf('镜像短名') === false);
  check('jar：保留构建分区', html.includes('id="sec-build"'));

  // 模式切换必须同时回填构建类型：从「前端服务复制来的配置」切 jar，不能沿用 frontend
  html = await openWith('container', { build_type: 'frontend' });
  el('sDeployMode').onchange({ target: { value: 'jar_service' } });
  html = el('dwBody').innerHTML;
  check('切换 jar：重渲染出 Java 字段', html.includes('id="jAppHome"'));
  check('切换 jar：构建类型回填 Maven',
        /<option value="maven" selected/.test(html));
  check('切换 jar：Docker 字段消失', !html.includes('id="sDockerCtx"'));

  if (failures.length) {
    console.error('FAIL');
    failures.forEach(f => console.error(' - ' + f));
    process.exit(1);
  }
  console.log('PASS config_service_form_visibility_test');
})();
```

- [ ] **Step 2: 运行确认失败**

Run: `node tests/js/config_service_form_visibility_test.js`
Expected: FAIL（`jar：显示 app_home / 端口 / 单元名` 等断言失败——当前表单只有容器与静态两种发布方式，`jAppHome` 不存在）

- [ ] **Step 3: 实现前端**

`config-service-form.js` 第 10-20 行默认数据补 `runtime_config`：

```javascript
        build_type:'maven', deploy_mode:'container',
        runtime_config:{app_home:'', java_bin:'/usr/bin/java', jvm_opts:'',
                        app_args:'', run_user:'', work_dir:'', unit_name:'',
                        port:'', stop_timeout:60, start_timeout:120,
                        artifact_pattern:'target/*.jar', env_files:[],
                        readiness:{type:'http', path:'/actuator/health',
                                   timeout:90, interval:5}},
```

`render()` 开头（第 39 行 `isStatic` 处）改为三态判断，并归一化 `runtime_config`：

```javascript
      const isStatic = (d.deploy_mode || 'container') === 'static_archive';
      const isJar = d.deploy_mode === 'jar_service';
      if (!d.runtime_config || typeof d.runtime_config !== 'object') d.runtime_config = {};
      const rc = d.runtime_config;
```

发布方式下拉改为三选一——选中态必须显式三选，不能沿用 `!isStatic`（否则 jar 会选中容器）：

```javascript
              <option value="container" ${!isStatic && !isJar ? 'selected' : ''}>🐳 容器（Docker 镜像 + compose）</option>
              <option value="static_archive" ${isStatic ? 'selected' : ''}>🗂️ 静态站点（zip → 1Panel/nginx 目录）</option>
              <option value="jar_service" ${isJar ? 'selected' : ''}>☕ 纯 Java 进程（jar → systemd）</option>
```

其下提示追加：`纯 Java 进程：Maven 构建出可执行 jar，按版本放入 app_home/releases 并切换 CURRENT 链接，由 systemd 托管；无容器、无镜像。单实例重启期间服务短暂不可用，不承诺零停机。`

同时调整既有字段的必填/显隐（不改容器与静态分支的现有行为）：

- 镜像短名、compose 服务名、Git 仓库的 `required`：`!isStatic` → `!isStatic && !isJar`
  （jar 产物可由 `dist/` 手工放入，不强制镜像/仓库）。
- Docker 上下文与 Dockerfile 的显示条件：`isStatic ? '' : ...` → `isStatic || isJar ? '' : ...`。
- 构建类型下拉 `disabled` 条件：`isStatic` → `isStatic || isJar`；Maven 选项 `selected`
  条件改为 `!isFrontend && !isStatic`，并把 `isFrontend` 定义改为
  `const isFrontend = isStatic || (!isJar && d.build_type === 'frontend');`
  （jar 用 `d.build_type` 可能因复制前端服务而是 `frontend`，必须强制回 Maven 分支）。

`②b 静态站点` 区块之后新增 `javaCfg`（仅 `isJar` 渲染）：

```javascript
      // ②c 纯 Java 进程专属配置（deploy_mode=jar_service）
      const javaCfg = isJar ? `
        <div class="form-grid">
          ${H.field({ label:'应用根目录（app_home）', required:true, input:`<input id="jAppHome" value="${esc(rc.app_home || '')}" placeholder="/opt/apps/hqb-auth">`, hint:'版本目录 releases/ 与 CURRENT 链接的根；回滚只换链接，不重传 jar' })}
          ${H.field({ label:'Java 路径（java_bin）', input:`<input id="jJavaBin" value="${esc(rc.java_bin || '/usr/bin/java')}">`, hint:'目标机已安装的 JDK/JRE 绝对路径，平台不负责安装' })}
          ${H.field({ label:'JVM 参数（jvm_opts）', input:`<input id="jJvmOpts" value="${esc(rc.jvm_opts || '')}" placeholder="-Xms512m -Xmx1g">` })}
          ${H.field({ label:'启动参数（app_args）', input:`<input id="jAppArgs" value="${esc(rc.app_args || '')}" placeholder="--spring.profiles.active=prod">` })}
          ${H.field({ label:'运行用户（run_user）', input:`<input id="jRunUser" value="${esc(rc.run_user || '')}" placeholder="appuser">`, hint:'留空则由 systemd 以 root 运行；平台不改目标机用户与权限' })}
          ${H.field({ label:'工作目录（work_dir）', input:`<input id="jWorkDir" value="${esc(rc.work_dir || '')}" placeholder="留空默认 {app_home}/CURRENT">` })}
          ${H.field({ label:'systemd 单元名（unit_name）', input:`<input id="jUnit" value="${esc(rc.unit_name || '')}" placeholder="留空默认同服务名">` })}
          ${H.field({ label:'端口（port）', input:`<input id="jPort" type="number" value="${esc(rc.port || '')}" placeholder="8080">`, hint:'HTTP/TCP 就绪方式必填；仅进程存活方式可留空' })}
          ${H.field({ label:'就绪方式（readiness.type）', input:`<select id="jReadinessType">
              <option value="http" ${(!rc.readiness || rc.readiness.type === 'http') ? 'selected' : ''}>HTTP 健康端点</option>
              <option value="tcp" ${rc.readiness?.type === 'tcp' ? 'selected' : ''}>TCP 端口</option>
              <option value="process" ${rc.readiness?.type === 'process' ? 'selected' : ''}>仅进程存活</option>
            </select>` })}
          ${H.field({ label:'健康路径（readiness.path）', input:`<input id="jReadinessPath" value="${esc(rc.readiness?.path || '/actuator/health')}">`, hint:'仅 HTTP 方式使用；要求响应体含 UP 语义（Actuator 约定）' })}
          ${H.field({ label:'就绪超时/间隔（秒）', input:`<input id="jReadinessTimeout" type="number" value="${esc((rc.readiness && rc.readiness.timeout) || 90)}" style="width:45%"> <input id="jReadinessInterval" type="number" value="${esc((rc.readiness && rc.readiness.interval) || 5)}" style="width:45%">`, hint:'前为总超时，后为轮询间隔' })}
          ${H.field({ label:'停止/启动超时（秒）', input:`<input id="jStopTimeout" type="number" value="${esc(rc.stop_timeout || 60)}" style="width:45%"> <input id="jStartTimeout" type="number" value="${esc(rc.start_timeout || 120)}" style="width:45%">`, hint:'写入 systemd TimeoutStopSec / TimeoutStartSec' })}
          ${H.field({ label:'可执行 jar 匹配（artifact_pattern）', input:`<input id="jArtifactPattern" value="${esc(rc.artifact_pattern || 'target/*.jar')}">`, hint:'命中多个时取体积最大者（fat jar 必然更大），并排除 sources/javadoc/original' })}
          ${H.field({ label:'环境变量文件（env_files）', input:`<textarea id="jEnvFiles" rows="2" placeholder="/opt/apps/hqb-auth/.env">${esc((rc.env_files || []).join('\n'))}</textarea>`, hint:'每行一个绝对路径，写入 systemd EnvironmentFile；文件缺失会导致单元启动失败' })}
          <div class="hint">单实例发布重启期间服务不可用，平台不承诺零停机。首次发布前请用下方「预览 systemd 单元」核对 ExecStart。</div>
          <button class="btn" id="jUnitPreview" type="button" ${d.id ? '' : 'disabled title="保存后可预览"'}>预览 systemd 单元</button>
          <pre id="jUnitContent" class="weak" style="display:none;white-space:pre-wrap;font-size:12px;margin:0"></pre>
        </div>` : '';
```

`groups` 数组改为（jar 同时保留注册中心区块，静态仍不显示注册中心）：

```javascript
      const groups = [
        H.section({ id:'basic', icon:'📝', title:'基本信息', desc:'发布方式 / 服务名 / 镜像 / 仓库关联', open:true, body: basic }),
        H.section({ id:'build', icon:'🔨', title:'构建配置', desc:isStatic ? '前端构建步骤与静态打包产出' : (isJar ? 'Maven 构建与可执行 jar 产物' : '构建顺序与 Docker 上下文'), open:!!(d.build_order.length || isStatic || isJar), body: build }),
        isStatic
          ? H.section({ id:'static', icon:'🗂️', title:'静态站点', desc:'1Panel 站点目录与 zip 产物约定', open:true, body: staticCfg })
          : (isJar
              ? H.section({ id:'jar', icon:'☕', title:'Java 进程', desc:'app_home / JVM / systemd 单元', open:true, body: javaCfg })
              : null),
        isStatic ? null : H.section({ id:'reg', icon:'🛰️', title:'注册中心与任务调度', desc:'Nacos 分组 / Dubbo / SnailJob', open:!!(d.nacos_groups.length || d.dubbo_services.length || d.snailjob_group), body: reg }),
        H.section({ id:'health', icon:'💚', title:'健康检查与日志', desc:isStatic ? '站点探活 URL（发布后 HTTP 验证）' : '探活端口 / URL / 启动失败判定', open:true, body: health }),
      ].filter(Boolean);
```

模式切换处理补 jar→Maven：

```javascript
        d.deploy_mode = e.target.value;
        if (d.deploy_mode === 'static_archive') d.build_type = 'frontend';
        if (d.deploy_mode === 'jar_service') d.build_type = 'maven';
        render();
```

`collect()` 内先在既有 `const isStatic = d.deploy_mode === 'static_archive';` 旁补一行
`const isJar = d.deploy_mode === 'jar_service';`，否则后续分支引用会抛 ReferenceError；
再把 `d.build_type` 赋值改为 `d.build_type = isJar ? 'maven' : (val('sBuildType') || 'maven');`。
校验分支改为三态：

```javascript
      const checks = isStatic
        ? [['sName', 'name', '服务名必填']]
        : isJar
          ? [['sName', 'name', '服务名必填']]
          : [['sName', 'name', '服务名必填'],
             ['sImage', 'image_name', '镜像短名必填'],
             ['sGit', 'git_repo', 'Git 仓库路径必填']];
```

紧接着在校验循环之后加 jar 字段读取与校验（必须在 `let ok = true` 之后）：

```javascript
      if (isJar) {
        const num = (id, dflt) => {
          const n = Number(val(id));
          return Number.isFinite(n) && n > 0 ? n : dflt;
        };
        const prev = d.runtime_config || {};
        d.runtime_config = {
          ...prev,
          app_home: val('jAppHome').trim(),
          java_bin: val('jJavaBin').trim() || '/usr/bin/java',
          jvm_opts: val('jJvmOpts').trim(),
          app_args: val('jAppArgs').trim(),
          run_user: val('jRunUser').trim(),
          work_dir: val('jWorkDir').trim(),
          unit_name: val('jUnit').trim(),
          port: Number(val('jPort') || 0),
          stop_timeout: num('jStopTimeout', 60),
          start_timeout: num('jStartTimeout', 120),
          artifact_pattern: val('jArtifactPattern').trim() || 'target/*.jar',
          env_files: val('jEnvFiles').split('\n').map(x => x.trim()).filter(Boolean),
          readiness: {
            ...(prev.readiness || {}),
            type: val('jReadinessType') || 'http',
            path: val('jReadinessPath').trim() || '/actuator/health',
            timeout: num('jReadinessTimeout', 90),
            interval: num('jReadinessInterval', 5),
          },
        };
        if (!d.runtime_config.app_home.startsWith('/')) {
          H.fieldErr('jAppHome', '应用根目录必须为绝对路径（如 /opt/apps/hqb-auth）'); ok = false;
        } else H.fieldErr('jAppHome', '');
        const rt = d.runtime_config.readiness.type;
        const p = d.runtime_config.port;
        if ((rt === 'http' || rt === 'tcp') && !(Number.isInteger(p) && p > 0 && p < 65536)) {
          H.fieldErr('jPort', 'HTTP/TCP 就绪方式必须填写 1-65535 的端口'); ok = false;
        } else H.fieldErr('jPort', '');
      }
```

提交体（`svcSave` 的 payload）加一行，避免把旧配置带给非 jar 服务：

```javascript
          runtime_config: d.deploy_mode === 'jar_service' ? d.runtime_config : {},
```

预览按钮绑定（放在 `svcSave` 绑定之后；复用闭包内的 `svc` / `project` / `d`，
不要发明全局收集函数；`api()` 返回的就是后端 JSON 本体）：

```javascript
      const previewBtn = document.getElementById('jUnitPreview');
      if (previewBtn) previewBtn.onclick = async () => {
        if (!svc) return toastError('请先保存服务，再预览单元');
        try {
          const res = await api(`/api/projects/${project.id}/services/${svc.id}/unit`);
          const pre = document.getElementById('jUnitContent');
          pre.style.display = 'block';
          pre.textContent = `# ${res.unit_name}.service\n\n${res.content}`;
        } catch (e) { toastError(e.message); }
      };
```

`webapp/static/js/pages/devices.js`：在 `const isStatic = ...` 旁补 `isJar`；卡片头部的静态徽标
改为三态：把卡片头部渲染「🗂️ 静态站点」的那一段换成下面的 `modeBadge`；实时日志按钮对 jar 隐藏
（journalctl 日志采集在 M5，先不暴露会报错的入口）：

```javascript
      const isStatic = (s.deploy_mode || 'container') === 'static_archive';
      const isJar = s.deploy_mode === 'jar_service';
      const modeBadge = isStatic ? '🗂️ 静态站点' : (isJar ? '☕ Java 进程' : '');
```

jar 的在线版本同样取最近一次成功发布任务的版本（监控中心只采集容器，`st.version` 对 jar
恒为空；M5 接入进程指标后再改为读写 `service_host_state` 的每主机视图）：

```javascript
      const lastOk = (isStatic || isJar) ? tasksCache.find(t =>
        t.name === s.name && t.env === state.currentEnv && t.status === 'success' && t.version) : null;
      const ver = (isStatic || isJar) ? ((lastOk && lastOk.version) || '—') : (st.version || '—');
```

jar 的 meta 不能复用容器分支（会出现「镜像 / 实例 / 健康」这些无意义字段）。
把卡片里的 `<div class="svc-meta"> … </div>` 整块替换为下面三段式（jar / 静态 / 容器原样保留）；
`services` 列表接口已把 `runtime_config` 还原为对象（Task 1 的 `JSON_FIELDS`），直接读即可：

```javascript
        <div class="svc-meta">
          ${isJar ? `
          <div><span class="weak">版本</span> <span class="num">${esc(ver)}</span>
               <span class="weak" style="margin-left:10px">状态</span> <span class="weak" style="font-size:11px">${run ? '发布中' : '未知'}</span></div>
          <div><span class="weak">应用根</span> <span class="num ellipsis" style="display:inline-block;max-width:78%;vertical-align:bottom" title="${esc((s.runtime_config || {}).app_home || '')}">${esc((s.runtime_config || {}).app_home || '未配置')}</span></div>
          <div><span class="weak">端口</span> <span class="num">${(s.runtime_config || {}).port || s.health_port || '-'}</span></div>` : isStatic ? `
          <div><span class="weak">版本</span> <span class="num">${esc(ver)}</span>
               <span class="weak" style="margin-left:10px">状态</span> ${svcLabel(st, run, isStatic)}</div>
          <div><span class="weak">站点根</span> <span class="num ellipsis" style="display:inline-block;max-width:78%;vertical-align:bottom" title="${esc(s.site_root || '')}">${esc(s.site_root || '未配置')}</span></div>
          <div><span class="weak">探活</span> ${s.health_url
            ? `<span class="num ellipsis" style="display:inline-block;max-width:78%;vertical-align:bottom" title="${esc(s.health_url)}">${esc(s.health_url)}</span>`
            : '<span class="weak" style="font-size:11px">未配置，无法探测在线状态</span>'}</div>
          ${ts ? `<div><span class="weak">探测</span> ${ts}</div>` : ''}` : `
          <div><span class="weak">镜像</span> <span class="num">${esc(ver)}</span></div>
          <div><span class="weak">实例</span> <span class="num">${inst}</span>
               <span class="weak" style="margin-left:10px">状态</span> ${svcLabel(st, run)}</div>
          <div><span class="weak">端口</span> <span class="num">${s.health_port || '-'}</span>
               ${ts ? `<span class="weak" style="margin-left:10px">探测</span> ${ts}` : ''}</div>
          <div><span class="weak">健康</span> ${hcTxt}</div>`}
        </div>
```

jar 在 M5 前没有在线状态来源：`st.status` 为空时 `dotClass` 自然回落为灰点，不要在此臆造「在线」；
状态列固定显示「发布中 / 未知」，M5 接入进程观测后再换成 `service_host_state` 的每主机视图。

```javascript
          ${modeBadge ? `<span class="weak" style="margin-left:${upTxt ? '8' : 'auto'};font-size:11px">${modeBadge}</span>` : ''}
```

```javascript
          ${isJar ? '' : `<button class="svc-log-btn" data-svc="${esc(s.name)}" data-static="${isStatic ? 1 : 0}" title="${isStatic ? '查看发布日志' : '查看实时日志'}">📋 日志</button>`}
```

- [ ] **Step 4: 运行确认通过**

Run: `node tests/js/config_service_form_visibility_test.js`
Run: `node --check webapp/static/js/pages/config/config-service-form.js`
Run: `node --check webapp/static/js/pages/devices.js`
Expected: `PASS config_service_form_visibility_test`（容器/静态/jar 字段集互斥、镜像必填随模式变化、切换回填 Maven）；两个 `node --check` 无输出

- [ ] **Step 5: 提交**

```bash
git add webapp/static/js/pages/config/config-service-form.js webapp/static/js/pages/devices.js tests/js/config_service_form_visibility_test.js
git commit -m "feat(ui): 服务表单支持 jar_service 配置与单元预览"
```

---

### Task 13: 全量回归、质量门禁与文档

**Files:**
- Modify: `README.md`（发布方式说明处）
- Modify: `docs/usage.md`（新增 jar_service 配置与发布说明）
- Test: 全量测试 + `scripts/quality_gate.py`

**Interfaces:**
- Consumes: 前 12 个任务的全部产物
- Produces: 可交付的首期能力与文档

- [ ] **Step 1: 跑全量测试**

Run: `python -m pytest -q`
Expected: 原 644 passed 的全部用例通过，叠加新增用例后总数增加且 0 failed。
若出现失败：先定位是否由本次改动引起（`git stash` 对照不是选项，直接看失败断言与最近改动文件的交集），修复后再继续。

- [ ] **Step 2: 跑质量门禁**

Run: `python scripts/quality_gate.py`
Expected: AST/语法检查通过；pytest 通过；JS 检查通过（含新增的 `config_service_form_visibility_test.js`，`JS_TESTS` 为 glob 自动收录）；ruff 若本机未安装则显示 SKIP（不得报失败）。
注意：本机目前未安装 ruff，SKIP 是既有状态，**不要**为了让它变绿而临时安装依赖。

- [ ] **Step 3: 文档**

在 `README.md` 的发布方式说明中补一段：

```markdown
### 运行方式（deploy_mode）

- `container`：Docker 容器（默认）
- `static_archive`：静态站点压缩包（1Panel/nginx）
- `jar_service`：纯 Java 可执行 jar + Linux systemd

`jar_service` 要求目标机已安装 JDK/JRE 并由平台接入 systemd 单元；版本目录
`{app_home}/releases/{version}/app.jar` 不可变，`CURRENT` 符号链接原子切换，
回滚只换链接不重传。单实例重启存在停机窗口，平台不承诺零停机。
```

在 `docs/usage.md` 增加「Java 进程发布」小节：配置字段表（`app_home`/`java_bin`/`jvm_opts`/`app_args`/`run_user`/`unit_name`/`port`/`readiness`/`stop_timeout`/`start_timeout`/`artifact_pattern`/`env_files`）、单元接入步骤（预览 → 管理员确认 → 审计）、发布与回滚行为、失败排查（journal 尾部已自动进任务日志）。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/usage.md
git commit -m "docs: jar_service 运行方式、单元接入与回滚行为说明"
```

- [ ] **Step 5: 交付前自检**

逐条核对设计文档第 9 节的验收标准，并在交付说明中给出证据（命令 + 关键输出）：

1. jar_service 全链路发布不产生 docker 调用 —— `tests/test_build_jar_artifact.py`、`tests/test_jar_service.py` 的命令断言。
2. 多版本并存与秒级回滚 —— `TestRollback.test_relinks_previous_version_and_restarts`。
3. 启动失败自动回滚 + journal 进日志 —— `TestVerifyPhase` + `test_deploy_result_semantics`。
4. 停机窗口文案 —— 表单与文档中的明确提示。
5. 未接入单元不可发布、接入需管理员且留审计 —— `TestCheckPhase.test_prepare_requires_installed_unit` + `test_service_unit.py` + 路由审计。
6. 前端三态显隐与切换回填 —— `node tests/js/config_service_form_visibility_test.js`（`tests/js/*_test.js` 会被质量门禁自动收录）。
7. 容器/静态行为不变 —— 全量 644 基线 + `tests/test_static_archive.py`。

---

## 后续计划（不在本计划内）

M5 观测与界面（`monitor.py` 进程指标、`log_collector.py` journal 采集、每主机版本视图）待本计划落地后单独编写实施计划；它们依赖本计划产出的 `service_host_state` 与 `runtime_config`，但可独立验收，不阻塞发布链路。
