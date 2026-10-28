# 进程族运行时扩展（Go/Rust 单二进制 + Node/Python 进程）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 jar_service 的实现泛化为进程族共享基类，新增 `go_binary` / `rust_binary` / `node_process` / `python_process` 四种发布方式，复用全部既有编排、观测与回滚机制，既有 container/static_archive/jar_service 行为零变化。

**Architecture:** `deploy_strategies/process_service.py` 提供 `ProcessServiceStrategy` 基类（五阶段 + file/archive 两种产物形态钩子），jar 改为薄子类，四个新模式各为薄子类 `@register`；观测放行通过 `base.process_modes()` 注册表自动收集；构建侧 `scripts/build.py` 每种运行时一个 build_type 分支，Node/Python 走 tar.gz 目录产物，Python 依赖用 wheelhouse + 目标机 venv。

**Tech Stack:** Python 3 stdlib（flask 已有）、原生 JS SPA、SQLite、systemd/journalctl（目标机侧）、go/cargo/npm/pip（平台机构建子进程，测试全桩）。

**Spec:** `docs/superpowers/specs/2026-10-28-process-runtime-deploy-design.md`（计划从 spec 论证，执行者需同读）

## Global Constraints

- 单实例发布重启必有中断窗口，文档与 UI 不得承诺零停机。
- 所有测试必须桩掉 SSH/SFTP/systemctl/journalctl/docker/go/cargo/npm/pip，禁止真实网络与真实服务器。
- 测试不得读写仓库 `data/`：一律 `tempfile.mkdtemp()`，Flask 用例用 `create_app(data_dir=...)` 注入。
- 不新增第三方依赖；`scripts/quality_gate.py` 中 ruff/node 缺失时保持 SKIP。
- 不执行 push；每个任务只做本地 commit（用户明确要求推送时才推送）。
- 权威版本状态在目标机（`.deploy_version` + `CURRENT`）；`service_host_state` 只是展示缓存。
- 日志服务键统一 `service_name or name`；journal 采集扫描键统一 `systemd:<unit>`，文件目录用 `<unit>`。
- 单元名统一由 `derive_unit_name()` 派生（本计划起从 `process_config` 导入），禁止再复制三元表达式。
- jar_service 行为零变化：既有测试套件是基类抽取的回归门，禁止为过测试改变 jar 的命令序列/日志文案语义。
- 模块文件名不改（`jar_probe.py`/`jar_hosts.js` 等历史命名保留）；只泛化触碰到的函数/变量/文案。
- Python 目标机前提：`python3` + `python3-venv`（含 ensurepip）；Node 目标机前提：`node`。平台不代装。

---

### Task 1: process_config 共享层与 process_modes

**Files:**
- Create: `deploy_strategies/process_config.py`
- Modify: `deploy_strategies/jar_config.py`
- Modify: `deploy_strategies/base.py`（`registered_modes` 之后）
- Test: `tests/test_process_config.py`（新建）

**Interfaces:**
- Produces:
  - `process_config.parse_runtime_config(raw, deploy_mode="jar_service") -> dict`（未知模式返回 `{}`；jar 惰性委托 `jar_config.parse_runtime_config`）
  - `process_config.validate_runtime_config(raw, deploy_mode) -> list`（未知模式返回 `[]`；jar 惰性委托）
  - `process_config.parse_common(cfg: dict, mode: str) -> dict`、`process_config.validate_common(cfg: dict, mode: str) -> list`
  - `process_config.build_exec_start(cfg: dict, deploy_mode: str) -> str`、`process_config.exec_start_marker(cfg: dict, deploy_mode: str) -> str`
  - `process_config.sha256_file(path) -> str`、`process_config.derive_unit_name(runtime_config, image_name="", service_name="") -> str`（自 jar_config 迁入）
  - `base.process_modes() -> list`（注册表自动收集 `is_process=True` 的策略名）
- Consumes: 现有 `jar_config` 的 `DEFAULTS`、`_UNIT_RE` 语义。
- 兼容契约：`jar_config.parse_runtime_config(raw)`（单参）与 `jar_config.validate_runtime_config(raw, deploy_mode)` 签名不变、行为不变；`derive_unit_name`/`sha256_file` 在 `jar_config` 保持可导入（re-export）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_process_config.py`：

```python
"""process_config 共享层：分派解析/校验、ExecStart 拼装与接管特征串、process_modes"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deploy_strategies import process_config as pc
from deploy_strategies.jar_config import parse_runtime_config as parse_jar


class ParseDispatchTest(unittest.TestCase):
    def test_go_minimal_defaults(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/goapp"}, "go_binary")
        self.assertEqual(cfg["app_home"], "/opt/goapp")
        self.assertEqual(cfg["readiness"]["type"], "process")
        self.assertEqual(cfg["readiness"]["path"], "")
        self.assertEqual(cfg["stop_timeout"], 60)
        self.assertEqual(cfg["start_timeout"], 120)

    def test_node_fields(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/napp", "start_file": "dist/server.js",
             "node_bin": "/usr/local/bin/node"}, "node_process")
        self.assertEqual(cfg["start_file"], "dist/server.js")
        self.assertEqual(cfg["node_bin"], "/usr/local/bin/node")

    def test_node_default_node_bin(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/napp"}, "node_process")
        self.assertEqual(cfg["node_bin"], "/usr/bin/node")

    def test_python_fields(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/papp", "start_file": "main.py",
             "python_version": "3.11"}, "python_process")
        self.assertEqual(cfg["requirements"], "requirements.txt")
        self.assertEqual(cfg["python_version"], "3.11")

    def test_jar_delegates_and_keeps_java_keys(self):
        raw = {"app_home": "/opt/a", "jvm_opts": "-Xmx1g"}
        cfg = pc.parse_runtime_config(raw, "jar_service")
        self.assertEqual(cfg["java_bin"], "/usr/bin/java")
        self.assertEqual(cfg, parse_jar(raw))

    def test_unknown_mode_returns_empty(self):
        self.assertEqual(pc.parse_runtime_config({"app_home": "/x"}, "container"), {})


class ValidateDispatchTest(unittest.TestCase):
    def test_go_ok(self):
        self.assertEqual(pc.validate_runtime_config(
            {"app_home": "/opt/goapp"}, "go_binary"), [])

    def test_app_home_required_per_mode(self):
        errs = pc.validate_runtime_config({}, "rust_binary")
        self.assertTrue(any("app_home" in e for e in errs))

    def test_start_file_rules(self):
        for bad in ("", "/abs/server.js", "../x.js", "a b.js"):
            errs = pc.validate_runtime_config(
                {"app_home": "/opt/n", "start_file": bad}, "node_process")
            self.assertTrue(any("start_file" in e for e in errs), bad)
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/n", "start_file": "dist/server.js"}, "node_process")
        self.assertEqual(errs, [])

    def test_python_version_format(self):
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/p", "start_file": "m.py", "python_version": "3"},
            "python_process")
        self.assertTrue(any("python_version" in e for e in errs))

    def test_jar_validation_still_enforced(self):
        errs = pc.validate_runtime_config({}, "jar_service")
        self.assertTrue(any("app_home" in e for e in errs))
        errs = pc.validate_runtime_config(
            {"app_home": "/opt/a", "java_bin": "java"}, "jar_service")
        self.assertTrue(any("java_bin" in e for e in errs))

    def test_non_process_mode_no_validation(self):
        self.assertEqual(pc.validate_runtime_config({}, "container"), [])


class ExecStartTest(unittest.TestCase):
    def test_jar(self):
        cfg = parse_jar({"app_home": "/opt/a", "java_bin": "/usr/bin/java",
                         "jvm_opts": "-Xmx1g", "app_args": "--p=1"})
        self.assertEqual(pc.build_exec_start(cfg, "jar_service"),
                         "/usr/bin/java -Xmx1g -jar /opt/a/CURRENT/app.jar --p=1")
        self.assertEqual(pc.exec_start_marker(cfg, "jar_service"),
                         "/opt/a/CURRENT/app.jar")

    def test_go_rust(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/g", "app_args": "--port=9"}, "go_binary")
        self.assertEqual(pc.build_exec_start(cfg, "go_binary"),
                         "/opt/g/CURRENT/app --port=9")
        self.assertEqual(pc.exec_start_marker(cfg, "rust_binary"),
                         "/opt/g/CURRENT/app")

    def test_node(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/n", "start_file": "dist/server.js"}, "node_process")
        self.assertEqual(pc.build_exec_start(cfg, "node_process"),
                         "/usr/bin/node /opt/n/CURRENT/dist/server.js")
        self.assertEqual(pc.exec_start_marker(cfg, "node_process"),
                         "/opt/n/CURRENT/dist/server.js")

    def test_python(self):
        cfg = pc.parse_runtime_config(
            {"app_home": "/opt/p", "start_file": "main.py"}, "python_process")
        self.assertEqual(pc.build_exec_start(cfg, "python_process"),
                         "/opt/p/venv/bin/python /opt/p/CURRENT/main.py")
        self.assertEqual(pc.exec_start_marker(cfg, "python_process"),
                         "/opt/p/venv/bin/python")

    def test_no_args(self):
        cfg = pc.parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        self.assertEqual(pc.build_exec_start(cfg, "go_binary"), "/opt/g/CURRENT/app")


class ProcessModesTest(unittest.TestCase):
    def test_registry_collects_process_family(self):
        from deploy_strategies.base import process_modes
        modes = process_modes()
        self.assertIn("jar_service", modes)
        self.assertNotIn("container", modes)
        self.assertNotIn("static_archive", modes)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_process_config.py -q`
Expected: FAIL（`ModuleNotFoundError: deploy_strategies.process_config`）。

- [ ] **Step 3: 实现 `deploy_strategies/process_config.py`**

```python
"""进程族（jar_service/go_binary/rust_binary/node_process/python_process）
运行时配置共享层：公共字段解析、按模式分派的解析/校验、ExecStart 拼装与接管特征串。

纯标准库实现，CLI 子进程（策略层/构建）与 Web 路由共用同一份规则——校验口径只允许
一个来源。jar 专属字段与缺省值留在 jar_config.py（公共函数签名不变），本模块按
deploy_mode 分派并惰性导入，避免 jar_config ↔ process_config 的模块级循环。
"""
import hashlib
import json
import re

_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_PYVER_RE = re.compile(r"^\d+\.\d+$")

PROCESS_MODES = ("jar_service", "go_binary", "rust_binary",
                 "node_process", "python_process")

# readiness 缺省按模式分派：jar 维持首期行为（Actuator 语义）；其余模式没有
# 通用健康端点，缺省只判进程存活，有端点的服务应显式配置 http/tcp + port
_READINESS_JAR = {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5}
_READINESS_BASE = {"type": "process", "path": "", "timeout": 90, "interval": 5}


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


def _readiness_default(mode: str) -> dict:
    return dict(_READINESS_JAR if mode == "jar_service" else _READINESS_BASE)


def parse_common(cfg: dict, mode: str) -> dict:
    """进程族公共字段（五种模式同构）；jar 专属键由 jar_config.parse 追加"""
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rd = _readiness_default(mode)
    return {
        "app_home": str(cfg.get("app_home") or "").rstrip("/"),
        "run_user": str(cfg.get("run_user") or ""),
        "work_dir": str(cfg.get("work_dir") or ""),
        "unit_name": str(cfg.get("unit_name") or ""),
        "port": _as_int(cfg.get("port"), 0),
        "app_args": str(cfg.get("app_args") or ""),
        "readiness": {
            "type": str(rt.get("type") or rd["type"]),
            "path": str(rt.get("path") or rd["path"]),
            "timeout": _as_int(rt.get("timeout"), rd["timeout"]),
            "interval": _as_int(rt.get("interval"), rd["interval"]),
        },
        "stop_timeout": _as_int(cfg.get("stop_timeout"), 60),
        "start_timeout": _as_int(cfg.get("start_timeout"), 120),
        "env_files": [str(p) for p in (cfg.get("env_files") or [])
                      if isinstance(p, str) and p],
    }


def parse_runtime_config(raw, deploy_mode: str = "jar_service") -> dict:
    """按 deploy_mode 分派；未知模式返回 {}（container/static 不消费该配置）"""
    mode = deploy_mode or "container"
    if mode == "jar_service":
        from .jar_config import parse_runtime_config as parse_jar
        return parse_jar(raw)
    if mode not in ("go_binary", "rust_binary", "node_process", "python_process"):
        return {}
    cfg = _as_dict(raw)
    out = parse_common(cfg, mode)
    if mode == "node_process":
        out["node_bin"] = str(cfg.get("node_bin") or "/usr/bin/node")
        out["start_file"] = str(cfg.get("start_file") or "")
    elif mode == "python_process":
        out["start_file"] = str(cfg.get("start_file") or "")
        out["requirements"] = str(cfg.get("requirements") or "requirements.txt")
        out["python_version"] = str(cfg.get("python_version") or "")
    return out


def validate_common(cfg: dict, mode: str) -> list:
    """进程族公共字段校验（cfg 非空前提下调用）"""
    errors = []
    app_home = cfg.get("app_home")
    if not isinstance(app_home, str) or not app_home.startswith("/"):
        errors.append("runtime_config.app_home 必填，且必须是绝对路径")
    unit_name = cfg.get("unit_name") or ""
    if unit_name and (not isinstance(unit_name, str) or not _UNIT_RE.match(unit_name)):
        errors.append("runtime_config.unit_name 只能包含字母、数字与 _ . @ -")
    rt = cfg.get("readiness") if isinstance(cfg.get("readiness"), dict) else {}
    rtype = rt.get("type") or _readiness_default(mode)["type"]
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
    env_files = cfg.get("env_files") or []
    if not isinstance(env_files, list) or any(
            not isinstance(p, str) or not p.startswith("/") for p in env_files):
        errors.append("runtime_config.env_files 必须是绝对路径数组")
    return errors


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """按 deploy_mode 分派；非进程族方式配置原样忽略（返回 []）"""
    mode = deploy_mode or "container"
    if mode == "jar_service":
        from .jar_config import validate_jar_config
        return validate_jar_config(raw)
    if mode not in ("go_binary", "rust_binary", "node_process", "python_process"):
        return []
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return [f"deploy_mode={mode} 必须配置 runtime_config.app_home"]
    errors = validate_common(cfg, mode)
    if mode in ("node_process", "python_process"):
        sf = cfg.get("start_file")
        if (not isinstance(sf, str) or not sf or sf.startswith("/")
                or ".." in sf.split("/") or any(c.isspace() for c in sf)):
            errors.append("runtime_config.start_file 必填，且为 CURRENT 下的相对路径"
                          "（不含空白与 .. 段）")
        if mode == "node_process":
            node_bin = cfg.get("node_bin", "/usr/bin/node")
            if not isinstance(node_bin, str) or not node_bin.startswith("/"):
                errors.append("runtime_config.node_bin 必须是绝对路径")
        else:
            pv = cfg.get("python_version")
            if pv is not None and str(pv) and not _PYVER_RE.match(str(pv)):
                errors.append("runtime_config.python_version 形如 3.11（major.minor）")
            req = cfg.get("requirements")
            if req is not None and (not isinstance(req, str) or not req.strip()
                                    or req.startswith("/")):
                errors.append("runtime_config.requirements 须为项目内相对路径")
    return errors


def build_exec_start(cfg: dict, deploy_mode: str) -> str:
    """ExecStart 拼装（cfg 为 parse_runtime_config 的产物）；固定引用 CURRENT/venv"""
    mode = deploy_mode or "jar_service"
    curr = f"{cfg['app_home']}/CURRENT"
    if mode == "jar_service":
        return " ".join(x for x in (cfg["java_bin"], cfg["jvm_opts"],
                                    "-jar", f"{curr}/app.jar", cfg["app_args"]) if x)
    if mode in ("go_binary", "rust_binary"):
        return " ".join(x for x in (f"{curr}/app", cfg["app_args"]) if x)
    if mode == "node_process":
        return " ".join(x for x in (cfg["node_bin"], f"{curr}/{cfg['start_file']}",
                                    cfg["app_args"]) if x)
    return " ".join(x for x in (f"{cfg['app_home']}/venv/bin/python",
                                f"{curr}/{cfg['start_file']}", cfg["app_args"]) if x)


def exec_start_marker(cfg: dict, deploy_mode: str) -> str:
    """接管门槛特征串：ExecStart 必含该子串才认为是平台创建的单元"""
    mode = deploy_mode or "jar_service"
    curr = f"{cfg['app_home']}/CURRENT"
    if mode == "jar_service":
        return f"{curr}/app.jar"
    if mode in ("go_binary", "rust_binary"):
        return f"{curr}/app"
    if mode == "node_process":
        return f"{curr}/{cfg['start_file']}"
    return f"{cfg['app_home']}/venv/bin/python"


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_unit_name(runtime_config, image_name: str = "",
                     service_name: str = "") -> str:
    """平台统一口径：unit_name → image_name → 服务名 → app（不含 .service）"""
    cfg = _as_dict(runtime_config)
    return str(cfg.get("unit_name") or image_name or service_name or "app")
```

- [ ] **Step 4: 改造 `deploy_strategies/jar_config.py` 为共享层委托**

整文件替换为：

```python
"""jar_service 运行时配置契约：Java 专属字段、产物命名

公共字段解析/校验、sha256、单元名派生已上移到 process_config.py（进程族共享层）；
本模块保留 Java 专属部分并维持既有公共函数签名不变——既有导入
（deploy_strategies.jar_config import ...）不受影响。
"""
from .process_config import (derive_unit_name,  # noqa: F401 — re-export 兼容
                             parse_common, sha256_file,  # noqa: F401 — re-export
                             validate_common, _as_dict, _as_int)

DEFAULTS = {
    "java_bin": "/usr/bin/java",
    "stop_timeout": 60,
    "start_timeout": 120,
    "artifact_pattern": "target/*.jar",
    "readiness": {"type": "http", "path": "/actuator/health",
                  "timeout": 90, "interval": 5},
}


def parse_runtime_config(raw) -> dict:
    """把 DB/YAML 中的 JSON（dict 或字符串）归一为策略可直接消费的完整结构"""
    cfg = _as_dict(raw)
    out = parse_common(cfg, "jar_service")
    out["java_bin"] = str(cfg.get("java_bin") or DEFAULTS["java_bin"])
    out["jvm_opts"] = str(cfg.get("jvm_opts") or "")
    out["artifact_pattern"] = str(cfg.get("artifact_pattern")
                                  or DEFAULTS["artifact_pattern"])
    return out


def validate_jar_config(raw) -> list:
    """jar 专属校验（Java 字段）；公共字段委托 process_config.validate_common"""
    if not isinstance(raw, (dict, str)):
        return ["runtime_config 必须是 JSON 对象"]
    cfg = _as_dict(raw)
    if not cfg:
        return ["deploy_mode=jar_service 必须配置 runtime_config.app_home"]
    errors = validate_common(cfg, "jar_service")
    java_bin = cfg.get("java_bin", DEFAULTS["java_bin"])
    if not isinstance(java_bin, str) or not java_bin.startswith("/"):
        errors.append("runtime_config.java_bin 必须是绝对路径")
    pattern = cfg.get("artifact_pattern")
    if pattern is not None and (not isinstance(pattern, str) or not pattern.strip()):
        errors.append("runtime_config.artifact_pattern 不能为空")
    return errors


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """兼容入口：全量分派给 process_config（jar/Go/Rust/Node/Python）"""
    from .process_config import validate_runtime_config as validate
    return validate(raw, deploy_mode)


def artifact_filename(service: str, version: str) -> str:
    return f"{service}-{version}.jar"
```

注意：删除原文件中的 `_as_dict/_as_int/_UNIT_RE/parse/validate/derive_unit_name/sha256_file` 旧实现（全部由上面的导入与委托替代）；`derive_unit_name` 与 `sha256_file` 通过 re-export 保持可导入。

- [ ] **Step 5: `deploy_strategies/base.py` 增加 process_modes**

在 `registered_modes()` 之后追加：

```python
def process_modes() -> list:
    """进程族 deploy_mode（注册表自动收集：ProcessServiceStrategy 的子类）

    新增进程运行时 = 新策略子类 + @register，观测/路由放行无需改清单。
    """
    return sorted(name for name, cls in _REGISTRY.items()
                  if getattr(cls, "is_process", False))
```

（同时给 `deploy_strategies/jar_service.py` 的 `JarServiceStrategy` 补一行类属性
`is_process = True`（放在既有 `supports_*` 标志旁）——此刻注册表里唯一应被收集的
进程模式，`ProcessModesTest` 断言即为此态（`process_modes() == ["jar_service"]`）。
Task 2 抽基类后该标志由 `ProcessServiceStrategy` 承载、jar 薄子类继承，本行随整文件
重写自然消失。）

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/test_process_config.py tests/test_jar_service.py tests/test_jar_db.py tests/test_service_unit.py -q`
Expected: PASS（既有 jar 用例零回归）。

- [ ] **Step 7: 提交**

```bash
git add deploy_strategies/process_config.py deploy_strategies/jar_config.py deploy_strategies/base.py deploy_strategies/jar_service.py tests/test_process_config.py
git commit -m "feat(strategy): shared process_config layer and process_modes registry"
```

---

### Task 2: ProcessServiceStrategy 基类抽取 + jar 子类化 + service_unit 钩子化

**Files:**
- Create: `deploy_strategies/process_service.py`
- Modify: `deploy_strategies/jar_service.py`（整文件重写为薄子类）
- Modify: `service_unit.py`
- Test: `tests/test_process_unit.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `parse_runtime_config`/`build_exec_start`/`exec_start_marker`/`sha256_file`/`derive_unit_name`、`base.process_modes` 的 `is_process` 标志位。
- Produces:
  - `ProcessServiceStrategy`（`deploy_strategies.process_service`）：属性 `is_process=True`、`artifact_kind`（"file"）、`artifact_label`、`mode`、`cfg`、`app_home`、`service`、`unit`、`jars_dir`、`curr_link`；钩子 `artifact_name(version) -> str`、`remote_entry() -> str`、`post_transfer(server, remote_path)`、`post_extract(server, tmp_dir, version)`、`runtime_probe(server)`、`ensure_runtime(server, version)`；具体方法 `local_artifact(version=None)`、`check_local()`、`query_current_version()`、`next_version(current)`、`check(server)`、`prepare(server)`、`transfer(server, version=None)`、`prefetch(servers)`、`switch(server)`、`verify(server, server_entry=None)`、`rollback(server)`、`rollback_source_missing(server)`、`exec_start_argv()`、`exec_start_marker()`。
  - `service_unit.render_unit(runtime_config, service, deploy_mode="jar_service")`（新增第三参，默认值向后兼容）。
  - `service_unit.onboard_unit(server, unit, content, cfg, deploy_mode="jar_service")`（第 4 参从 `app_home: str` 改为解析后的 `cfg: dict`，由 Task 5 更新调用点）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_process_unit.py`：

```python
"""进程族 unit 渲染与接入门槛：go/node/python 形态 + 接管特征串按模式分派"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import service_unit
from deploy_strategies.process_config import parse_runtime_config


class _Server:
    def __init__(self, existing=""):
        self.commands = []
        self._existing = existing

    def exec(self, cmd, timeout=60, stdin_data=None):
        self.commands.append(cmd)
        return self._existing

    def try_exec(self, cmd, timeout=60):
        self.commands.append(cmd)
        return True


class RenderUnitTest(unittest.TestCase):
    def test_jar_default_mode_unchanged(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/a", "java_bin": "/usr/bin/java"}, "asvc")
        self.assertIn("ExecStart=/usr/bin/java -jar /opt/a/CURRENT/app.jar\n", content)
        self.assertIn("SuccessExitStatus=143", content)

    def test_go_exec_start(self):
        content = service_unit.render_unit({"app_home": "/opt/g"}, "gsvc", "go_binary")
        self.assertIn("ExecStart=/opt/g/CURRENT/app\n", content)
        self.assertIn("SuccessExitStatus=143", content)

    def test_node_exec_start(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/n", "node_bin": "/usr/bin/node",
             "start_file": "dist/server.js"}, "nsvc", "node_process")
        self.assertIn("ExecStart=/usr/bin/node /opt/n/CURRENT/dist/server.js\n", content)

    def test_python_exec_start_uses_venv(self):
        content = service_unit.render_unit(
            {"app_home": "/opt/p", "start_file": "main.py"}, "psvc", "python_process")
        self.assertIn("ExecStart=/opt/p/venv/bin/python /opt/p/CURRENT/main.py\n",
                      content)


class OnboardMarkerTest(unittest.TestCase):
    def test_rejects_foreign_unit_per_mode(self):
        cfg = parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        server = _Server(existing="ExecStart={ path=/bin/other }")
        with self.assertRaises(RuntimeError) as ctx:
            service_unit.onboard_unit(server, "gsvc", "[Unit]\n", cfg, "go_binary")
        self.assertIn("拒绝接管", str(ctx.exception))

    def test_accepts_platform_unit(self):
        cfg = parse_runtime_config({"app_home": "/opt/g"}, "go_binary")
        server = _Server(existing="ExecStart=/opt/g/CURRENT/app")
        service_unit.onboard_unit(server, "gsvc", "[Unit]\n", cfg, "go_binary")
        self.assertTrue(any("base64 -d" in c for c in server.commands))
        self.assertTrue(any("daemon-reload" in c for c in server.commands))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_process_unit.py -q`
Expected: FAIL（`render_unit` 不接受第三参 → TypeError；`onboard_unit` 签名不符）。

- [ ] **Step 3: 实现 `deploy_strategies/process_service.py`**

```python
"""进程族发布策略基类（deploy_mode: jar_service/go_binary/rust_binary/node_process/python_process）

产物按版本不可变存放，CURRENT 符号链接原子切换，systemd unit 的 ExecStart 固定引用
CURRENT（或 venv 解释器）：发布与回滚都只是「换链接 + 重启单元」，回滚不需要重传。
权威版本状态在目标机（.deploy_version 标记 + CURRENT 链接），平台库只做展示缓存。

产物形态两种：
- file（go/rust/jar）：单文件，SFTP 上传 + sha256 复核 + post_transfer 钩子（chmod）；
- archive（node/python）：tar.gz，上传复核后解压到 {ver}.tmp → post_extract 钩子 →
  原子 mv 就位（Task 8 落地，本任务仅含 file 路径）。

与容器模式的根本差异：单实例重启必然有停机窗口，切换语义是「停机→换链接→启动」，
不承诺零停机；Nacos 上下线仍由编排层按能力标志调度，与运行时方式无关。
"""
import shlex
import time
from pathlib import Path

from .base import DeployStrategy
from .process_config import (build_exec_start, derive_unit_name,
                             exec_start_marker, parse_runtime_config,
                             sha256_file)
from common import (log, load_yaml, default_servers_file, merge_server_ssh,
                    get_ssh_config)


class ProcessServiceStrategy(DeployStrategy):
    """进程族五阶段实现；差异下沉为少量钩子（见各钩子 docstring）"""

    is_process = True
    name = ""
    artifact_kind = "file"      # file | archive（archive 路径由 Task 8 引入）
    artifact_label = "产物"      # 日志文案用名词（jar/二进制/压缩包）
    supports_nacos = True
    supports_canary = True
    supports_prefetch = True
    supports_restart = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        self.mode = proj.get("deploy_mode") or self.name
        self.cfg = parse_runtime_config(proj.get("runtime_config"), self.mode)
        if not self.cfg["app_home"]:
            raise ValueError(
                f"deploy_mode={self.name} 必须配置 runtime_config.app_home"
                f"（如 /opt/apps/<svc>）")
        self.app_home = self.cfg["app_home"]
        self.service = (proj.get("image_name") or
                        getattr(deployer, "project", "app"))
        self.unit = derive_unit_name(self.cfg, self.service)
        self.jars_dir = f"{self.app_home}/releases"
        self.curr_link = f"{self.app_home}/CURRENT"

    # ---------- 子类钩子 ----------

    def artifact_name(self, version: str) -> str:
        """dist 产物文件名（含扩展名语义）"""
        raise NotImplementedError

    def remote_entry(self) -> str:
        """releases/{ver}/ 下入口文件相对路径；archive 模式返回 None"""
        raise NotImplementedError

    def post_transfer(self, server, remote_path: str):
        """file 模式上传复核后的钩子（go/rust 在此 chmod 0755）；默认无动作"""

    def post_extract(self, server, tmp_dir: str, version: str):
        """archive 模式解压后、mv 就位前的钩子（node 修 .bin 执行位）；默认无动作"""

    def runtime_probe(self, server):
        """check 阶段附加探测（java_bin/node_bin/python3+ensurepip）；默认无动作"""

    def ensure_runtime(self, server, version: str):
        """版本级运行时保障（python 的 venv），switch/rollback 在启动前调用；默认无动作"""

    # ---------- ExecStart 与接管门槛（公式化，子类无需覆盖） ----------

    def exec_start_argv(self) -> str:
        return build_exec_start(self.cfg, self.mode)

    def exec_start_marker(self) -> str:
        return exec_start_marker(self.cfg, self.mode)

    # ---------- 产物定位 ----------

    def local_artifact(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / \
            self.artifact_name(version or self.d.version)

    # ---------- 阶段 0：本地预检与版本推导 ----------

    def check_local(self):
        p = self.local_artifact()
        if not p.exists():
            raise RuntimeError(
                f"本地 {self.artifact_label} 不存在: {p}"
                f"（先执行构建，或把产物与 .sha256 放入 dist 目录）")
        side = p.with_name(p.name + ".sha256")
        if not side.exists():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        if expected != sha256_file(p):
            raise RuntimeError(
                f"{self.artifact_label} 校验和不一致: {p}"
                f"（产物被改动或旁文件过期，请重新构建）")

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

    # ---------- 五阶段 ----------

    def check(self, server):
        """预检：运行时依赖、systemd、目录可写、磁盘余量——全部在造成变更前完成"""
        self.runtime_probe(server)
        server.exec("command -v systemctl >/dev/null", timeout=15)
        server.exec(
            f"mkdir -p {shlex.quote(self.jars_dir)} && test -w {shlex.quote(self.app_home)}",
            timeout=15)
        self._check_disk(server)

    def _check_disk(self, server):
        """按包大小倍数检查可用空间：file 留 1.5 倍；archive 留 3 倍（压缩包+解压+tmp 并存）"""
        p = self.local_artifact()
        if not p.exists():
            return  # 本地产物缺失由 check_local/transfer 报错，这里不重复报
        factor = 3.0 if self.artifact_kind == "archive" else 1.5
        need_kb = int(p.stat().st_size * factor / 1024) + 1
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
                f"需要约 {need_kb}KB（含 {factor} 倍余量）")

    def prepare(self, server):
        """确认单元已由平台接入，且不会被误当成别人创建的服务接管

        平台对 unit 的承诺是「只管理自己创建的」：ExecStart 不含模式特征串的
        单元一律拒绝，避免把人工维护的服务改坏。
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
        expected = self.exec_start_marker()
        if expected not in exec_start:
            raise RuntimeError(
                f"[{server.host}] 拒绝接管非平台创建的单元 {self.unit}.service："
                f"ExecStart 未指向 {expected}")

    def transfer(self, server, version: str = None):
        """上传到 releases/{ver}/（幂等：远端 sha256 相同则跳过）——file 路径"""
        ver = version or self.d.version
        local = self.local_artifact(ver)
        if not local.exists():
            raise RuntimeError(f"本地 {self.artifact_label} 不存在: {local}")
        local_sha = sha256_file(local)
        remote_dir = f"{self.jars_dir}/{ver}"
        remote = f"{remote_dir}/{self.remote_entry()}"
        server.exec(f"mkdir -p {shlex.quote(remote_dir)}", timeout=15)
        if server.try_exec(f"test -f {remote}", timeout=15):
            current = server.exec(
                f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
            if current == local_sha:
                log(f"[{server.host}] 远端已有相同产物，跳过上传: {ver}")
                return True
        log(f"[{server.host}] 上传 {self.artifact_label} {self.artifact_name(ver)}"
            f"（{local.stat().st_size / 1024 / 1024:.1f} MB）-> {remote}")
        server.sftp_put_with_progress(
            str(local), remote, label=self.artifact_name(ver),
            streams=getattr(self.d, "upload_streams", 1))
        # 传输后必须复核：SFTP 中断可能留下半包，宁可在这里失败也不要切到坏包
        remote_sha = server.exec(
            f"sha256sum {shlex.quote(remote)} | awk '{{print $1}}'", timeout=60).strip()
        if remote_sha != local_sha:
            raise RuntimeError(
                f"[{server.host}] 上传后校验和不一致（本地 {local_sha[:12]}…，"
                f"远端 {remote_sha[:12]}…）")
        self.post_transfer(server, remote)
        return True

    def prefetch(self, servers: list) -> list:
        """并行预传（失败不中断，滚动阶段自动重试），与静态策略同构"""
        try:
            self.check_local()
        except Exception as e:
            log(f"{self.artifact_label} 预传输跳过: {e}")
            return [s["ip"] for s in servers]
        import deploy as deploy_mod
        from concurrent.futures import ThreadPoolExecutor
        log(f">>> 并行预上传 {self.artifact_label} 到 {len(servers)} 台服务器")

        def _one(srv: dict) -> tuple:
            host = srv["ip"]
            server = deploy_mod.RemoteServer(host, merge_server_ssh(self.d.ssh_cfg, srv))
            try:
                self.transfer(server)
                log(f"[{host}] {self.artifact_label} 预传输完成")
                return host, True
            except Exception as e:
                log(f"[{host}] {self.artifact_label} 预传输失败: {e}")
                return host, False
            finally:
                server.close()

        with ThreadPoolExecutor(max_workers=max(1, min(len(servers), 8))) as pool:
            results = list(pool.map(_one, servers))
        failures = [host for host, ok in results if not ok]
        log(f"{self.artifact_label} 预传输完成：成功 {len(servers) - len(failures)} 台" +
            (f"，失败 {len(failures)} 台: {failures}（滚动阶段自动重试）" if failures else "，全部就绪"))
        return failures

    # ---------- 阶段 4/5：切换与就绪验证 ----------

    def switch(self, server):
        """停机 → 原子换链接 → 运行时保障 → 启动 → 写版本标记

        换链接开始即视为目标状态已变更（编排层据此在后续失败时回滚）；
        链接用「临时名 + mv -Tf」替换，避免 ln -sfn 在目录已存在时的嵌套行为。
        """
        self.d.target_modified = True
        ver = self.d.version
        target = f"{self.jars_dir}/{ver}"
        tmp = f"{self.app_home}/.CURRENT.tmp"
        server.exec(f"systemctl stop {shlex.quote(self.unit)}.service || true",
                    timeout=self.cfg["stop_timeout"] + 30)
        server.exec(
            f"ln -sfn {shlex.quote(target)} {shlex.quote(tmp)} && "
            f"mv -Tf {shlex.quote(tmp)} {shlex.quote(self.curr_link)}",
            timeout=60)
        self.ensure_runtime(server, ver)
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

    # ---------- 阶段 5：回滚 ----------

    def rollback(self, server):
        """链接指回 previous_version 并重启；本机没有该版本目录时告警跳过

        产物按版本不可变保存，回滚只需换链接 + 重启，无需重传；目标版本目录缺失
        说明该主机从未发过该版本（或产物被清理），此处保持现状由编排层/人工介入。
        回滚目标版本的运行时保障（venv 依赖变化重装）在重启前完成。
        """
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
        self.ensure_runtime(server, ver)
        server.exec(f"systemctl restart {shlex.quote(self.unit)}.service",
                    timeout=self.cfg["start_timeout"] + 30)
        server.exec(
            f"echo {shlex.quote(ver)} > {shlex.quote(self.app_home + '/.deploy_version')}",
            timeout=15)
        log(f"[{server.host}] 已回滚 CURRENT -> releases/{ver}")

    def rollback_source_missing(self, server) -> bool:
        """显式回滚前置检查：本机缺少 previous_version 产物目录即判定该主机回滚失败"""
        ver = self.d.previous_version
        if not ver:
            return False  # 无目标版本属于「跳过回滚」，由 rollback 自己说明
        return not server.try_exec(
            f"test -d {shlex.quote(self.jars_dir + '/' + ver)}", timeout=15)
```

- [ ] **Step 4: 重写 `deploy_strategies/jar_service.py` 为薄子类**

整文件替换为：

```python
"""纯 Java 进程发布策略（deploy_mode: jar_service，Linux + systemd）

进程族共享基类（process_service.ProcessServiceStrategy）的 Java 形态：
产物为可执行 jar（dist/{service}-{version}.jar + .sha256），
ExecStart = {java_bin} {jvm_opts} -jar {app_home}/CURRENT/app.jar {app_args}。
五阶段语义与基类完全一致；jar 的默认 readiness（http + /actuator/health）由
process_config 按模式分派，行为与首期实现逐字一致。
"""
import shlex

from .base import register
from .jar_config import artifact_filename
from .process_service import ProcessServiceStrategy


@register
class JarServiceStrategy(ProcessServiceStrategy):
    name = "jar_service"
    artifact_label = "jar"

    def artifact_name(self, version: str) -> str:
        return artifact_filename(self.service, version or self.d.version)

    def remote_entry(self) -> str:
        return "app.jar"

    def runtime_probe(self, server):
        server.exec(f"command -v {shlex.quote(self.cfg['java_bin'])} >/dev/null",
                    timeout=15)

    # ---------- 兼容别名：既有测试与调用点使用 jar 命名 ----------

    def jar_name(self, version: str = None) -> str:
        return self.artifact_name(version or self.d.version)

    def local_jar(self, version: str = None) -> Path:
        return self.local_artifact(version or self.d.version)
```

注意文件头需要 `from pathlib import Path`（`local_jar` 返回类型标注用）；如 ruff 提示未使用可去掉标注。

同时把 `deploy_strategies/__init__.py` 的 jar 注释更新为：

```python
from . import jar_service  # noqa: F401 — 触发 @register JarServiceStrategy（进程族基类的 Java 形态）
```

- [ ] **Step 5: 改造 `service_unit.py`**

整文件替换为：

```python
"""systemd 单元生成与接入

平台对目标机的系统写入被刻意限制为三件事：写 unit 文件、daemon-reload、enable。
不安装运行时、不改内核参数、不动防火墙；对已存在且 ExecStart 不含本模式特征串
（process_config.exec_start_marker）的单元一律拒绝接管。
"""
import base64
import shlex
import sys
from pathlib import Path

# 仓库根模块（与 deploy.py 同级）需自行把 scripts/ 加入搜索路径：
# 不能依赖「先 import 了 deploy_strategies 才顺带生效」这种隐式顺序
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from deploy_strategies.process_config import (build_exec_start, exec_start_marker,
                                              parse_runtime_config)
from common import log


def render_unit(runtime_config, service: str, deploy_mode: str = "jar_service") -> str:
    """按运行时配置渲染 unit：ExecStart 固定引用 CURRENT（或 venv），发布只换链接不改 unit"""
    cfg = parse_runtime_config(runtime_config, deploy_mode)
    work_dir = cfg["work_dir"] or f"{cfg['app_home']}/CURRENT"
    exec_start = build_exec_start(cfg, deploy_mode)
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
        # 有 SIGTERM 处理器的运行时（Java、部分 Python 应用）正常退出码 143：
        # 不声明会被 systemd 判为失败并触发重启；无处理器的运行时此行无害
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


def onboard_unit(server, unit: str, content: str, cfg: dict,
                 deploy_mode: str = "jar_service") -> None:
    """在单台服务器接入 unit（幂等）：拒绝接管、写文件、reload、enable

    cfg 为 parse_runtime_config 的产物（含 app_home 与模式专属字段），
    接管特征串按 deploy_mode 分派。
    """
    expected = exec_start_marker(cfg, deploy_mode)
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
    log(f"[{getattr(server, 'host', '?')}] 单元已接入: {unit}.service")
```

- [ ] **Step 6: 运行测试（回归门）**

Run: `python -m pytest tests/test_process_unit.py tests/test_jar_service.py tests/test_service_unit.py tests/test_config_routes_jar.py -q`

预期：`test_process_unit.py` PASS；既有 jar/unit 用例可能因以下两处契约变化失败，按说明修正测试（行为语义不变）：

1. `tests/test_service_unit.py` 中 `onboard_unit(...)` 的旧调用传的是 `app_home` 字符串 → 改为传 `parse_runtime_config(raw, "jar_service")` 的 cfg dict（第 4 参）。
2. 若有用例直接构造 `JarServiceStrategy` 并访问被上移的属性，确认仍可用（`jars_dir`/`curr_link`/`cfg`/`unit` 名称未变）。

Run: `python -m pytest -q`
Expected: 全部 PASS（全量回归门——基类抽取不改任何行为）。

- [ ] **Step 7: 提交**

```bash
git add deploy_strategies/process_service.py deploy_strategies/jar_service.py deploy_strategies/__init__.py service_unit.py tests/test_process_unit.py tests/test_service_unit.py
git commit -m "refactor(strategy): extract ProcessServiceStrategy base from jar_service"
```

---

### Task 3: go_binary / rust_binary 策略

**Files:**
- Create: `deploy_strategies/go_binary.py`
- Create: `deploy_strategies/rust_binary.py`
- Modify: `deploy_strategies/__init__.py`
- Test: `tests/test_go_rust_service.py`（新建）

**Interfaces:**
- Consumes: Task 2 的 `ProcessServiceStrategy`（钩子 `artifact_name`/`remote_entry`/`post_transfer`）。
- Produces: 注册名 `go_binary`、`rust_binary`；dist 产物 `{service}-{version}`（无扩展名）+ `.sha256`；远端 `releases/{ver}/app`；`process_modes()` 含两者。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_go_rust_service.py`：

```python
"""go_binary/rust_binary 策略：注册、五阶段命令序列、chmod 与接管门槛（Fake SSH）"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY, process_modes
from deploy_strategies.go_binary import GoBinaryStrategy
from deploy_strategies.process_config import sha256_file
from deploy_strategies.rust_binary import RustBinaryStrategy

APP_HOME = "/opt/apps/report"


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


def make_deployer(tmp_dir: str, mode="go_binary", version="1.0.0") -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "report"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0, "health_timeout": "1",
                           "health_interval": "0"}}
    d.ssh_cfg = {}
    d.proj_cfg = {"deploy_mode": mode, "image_name": "report",
                  "runtime_config": {"app_home": APP_HOME}}
    d.dist_dir = Path(tmp_dir)
    d.remote_tmp_dir = "/tmp/platform-deploy"
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_binary(deployer: Deployer, version=None, payload=b"\x7fELFbin"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"report-{version}"
    p.write_bytes(payload)
    p.with_name(p.name + ".sha256").write_text(sha256_file(p) + "\n",
                                               encoding="utf-8")
    return p


class RegistryTest(unittest.TestCase):
    def test_registered_and_routed(self):
        self.assertIn("go_binary", _REGISTRY)
        self.assertIn("rust_binary", _REGISTRY)
        self.assertIsInstance(
            create_strategy("go_binary", make_deployer(tempfile.mkdtemp())),
            GoBinaryStrategy)
        self.assertIsInstance(
            create_strategy("rust_binary",
                            make_deployer(tempfile.mkdtemp(), mode="rust_binary")),
            RustBinaryStrategy)

    def test_process_modes_contains_both(self):
        modes = process_modes()
        self.assertIn("go_binary", modes)
        self.assertIn("rust_binary", modes)

    def test_capability_flags(self):
        s = create_strategy("go_binary", make_deployer(tempfile.mkdtemp()))
        self.assertTrue(s.supports_nacos)
        self.assertTrue(s.supports_canary)
        self.assertTrue(s.supports_prefetch)
        self.assertTrue(s.supports_restart)
        self.assertEqual(s.version_scheme, "semver")

    def test_app_home_required(self):
        d = make_deployer(tempfile.mkdtemp())
        d.proj_cfg["runtime_config"] = {}
        with self.assertRaises(ValueError) as ctx:
            create_strategy("go_binary", d)
        self.assertIn("app_home", str(ctx.exception))


class TransferTest(unittest.TestCase):
    def test_upload_then_chmod(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        bin_path = make_binary(d)
        s = create_strategy("go_binary", d)
        remote = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"sha256sum": sha256_file(bin_path)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded[0][1], f"{APP_HOME}/releases/1.0.0/app")
        self.assertTrue(any(c.startswith("chmod 0755") for c in remote.exec_calls))

    def test_skips_when_remote_sha_matches(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        bin_path = make_binary(d)
        s = create_strategy("go_binary", d)
        remote = FakeRemote(rules={"sha256sum": sha256_file(bin_path)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded, [])


class CheckPrepareTest(unittest.TestCase):
    def test_check_has_no_runtime_probe_and_no_java(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        make_binary(d)
        s = create_strategy("go_binary", d)
        r = FakeRemote(rules={"df -Pk": "1048576"})
        s.check(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn("command -v systemctl", joined)
        self.assertNotIn("java", joined)

    def test_prepare_rejects_foreign_exec_start(self):
        d = make_deployer(tempfile.mkdtemp())
        s = create_strategy("go_binary", d)
        r = FakeRemote(rules={"systemctl show -p ExecStart":
                              "ExecStart={ path=/bin/other }"})
        with self.assertRaises(RuntimeError) as ctx:
            s.prepare(r)
        self.assertIn("拒绝接管", str(ctx.exception))
        self.assertIn(f"{APP_HOME}/CURRENT/app", str(ctx.exception))

    def test_prepare_missing_unit(self):
        d = make_deployer(tempfile.mkdtemp())
        s = create_strategy("go_binary", d)
        r = FakeRemote(try_exec_ok={"systemctl cat": False})
        with self.assertRaises(RuntimeError) as ctx:
            s.prepare(r)
        self.assertIn("未接入", str(ctx.exception))


class SwitchRollbackTest(unittest.TestCase):
    def test_switch_symlink_marker_and_version(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp, version="1.0.1")
        s = create_strategy("go_binary", d)
        r = FakeRemote()
        s.switch(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"ln -sfn {APP_HOME}/releases/1.0.1", joined)
        self.assertIn("mv -Tf", joined)
        self.assertIn("systemctl start", joined)
        self.assertIn(".deploy_version", joined)
        self.assertTrue(d.target_modified)

    def test_rollback_without_source_skips(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        d.previous_version = "0.9.0"
        s = create_strategy("go_binary", d)
        r = FakeRemote(try_exec_ok={"test -d": False})
        s.rollback(r)
        self.assertFalse(d.target_modified)
        self.assertFalse(any("systemctl restart" in c for c in r.exec_calls))

    def test_rollback_source_missing_flag(self):
        tmp = tempfile.mkdtemp(prefix="go_svc_")
        d = make_deployer(tmp)
        d.previous_version = "0.9.0"
        s = create_strategy("rust_binary", d)
        self.assertTrue(s.rollback_source_missing(
            FakeRemote(try_exec_ok={"test -d": False})))
        self.assertFalse(s.rollback_source_missing(FakeRemote()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_go_rust_service.py -q`
Expected: FAIL（`ModuleNotFoundError: deploy_strategies.go_binary`）。

- [ ] **Step 3: 实现 `deploy_strategies/go_binary.py`**

```python
"""Go 单二进制发布策略（deploy_mode: go_binary，Linux + systemd）

产物为平台机交叉编译的 Linux 二进制（CGO_ENABLED=0，见 build.py 的 go 分支），
transfer 后 chmod 0755；ExecStart 直接执行 {app_home}/CURRENT/app——目标机零运行时依赖。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class GoBinaryStrategy(ProcessServiceStrategy):
    name = "go_binary"
    artifact_label = "二进制"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}"

    def remote_entry(self) -> str:
        return "app"

    def post_transfer(self, server, remote_path: str):
        server.exec(f"chmod 0755 {shlex.quote(remote_path)}", timeout=15)

    def runtime_probe(self, server):
        pass  # 无解释器：目标机零依赖，预检只看 systemctl/磁盘（基类）
```

- [ ] **Step 4: 实现 `deploy_strategies/rust_binary.py`**

```python
"""Rust 单二进制发布策略（deploy_mode: rust_binary，Linux + systemd）

产物为 cargo --release 构建的 Linux 二进制（交叉编译需 build.rust_target，
见 build.py 的 rust 分支）；transfer 后 chmod 0755；ExecStart 直接执行
{app_home}/CURRENT/app——目标机零运行时依赖。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class RustBinaryStrategy(ProcessServiceStrategy):
    name = "rust_binary"
    artifact_label = "二进制"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}"

    def remote_entry(self) -> str:
        return "app"

    def post_transfer(self, server, remote_path: str):
        server.exec(f"chmod 0755 {shlex.quote(remote_path)}", timeout=15)

    def runtime_probe(self, server):
        pass  # 无解释器：目标机零依赖，预检只看 systemctl/磁盘（基类）
```

- [ ] **Step 5: 注册导入**

`deploy_strategies/__init__.py` 的 jar 行之后追加：

```python
from . import go_binary  # noqa: F401 — 触发 @register GoBinaryStrategy
from . import rust_binary  # noqa: F401 — 触发 @register RustBinaryStrategy
```

- [ ] **Step 6: 运行测试**

Run: `python -m pytest tests/test_go_rust_service.py tests/test_process_config.py tests/test_jar_service.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add deploy_strategies/go_binary.py deploy_strategies/rust_binary.py deploy_strategies/__init__.py tests/test_go_rust_service.py
git commit -m "feat(strategy): go_binary and rust_binary process strategies"
```

---

### Task 4: build.py 的 go/rust 分支

**Files:**
- Modify: `scripts/build.py`
- Test: `tests/test_build_go_rust.py`（新建）

**Interfaces:**
- Consumes: `find_mvn` 同款的工具链定位约定（`build.{go_home,cargo_home}` / PATH）；`run()`/`fail()`/`log()` 既有函数。
- Produces:
  - `build_type` 新分支 `go`/`rust`：`_run_go_steps(proj, project_root, version, env_vars, config)`、`_run_rust_steps(...)`（构建内直接产出 dist 产物与 `.sha256`）。
  - 工具函数 `_find_tool(tool, config_key, config, hint) -> str`、`_dist_dir(config) -> Path`、`_write_sha256(path) -> None`。
  - `build_project` 的 artifact 映射新增 `go_binary/rust_binary → "binary"`（构建分支自含产物步骤，`build_project` 对 `binary` 直接返回，不进 docker 段）。
  - 构建配置键：`build.go_arch`（缺省 amd64）、`build.go_main`（缺省 `.`）、`build.go_ldflags`（可选）、`build.rust_target`（Windows 主机必填）、`build.rust_bin`（缺省服务名）、`build.go_home`/`build.cargo_home`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_build_go_rust.py`：

```python
"""build.py 的 go/rust 分支：交叉编译命令、产物与 sha256、Windows 缺 target 报错、docker 零调用"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build as build_mod


def base_config(root: Path) -> dict:
    return {"deploy": {"project_root": str(root), "dist_dir": str(root / "out"),
                       "docker_compose_path": "", "remote_tmp_dir": "/tmp/x"},
            "registry": {"host": "h", "namespace": "n"},
            "deploy_mode": "direct"}


class GoBuildTest(unittest.TestCase):
    def test_cross_compile_env_and_artifact(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_"))
        calls = []

        def fake_run(cmd, cwd, env=None, timeout=None, **kw):
            calls.append((cmd, dict(env or {})))
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\x7fELF")

        with mock.patch.object(build_mod, "run", side_effect=fake_run), \
                mock.patch.object(build_mod, "_find_tool", return_value="go"):
            build_mod._run_go_steps({"image_name": "report"}, root, "1.0.0", {},
                                    base_config(root))
        cmd, env = calls[0]
        self.assertEqual(cmd[:3], ["go", "build", "-trimpath"])
        self.assertEqual(env["CGO_ENABLED"], "0")
        self.assertEqual(env["GOOS"], "linux")
        self.assertEqual(env["GOARCH"], "amd64")
        out = root / "out" / "report-1.0.0"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "report-1.0.0.sha256").exists())

    def test_ldflags_and_arch_from_config(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_"))
        calls = []

        def fake_run(cmd, cwd, env=None, timeout=None, **kw):
            calls.append(cmd)
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")

        cfg = base_config(root)
        cfg["build"] = {"go_arch": "arm64", "go_ldflags": "-s -w"}
        with mock.patch.object(build_mod, "run", side_effect=fake_run), \
                mock.patch.object(build_mod, "_find_tool", return_value="go"):
            build_mod._run_go_steps({"image_name": "report"}, root, "1.0.0", {}, cfg)
        self.assertIn("arm64", calls[0])  # 环境变量断言见上一用例；此处查命令存在
        env = None

    def test_project_branch_never_calls_docker(self):
        root = Path(tempfile.mkdtemp(prefix="go_build_proj_"))
        proj = {"deploy_mode": "go_binary", "build_type": "none",
                "image_name": "report"}
        config = base_config(root)
        with mock.patch.object(build_mod, "load_config", return_value=config), \
                mock.patch.object(build_mod, "get_project_config", return_value=proj), \
                mock.patch.object(build_mod, "get_registry_auth", return_value={}), \
                mock.patch.object(build_mod, "get_registry_mode", return_value="direct"), \
                mock.patch.object(build_mod, "git_prepare"), \
                mock.patch.object(build_mod, "docker_login") as dl:
            build_mod.build_project("report", "1.0.0")
        dl.assert_not_called()


class RustBuildTest(unittest.TestCase):
    def test_windows_without_target_fails(self):
        root = Path(tempfile.mkdtemp(prefix="rust_build_"))
        with mock.patch.object(build_mod, "_find_tool", return_value="cargo"), \
                mock.patch.object(build_mod.os, "name", "nt"):
            with self.assertRaises(SystemExit):
                build_mod._run_rust_steps({"image_name": "report"}, root, "1.0.0",
                                          {}, base_config(root))

    def test_build_with_target_and_copy(self):
        root = Path(tempfile.mkdtemp(prefix="rust_build_"))
        target_dir = root / "target" / "x86_64-unknown-linux-gnu" / "release"
        target_dir.mkdir(parents=True)
        (target_dir / "report").write_bytes(b"ELF")
        calls = []
        cfg = base_config(root)
        cfg["build"] = {"rust_target": "x86_64-unknown-linux-gnu"}
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "_find_tool", return_value="cargo"), \
                mock.patch.object(build_mod.os, "name", "nt"):
            build_mod._run_rust_steps({"image_name": "report"}, root, "1.0.0", {}, cfg)
        self.assertEqual(calls[0][:4], ["cargo", "build", "--release", "--target"])
        out = root / "out" / "report-1.0.0"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "report-1.0.0.sha256").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_build_go_rust.py -q`
Expected: FAIL（`AttributeError: ... has no attribute '_run_go_steps'`）。

- [ ] **Step 3: 实现工具函数与分支**

`scripts/build.py`：

1) 在 `_make_jar_artifact` 之后新增：

```python
def _dist_dir(config: dict) -> Path:
    """dist 目录解析：与 _make_static_zip/_make_jar_artifact 同规则，统一入口"""
    dist_dir = Path(config["deploy"].get("dist_dir", "dist"))
    if not dist_dir.is_absolute():
        dist_dir = Path(__file__).resolve().parent.parent / dist_dir
    dist_dir.mkdir(parents=True, exist_ok=True)
    return dist_dir


def _write_sha256(path: Path) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    path.with_name(path.name + ".sha256").write_text(h.hexdigest() + "\n",
                                                     encoding="utf-8")


def _find_tool(tool: str, config_key: str, config: dict, hint: str) -> str:
    """跨平台定位构建工具：build.{config_key} 显式配置（GOROOT/cargo 根或可执行文件）
    > PATH；与 find_mvn 同套路（Windows CreateProcess 只认 .exe/.cmd，which 已处理）"""
    home = (config.get("build") or {}).get(config_key) or ""
    if home:
        base = Path(home).expanduser()
        for cand in (base / "bin" / tool, base / tool):
            if cand.exists():
                return str(cand)
        fail(f"配置的 build.{config_key} 无效（找不到 {tool}）：{home}")
    found = shutil.which(tool)
    if found:
        return found
    fail(hint)


def _run_go_steps(proj: dict, project_root: Path, version: str,
                  env_vars: dict, config: dict):
    """Go 交叉编译：CGO_ENABLED=0 GOOS=linux，产物直接写 dist/{svc}-{ver}（零 docker）"""
    go = _find_tool("go", "go_home", config,
                    "未找到 Go：请在 config 的 build.go_home 配置 GOROOT，"
                    "或安装 Go 并加入 PATH（需支持 GOOS=linux 交叉编译）")
    build_cfg = config.get("build") or {}
    arch = str(build_cfg.get("go_arch") or "amd64")
    main = str(build_cfg.get("go_main") or ".")
    service = proj.get("image_name") or proj.get("name") or "app"
    out = _dist_dir(config) / f"{service}-{version}"
    env = dict(env_vars)
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = "linux"
    env["GOARCH"] = arch
    cmd = [go, "build", "-trimpath"]
    if build_cfg.get("go_ldflags"):
        cmd += ["-ldflags", str(build_cfg["go_ldflags"])]
    cmd += ["-o", str(out), main]
    log(f"使用 Go: {go}（CGO_ENABLED=0 GOOS=linux GOARCH={arch}）")
    run(cmd, project_root, env=env, timeout=600)
    _write_sha256(out)
    log(f"二进制产物已生成: {out}（sha256 已写旁文件）")


def _run_rust_steps(proj: dict, project_root: Path, version: str,
                    env_vars: dict, config: dict):
    """cargo --release 构建：交叉编译需 build.rust_target；Windows 主机缺配置即报错引导"""
    cargo = _find_tool("cargo", "cargo_home", config,
                       "未找到 cargo：请配置 build.cargo_home 或安装 Rust；"
                       "Windows 平台机建议改用 WSL/Linux 构建，"
                       "或 build_type=none 外部投入产物")
    build_cfg = config.get("build") or {}
    triple = str(build_cfg.get("rust_target") or "")
    if not triple and os.name == "nt":
        fail("build.rust_target 未配置：Windows 平台机无法自动确定 Linux 交叉目标；"
             "请配置（如 x86_64-unknown-linux-gnu，并安装对应 target 与 linker），"
             "或改用 WSL/Linux 构建，或 build_type=none 外部投入产物")
    cmd = [cargo, "build", "--release"]
    if triple:
        cmd += ["--target", triple]
    run(cmd, project_root, env=env_vars, timeout=1800)
    service = proj.get("image_name") or proj.get("name") or "app"
    bin_name = str(build_cfg.get("rust_bin") or service)
    parts = ["target"] + ([triple] if triple else []) + ["release", bin_name]
    src = project_root.joinpath(*parts)
    if not src.exists():
        fail(f"cargo 产物不存在: {src}（检查 build.rust_bin 与 build.rust_target）")
    out = _dist_dir(config) / f"{service}-{version}"
    shutil.copy2(src, out)
    _write_sha256(out)
    log(f"二进制产物已生成: {out}（sha256 已写旁文件）")
```

2) `_run_build_steps` 分支改为：

```python
def _run_build_steps(proj: dict, project_root: Path, version: str,
                     profile: str, env_vars: dict, config: dict = None):
    """根据 build_type 执行对应构建步骤（maven/frontend/go/rust/node/python/none）"""
    build_type = proj.get("build_type", "maven")
    if build_type == "none":
        # 产物由外部投入（build_type=none）：不执行任何构建命令，只做产物校验
        log("构建类型 none：跳过构建，直接使用现成产物")
        return
    if build_type == "frontend":
        _run_frontend_steps(proj, project_root, version, env_vars)
        return
    if build_type == "go":
        _run_go_steps(proj, project_root, version, env_vars, config)
        return
    if build_type == "rust":
        _run_rust_steps(proj, project_root, version, env_vars, config)
        return
    _run_maven_steps(proj, project_root, version, profile, env_vars, config)
```

3) `build_project` 的 artifact 映射与产物段改为：

```python
    artifact = {"static_archive": "archive", "jar_service": "jar",
                "go_binary": "binary", "rust_binary": "binary",
                "node_process": "package", "python_process": "package"
                }.get(deploy_mode, "image")
```

（`node_process`/`python_process` 的映射与 `"package"` 分支由 Task 9/10 消费；本任务先落映射不行为化——`binary` 分支在第 4 步落地。）

产物分发段（原 `if artifact == "jar":` 之后）：

```python
    if artifact in ("binary", "package"):
        return  # 产物已在对应 _run_*_steps 内生成（含 .sha256），无后续打包
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_build_go_rust.py tests/test_build_jar_artifact.py tests/test_build_clean_retry.py tests/test_build_step_env.py -q`
Expected: PASS（既有构建用例零回归）。

- [ ] **Step 5: 提交**

```bash
git add scripts/build.py tests/test_build_go_rust.py
git commit -m "feat(build): go and rust cross-compile artifact branches"
```

---

### Task 5: 观测与编排放行（monitor/log_collector/task_manager/config_routes/exporter/devices.js）

**Files:**
- Modify: `webapp/monitor.py`（821-836、920-934、1056-1059、1086-1090、1133、1149 附近）
- Modify: `webapp/log_collector.py`（258-259、293、312-332、350、418 附近）
- Modify: `webapp/task_manager.py`（331-334、348-363、371 附近）
- Modify: `webapp/routes/config_routes.py`（233-253、328-335、865-934、962 附近）
- Modify: `webapp/exporter.py:67`
- Modify: `webapp/static/js/pages/devices.js`（92-93、97-103、123、149-150）
- Test: `tests/test_config_routes_jar.py`（追加用例）

**Interfaces:**
- Consumes: `base.process_modes()`、Task 1 的 `parse_runtime_config`（带 mode）、Task 2 的 `render_unit`/`onboard_unit` 新签名。
- Produces: 四种新模式获得与 jar 相同的监控探测、journal 日志采集、重启任务、每主机版本 API 与设备页卡片；YAML 导出 runtime_config 对全进程族生效。

- [ ] **Step 1: 写失败测试**

在 `tests/test_config_routes_jar.py` 的 `JarHostsRoutesTest` 类内追加（复用该类既有的 `self.c/self.h/self.pid/self.app/dbm/mock` 测试设施；若类名或设施名不同，以文件内实际为准放入等价类）：

```python
    def test_hosts_accepts_go_binary_service(self):
        r = self.c.post(f"/api/projects/{self.pid}/services", json={
            "name": "gosvc", "image_name": "gosvc", "service_name": "gosvc",
            "deploy_mode": "go_binary", "build_type": "go",
            "runtime_config": {"app_home": "/opt/apps/gosvc"}}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        sid = r.get_json()["id"]
        r = self.c.post(f"/api/projects/{self.pid}/environments",
                        json={"name": "prod"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        r = self.c.post("/api/servers", json={
            "host": "10.0.0.21", "ssh_port": 22, "ssh_user": "root"}, headers=self.h)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        dbm.upsert_host_state(self.app.config["DB_PATH"], self.pid, "prod",
                              "gosvc", "10.0.0.21", "1.0.0", "", 0)
        with mock.patch("webapp.monitor.get_monitor", return_value=None):
            r = self.c.get(
                f"/api/projects/{self.pid}/services/{sid}/hosts?env=prod",
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        row = r.get_json()["hosts"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["version"], "1.0.0")
        self.assertFalse(row["live"])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_config_routes_jar.py -q -k go_binary`
Expected: FAIL（400/404，"该服务不是 jar_service 发布方式"）。

- [ ] **Step 3: 放行 `webapp/routes/config_routes.py`**

1) `create_service`（约 233-253 行）：

```python
    from deploy_strategies.base import process_modes, registered_modes
    from deploy_strategies.process_config import validate_runtime_config

    deploy_mode = body.get("deploy_mode") or "container"
    if deploy_mode not in registered_modes():
        return _err(f"deploy_mode 仅支持 {' / '.join(registered_modes())}")
    is_static = deploy_mode == "static_archive"
    is_process = deploy_mode in process_modes()
    # 镜像/compose 字段仅容器发布必需；静态与进程发布无镜像概念，留空时回退 name
    if not is_static and not is_process:
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

2) `update_service`（约 328-335 行）：

```python
    # 切到进程族或修改运行时配置都要校验：用「即将生效的值」而不是只看本次提交，
    # 否则从 container 切进程模式且不传 runtime_config 时会绕过 app_home 必填检查
    from deploy_strategies.base import process_modes
    from deploy_strategies.process_config import validate_runtime_config
    if mode in process_modes():
        errs = validate_runtime_config(
            fields.get("runtime_config", rec.get("runtime_config")), mode)
        if errs:
            return _err("；".join(errs))
```

3) `_jar_service_or_404` 重命名并放行（约 867-877 行）：

```python
def _process_service_or_404(pid, sid):
    from deploy_strategies.base import process_modes
    from deploy_strategies.process_config import validate_runtime_config
    rec, err = _owned_or_404("services", sid, pid)
    if err:
        return None, err
    mode = rec.get("deploy_mode") or "container"
    if mode not in process_modes():
        return None, _err("该服务不是进程族发布方式")
    errs = validate_runtime_config(rec.get("runtime_config"), mode)
    if errs:
        return None, _err("；".join(errs))
    return rec, None
```

同文件内 3 处调用点（`preview_service_unit`/`onboard_service_unit`/`service_hosts`）同步改名。

4) `_unit_target`（约 880-885 行）：

```python
def _unit_target(rec) -> tuple:
    import service_unit
    from deploy_strategies.process_config import (derive_unit_name,
                                                  parse_runtime_config)
    mode = rec.get("deploy_mode") or "jar_service"
    cfg = parse_runtime_config(rec.get("runtime_config"), mode)
    unit = derive_unit_name(cfg, rec.get("image_name"), rec["name"])
    return unit, service_unit.render_unit(cfg, unit, mode)
```

5) `onboard_service_unit` 内（约 905、918、925 行）：`parse_runtime_config` 导入改自 `deploy_strategies.process_config` 并带 mode（与 `_unit_target` 一致，可直接复用其返回前的 cfg——`_unit_target` 只返回 unit/content，故保留 918 行的独立解析）：

```python
    from deploy_strategies.process_config import parse_runtime_config
    ...
    cfg = parse_runtime_config(rec.get("runtime_config"),
                               rec.get("deploy_mode") or "jar_service")
    ...
    service_unit.onboard_unit(server, unit, content, cfg,
                              rec.get("deploy_mode") or "jar_service")
```

- [ ] **Step 4: 放行 `webapp/monitor.py`**

1) 约 821-836 行（`_probe_envs` 内单元收集）：

```python
        from deploy_strategies.base import process_modes
        from deploy_strategies.process_config import (derive_unit_name,
                                                      parse_runtime_config)
        env_ctxs = []
        process_by_project = {}  # {project_id: [{"unit", "app_home"}, ...]}
        for proj, env in env_tasks:
            services = dbm.list_services(self._db_path, proj["id"], enabled_only=True)
            servers = dbm.list_env_servers(self._db_path, env["id"])
            env_ctxs.append({"proj": proj, "env": env, "services": services,
                             "servers": servers})
            for svc in services:
                if (svc.get("deploy_mode") or "container") not in process_modes():
                    continue
                cfg = parse_runtime_config(svc.get("runtime_config"),
                                           svc.get("deploy_mode"))
                unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"])
                process_by_project.setdefault(proj["id"], []).append(
                    {"unit": unit, "app_home": cfg["app_home"]})
```

并把同函数内 852 行 `for info in jar_by_project.get(...)` 改为 `process_by_project`。

2) `_merge_jar_services` 重命名为 `_merge_process_services`，签名参数 `jar_services` 改 `process_services`，docstring 首行改为「进程族：每主机 systemd 进程视图 → 服务聚合 + 版本写穿」，函数头导入改为：

```python
        from deploy_strategies.process_config import (derive_unit_name,
                                                      parse_runtime_config)
        from . import jar_probe
        for svc in process_services:
            cfg = parse_runtime_config(svc.get("runtime_config"),
                                       svc.get("deploy_mode"))
```

函数体内 `jar_units`/`jar_probe` 探测键名不改（SSH 命令与缓存契约不变）。

3) 约 1056-1059 行（容器跳过分支）：

```python
            from deploy_strategies.base import process_modes
            for svc in services:
                if (svc.get("deploy_mode") or "container") in (
                        "static_archive", *process_modes()):
                    continue  # 静态托管/进程族服务没有容器，走各自的探测分支
```

（import 放到该 for 循环之前的函数体内一次即可。）

4) 约 1086-1090 行：

```python
        from deploy_strategies.base import process_modes
        process_services = [svc for svc in services
                            if (svc.get("deploy_mode") or "container")
                            in process_modes()]
        if process_services:
            self._merge_process_services(env, process_services, servers,
                                         host_results, result, health_hosts)
```

5) 约 1133 行：`(s.get("deploy_mode") or "") == "jar_service"` → `in process_modes()`（同函数体加 `from deploy_strategies.base import process_modes`）；1149 行注释「jar_service 的健康端口」→「进程族服务的健康端口」。

- [ ] **Step 5: 放行 `webapp/log_collector.py`**

1) 约 258-259 行：

```python
        # 项目 → systemd 单元（进程族走 journalctl，与容器共用一条 SSH 通道）
        process_by_project = self._load_process_services()
```

约 293 行 `for info in jar_by_project.get(...)` → `process_by_project`。

2) `_load_jar_services` 重命名为 `_load_process_services`（约 312-332 行）：

```python
    def _load_process_services(self) -> dict:
        """项目 → [{unit, service}]；单元名口径与部署/监控共用 derive_unit_name

        service 为 log_entries.service 的取值（service_name or name），与
        tail/pipeline 的服务名口径一致；unit 仅用于 journalctl -u 与目录名。
        进程族模式来自 process_modes()（注册表自动收集）。
        """
        from deploy_strategies.base import process_modes
        from deploy_strategies.process_config import (derive_unit_name,
                                                      parse_runtime_config)
        modes = process_modes()
        placeholders = ",".join("?" * len(modes))
        conn = dbm.get_conn(self.db_path)
        try:
            rows = conn.execute(
                "SELECT project_id, name, image_name, service_name, runtime_config, "
                f"deploy_mode FROM services "
                f"WHERE enabled=1 AND deploy_mode IN ({placeholders})",
                modes).fetchall()
        finally:
            conn.close()
        result = {}
        for r in rows:
            cfg = parse_runtime_config(r["runtime_config"], r["deploy_mode"])
            unit = derive_unit_name(cfg, r["image_name"], r["name"])
            result.setdefault(r["project_id"], []).append(
                {"unit": unit, "service": r["service_name"] or r["name"]})
        return result
```

3) 约 350 行 docstring「jar_service 的 systemd 单元走 journalctl」→「进程族的 systemd 单元走 journalctl」；约 418 行注释「# systemd 单元（jar_service）」→「# systemd 单元（进程族）」。`jar_units` 作业键名不改（与 monitor 共享主机作业契约）。

- [ ] **Step 6: 放行 `webapp/task_manager.py`（约 331-371 行）**

```python
def _run_restart(db_path: str, task: dict, log_file: str,
                 handle: "_RestartHandle") -> str:
    """执行服务重启：按发布方式并发重启每台服务器（容器走 docker restart，
    进程族走 systemctl restart）并做健康检查。

    返回 "success" / "failed"。日志逐行写入 log_file（每台一行 + 汇总行）。
    复用 monitor 的 SSH 连接池（同进程），凭据合并语义与监控探测一致。
    """
    import shlex
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from . import db as dbm
    from . import monitor as monitor_mod
    from deploy_strategies.base import process_modes

    rec = dbm.get_record(db_path, "services", task["ref_id"])
    env_obj = dbm.get_env_by_name(db_path, task["project_id"], task["env"])
    f = open(log_file, "w", encoding="utf-8")
    try:
        if not rec:
            f.write("[platform] 服务记录不存在，重启中止\n")
            return "failed"
        deploy_mode = rec.get("deploy_mode") or "container"
        if deploy_mode in process_modes():
            from deploy_strategies.process_config import (derive_unit_name,
                                                          parse_runtime_config)
            cfg = parse_runtime_config(rec.get("runtime_config"), deploy_mode)
            if not cfg["app_home"]:
                f.write("[platform] 进程族服务未配置 runtime_config.app_home，重启中止\n")
                return "failed"
            unit = derive_unit_name(cfg, rec.get("image_name"), rec["name"])
            svc_name = ""
        else:
            unit = ""
            svc_name = rec.get("service_name") or ""
        if deploy_mode not in process_modes() and not svc_name:
            f.write(f"[platform] 服务「{rec['name']}」未配置容器名（service_name），重启中止\n")
            return "failed"
```

约 371 行 `target_desc` 改为：

```python
        target_desc = (f"单元 {unit}" if deploy_mode in process_modes()
                       else f"容器名 {svc_name}")
```

（其后 `systemctl restart {unit}` 分支的既有实现按 `deploy_mode == "jar_service"` 判断处，同样改为 `in process_modes()`——用 `grep -n "jar_service" webapp/task_manager.py` 核对无遗漏。）

- [ ] **Step 7: 放行 `webapp/exporter.py`（约 60-70 行）**

模块顶部追加 `from deploy_strategies.base import process_modes`；约 67 行：

```python
        if deploy_mode in process_modes() and s.get("runtime_config"):
            # 进程发布没有镜像/站点字段，运行时参数必须完整导出，
            # 否则从 YAML 重建环境时会静默退回缺省值
            cfg["runtime_config"] = s["runtime_config"]
```

- [ ] **Step 8: 泛化 `webapp/static/js/pages/devices.js`**

1) 约 92-93 行：

```js
      const PROCESS_MODES = ['jar_service', 'go_binary', 'rust_binary',
                             'node_process', 'python_process'];
      const isProcess = PROCESS_MODES.includes(s.deploy_mode);
      const MODE_BADGE = { jar_service: '☕ Java 进程', go_binary: '🐹 Go 程序',
                           rust_binary: '🦀 Rust 程序', node_process: '🟩 Node 进程',
                           python_process: '🐍 Python 进程' };
      const modeBadge = isStatic ? '🗂️ 静态站点' : (MODE_BADGE[s.deploy_mode] || '');
```

2) 约 99/102 行 `isJar` → `isProcess`（`lastOk` 与版本链两处）；注释「纯 Java 进程」→「进程族服务」。
3) 约 123 行 `${isJar ? ` → `${isProcess ? `；130 行闭合 `</div>` 前的 `: isStatic ?` 结构不动。
4) 约 149-150 行 `isJar` → `isProcess`（「主机」按钮与日志按钮 title 两处）。

- [ ] **Step 9: 运行测试**

Run: `python -m pytest tests/test_config_routes_jar.py tests/test_monitor_jar.py tests/test_monitor_host_jar.py tests/test_log_journal.py tests/test_log_collector.py tests/test_restart_task.py tests/test_jar_service.py -q`
Expected: PASS（含新增 go_binary 用例；既有 jar 观测用例零回归）。

Run: `node --check webapp/static/js/pages/devices.js`
Expected: 无输出。

若 `tests/` 内有断言旧文案「该服务不是 jar_service 发布方式」的用例，改为「该服务不是进程族发布方式」。

- [ ] **Step 10: 提交**

```bash
git add webapp/monitor.py webapp/log_collector.py webapp/task_manager.py webapp/routes/config_routes.py webapp/exporter.py webapp/static/js/pages/devices.js tests/test_config_routes_jar.py
git commit -m "feat(web): admit process family modes into monitor, logs, restart, hosts and UI"
```

---

### Task 6: 服务表单接入 go/rust（进程族通用结构）

**Files:**
- Modify: `webapp/static/js/pages/config/config-service-form.js`

**Interfaces:**
- Consumes: 后端 `registered_modes()` 已含 go_binary/rust_binary（Task 3）；`runtime_config` 校验（Task 1）。
- Produces: 表单可选 5 种发布方式（本任务呈现 go/rust 两项的完整结构；node/python 选项由 Task 11 追加，共享组/专属组渲染结构在本任务一次成型）。`PROCESS_BUILD`/`PROCESS_RC_DEFAULT` 常量、共享 rc 字段组、mode 专属字段组、collect 校验。

- [ ] **Step 1: 修改常量与默认数据**

文件顶部 IIFE 内（`const H = window.cfgHelpers;` 之后）加：

```js
  // 进程族：deploy_mode → 固定/缺省 build_type；none 作为外部投入产物的兜底
  const PROCESS_BUILD = { jar_service: 'maven', go_binary: 'go', rust_binary: 'rust',
                          node_process: 'node', python_process: 'python' };
  const PROCESS_RC_DEFAULT = {
    jar_service: { java_bin: '/usr/bin/java', artifact_pattern: 'target/*.jar',
                   readiness: { type: 'http', path: '/actuator/health',
                                timeout: 90, interval: 5 } },
    go_binary: { readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    rust_binary: { readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    node_process: { node_bin: '/usr/bin/node', start_file: '',
                    readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
    python_process: { requirements: 'requirements.txt', start_file: '',
                      readiness: { type: 'process', path: '', timeout: 90, interval: 5 } },
  };
```

- [ ] **Step 2: render() 内模式判定与发布方式下拉（约 45-58 行）**

```js
      const isStatic = (d.deploy_mode || 'container') === 'static_archive';
      const isProc = !!PROCESS_BUILD[d.deploy_mode];
      const isJar = d.deploy_mode === 'jar_service';
      const isGo = d.deploy_mode === 'go_binary';
      const isRust = d.deploy_mode === 'rust_binary';
      if (!d.runtime_config || typeof d.runtime_config !== 'object') d.runtime_config = {};
      const rc = d.runtime_config;
```

下拉（option 列表在 jar 之后追加两项，hint 更新）：

```js
              <option value="jar_service" ${isJar ? 'selected' : ''}>☕ 纯 Java 进程（jar → systemd）</option>
              <option value="go_binary" ${isGo ? 'selected' : ''}>🐹 Go 单二进制（systemd）</option>
              <option value="rust_binary" ${isRust ? 'selected' : ''}>🦀 Rust 单二进制（systemd）</option>
```

f-hint 追加文案：`Go/Rust 单二进制：平台机交叉编译（Go）或本地构建（Rust）出 Linux 可执行文件，按版本放入 app_home/releases 并切换 CURRENT 链接；目标机零运行时依赖。单实例重启期间服务短暂不可用，不承诺零停机。`

镜像短名/Compose/Git 的 `required` 条件（约 70/71/91 行）`!isStatic && !isJar` → `!isStatic && !isProc`。

- [ ] **Step 3: 构建配置区（约 93-117 行）**

```js
      // ② 构建配置（静态=前端；jar=Maven；go/rust=go/rust（可切 none 外部投入）；其余进程族在批 B 扩展）
      const isFrontend = isStatic || (!isProc && d.build_type === 'frontend');
      const btLocked = isStatic || isJar;  // 构建类型跟随发布方式锁定
      const btOptions = (mode) => {
        if (mode === 'jar_service') return `<option value="maven" selected>☕ Java (Maven)</option>`;
        if (mode === 'go_binary') return `<option value="go" ${d.build_type === 'go' ? 'selected' : ''}>🐹 Go 交叉编译</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'rust_binary') return `<option value="rust" ${d.build_type === 'rust' ? 'selected' : ''}>🦀 Cargo 构建</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'node_process') return `<option value="node" ${d.build_type === 'node' ? 'selected' : ''}>🟩 Node (npm)</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        if (mode === 'python_process') return `<option value="python" ${d.build_type === 'python' ? 'selected' : ''}>🐍 Python (wheelhouse)</option><option value="none" ${d.build_type === 'none' ? 'selected' : ''}>📦 外部投入产物（none）</option>`;
        return `<option value="maven" ${!isFrontend && !isStatic ? 'selected' : ''}>☕ Java (Maven)</option>
                <option value="frontend" ${isFrontend || isStatic ? 'selected' : ''}>🌐 Web 前端 (npm/pnpm/yarn)</option>`;
      };
      const hasBuildSteps = isFrontend || (!isProc && d.build_type !== 'none');
      const build = `
        <div class="form-grid">
          <div class="field full">
            <label>构建类型</label>
            <select id="sBuildType" ${btLocked ? 'disabled' : ''}>${btOptions(d.deploy_mode)}</select>
            <div class="f-hint">${isStatic ? '静态站点固定为前端构建（构建产出目录打包 zip，不产镜像）'
              : isJar ? 'Java 项目使用 Maven 构建'
              : isGo ? 'Go：平台机交叉编译 Linux 二进制（CGO_ENABLED=0，GOOS=linux；架构与 ldflags 见构建配置文件）；none = 自行构建后把产物放入平台 dist 目录'
              : isRust ? 'Rust：cargo 构建（Windows 平台机须配置 rust_target，建议 WSL/Linux 构建或选 none）'
              : 'Java 项目使用 Maven 构建；Web 前端项目使用 npm/pnpm/yarn 等命令构建'}</div>
          </div>
          ${isFrontend ? `
          <div class="field full"><label>构建步骤（commands）</label>
            ${H.cmdList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">每个步骤填写工作目录和命令列表（每行一条命令）；支持 \${VERSION} 变量替换；自动探测 npm/pnpm/yarn 可执行路径。</div>
          </div>` : (!isProc ? `
          <div class="field full"><label>构建顺序（build_order）</label>
            ${H.rowList({ id:'sBuildOrder', rows: d.build_order }).html}
            <div class="f-hint">按顺序对每个模块执行 mvn clean {goal} -P{profile}。</div>
          </div>` : `
          <div class="field full"><span class="weak">Go/Rust 构建无需 build_order：产物定位用构建配置（build.go_main / build.rust_bin 等），详见使用文档。</span></div>`)}
          ${isStatic || isProc ? '' : `
          ${H.field({ label:'Docker 上下文目录', tip:'docker build 的上下文', input:`<input id="sDockerCtx" value="${esc(d.docker_context)}" placeholder="如 .">` })}
          ${H.field({ label:'Dockerfile 路径', tip:'相对 docker_context', input:`<input id="sDockerfile" value="${esc(d.dockerfile)}" placeholder="如 Dockerfile">` })}`}
        </div>`;
```

- [ ] **Step 4: 进程族专属配置组（约 128-152 行 javaCfg 泛化为 procCfg）**

```js
      // ②c 进程族专属配置（jar/go/rust/node/python 共享组 + 模式专属组）
      const procCfg = isProc ? `
        <div class="form-grid">
          ${H.field({ label:'应用根目录（app_home）', required:true, input:`<input id="jAppHome" value="${esc(rc.app_home || '')}" placeholder="/opt/apps/${esc(d.name || 'app')}">`, hint:'版本目录 releases/ 与 CURRENT 链接的根；回滚只换链接，不重传产物' })}
          ${isJar ? H.field({ label:'Java 路径（java_bin）', input:`<input id="jJavaBin" value="${esc(rc.java_bin || '/usr/bin/java')}">`, hint:'目标机已安装的 JDK/JRE 绝对路径，平台不负责安装' })
                 + H.field({ label:'JVM 参数（jvm_opts）', input:`<input id="jJvmOpts" value="${esc(rc.jvm_opts || '')}" placeholder="-Xms512m -Xmx1g">` })
                 + H.field({ label:'可执行 jar 匹配（artifact_pattern）', input:`<input id="jArtifactPattern" value="${esc(rc.artifact_pattern || 'target/*.jar')}">`, hint:'命中多个时取体积最大者，并排除 sources/javadoc/original' }) : ''}
          ${d.deploy_mode === 'node_process' ? H.field({ label:'Node 路径（node_bin）', input:`<input id="gNodeBin" value="${esc(rc.node_bin || '/usr/bin/node')}">`, hint:'目标机 node 绝对路径，平台不负责安装' }) : ''}
          ${(d.deploy_mode === 'node_process' || d.deploy_mode === 'python_process') ? H.field({ label:'入口文件（start_file）', required:true, input:`<input id="gStartFile" value="${esc(rc.start_file || '')}" placeholder="${d.deploy_mode === 'node_process' ? 'dist/server.js' : 'main.py'}">`, hint:'CURRENT 下的相对路径；ExecStart 直接执行该入口' }) : ''}
          ${d.deploy_mode === 'python_process' ? H.field({ label:'依赖清单（requirements）', input:`<input id="pRequirements" value="${esc(rc.requirements || 'requirements.txt')}">`, hint:'相对项目根；平台机收 wheel，目标机 venv 离线安装' })
                 + H.field({ label:'目标机 Python 版本（python_version）', input:`<input id="pPythonVersion" value="${esc(rc.python_version || '')}" placeholder="3.11">`, hint:'wheel 解析用；留空取平台机版本并需自行核对目标机一致' }) : ''}
          ${H.field({ label:'启动参数（app_args）', input:`<input id="jAppArgs" value="${esc(rc.app_args || '')}" placeholder="--spring.profiles.active=prod">` })}
          ${H.field({ label:'运行用户（run_user）', input:`<input id="jRunUser" value="${esc(rc.run_user || '')}" placeholder="appuser">`, hint:'留空则由 systemd 以 root 运行；平台不改目标机用户与权限' })}
          ${H.field({ label:'工作目录（work_dir）', input:`<input id="jWorkDir" value="${esc(rc.work_dir || '')}" placeholder="留空默认 {app_home}/CURRENT">` })}
          ${H.field({ label:'systemd 单元名（unit_name）', input:`<input id="jUnit" value="${esc(rc.unit_name || '')}" placeholder="留空默认同服务名">` })}
          ${H.field({ label:'端口（port）', input:`<input id="jPort" type="number" value="${esc(rc.port || '')}" placeholder="8080">`, hint:'HTTP/TCP 就绪方式必填；仅进程存活方式可留空' })}
          ${H.field({ label:'就绪方式（readiness.type）', input:`<select id="jReadinessType">
              <option value="http" ${rc.readiness?.type === 'http' ? 'selected' : ''}>HTTP 健康端点</option>
              <option value="tcp" ${rc.readiness?.type === 'tcp' ? 'selected' : ''}>TCP 端口</option>
              <option value="process" ${(!rc.readiness?.type || rc.readiness?.type === 'process') ? 'selected' : ''}>仅进程存活</option>
            </select>`, hint: isJar ? 'Java 缺省 HTTP + /actuator/health' : 'Go/Rust/Node/Python 缺省仅进程存活；有健康端点请显式选 HTTP/TCP' })}
          ${H.field({ label:'健康路径（readiness.path）', input:`<input id="jReadinessPath" value="${esc(rc.readiness?.path || (isJar ? '/actuator/health' : ''))}">`, hint:'仅 HTTP 方式使用' })}
          ${H.field({ label:'就绪超时/间隔（秒）', input:`<input id="jReadinessTimeout" type="number" value="${esc((rc.readiness && rc.readiness.timeout) || 90)}" style="width:45%"> <input id="jReadinessInterval" type="number" value="${esc((rc.readiness && rc.readiness.interval) || 5)}" style="width:45%">`, hint:'前为总超时，后为轮询间隔' })}
          ${H.field({ label:'停止/启动超时（秒）', input:`<input id="jStopTimeout" type="number" value="${esc(rc.stop_timeout || 60)}" style="width:45%"> <input id="jStartTimeout" type="number" value="${esc(rc.start_timeout || 120)}" style="width:45%">`, hint:'写入 systemd TimeoutStopSec / TimeoutStartSec' })}
          ${H.field({ label:'环境变量文件（env_files）', input:`<textarea id="jEnvFiles" rows="2" placeholder="/opt/apps/app/.env">${esc((rc.env_files || []).join('\n'))}</textarea>`, hint:'每行一个绝对路径，写入 systemd EnvironmentFile；文件缺失会导致单元启动失败' })}
          <div class="hint">单实例发布重启期间服务不可用，平台不承诺零停机。首次发布前请用下方「预览 systemd 单元」核对 ExecStart。</div>
          <button class="btn" id="jUnitPreview" type="button" ${d.id ? '' : 'disabled title="保存后可预览"'}>预览 systemd 单元</button>
          <pre id="jUnitContent" class="weak" style="display:none;white-space:pre-wrap;font-size:12px;margin:0"></pre>
        </div>` : '';
```

分组挂载（约 185-189 行）：jar 分支改为

```js
          : (isProc
              ? H.section({ id:'jar', icon:'⚙️', title:'进程配置', desc:'app_home / systemd 单元 / 就绪方式', open:true, body: procCfg })
              : null),
```

（原 jar 分支图标/标题/描述同步更新；构建分组 desc 中 `isJar` 分支改为 `isProc ? '进程族构建与产物' : ...`。）

- [ ] **Step 5: 事件与 collect()（约 210-226、300-390 行）**

发布方式切换：

```js
      if (dmSel) dmSel.onchange = e => {
        d.deploy_mode = e.target.value;
        const prev = d.runtime_config || {};
        const base = PROCESS_RC_DEFAULT[d.deploy_mode] || {};
        d.runtime_config = { ...base, ...prev,
          readiness: { ...(base.readiness || {}), ...(prev.readiness || {}) } };
        if (d.deploy_mode === 'static_archive') d.build_type = 'frontend';
        else if (PROCESS_BUILD[d.deploy_mode]) d.build_type = PROCESS_BUILD[d.deploy_mode];
        render();
      };
```

`collect()` 关键改动：

```js
      d.deploy_mode = val('sDeployMode') || 'container';
      const isStatic = d.deploy_mode === 'static_archive';
      const isProc = !!PROCESS_BUILD[d.deploy_mode];
      const isJar = d.deploy_mode === 'jar_service';
      ...
      d.build_type = isJar ? 'maven'
        : (isProc ? (val('sBuildType') || PROCESS_BUILD[d.deploy_mode])
                  : (isStatic ? 'frontend' : (val('sBuildType') || 'maven')));
      ...
      d.build_order = (d.build_type === 'frontend'
        ? H.cmdList({ id:'sBuildOrder', rows:[] }).read()
        : (isProc ? [] : H.rowList({ id:'sBuildOrder', rows:[] }).read()));
```

runtime_config 收集（替换原 `if (isJar) {...}` 块）：

```js
      if (isProc) {
        const num = (id, dflt) => {
          const n = Number(val(id));
          return Number.isFinite(n) && n > 0 ? n : dflt;
        };
        const prev = d.runtime_config || {};
        d.runtime_config = {
          ...prev,
          app_home: val('jAppHome').trim(),
          app_args: val('jAppArgs').trim(),
          run_user: val('jRunUser').trim(),
          work_dir: val('jWorkDir').trim(),
          unit_name: val('jUnit').trim(),
          port: Number(val('jPort') || 0),
          stop_timeout: num('jStopTimeout', 60),
          start_timeout: num('jStartTimeout', 120),
          env_files: val('jEnvFiles').split('\n').map(x => x.trim()).filter(Boolean),
          readiness: {
            ...(prev.readiness || {}),
            type: val('jReadinessType') || 'process',
            path: val('jReadinessPath').trim(),
            timeout: num('jReadinessTimeout', 90),
            interval: num('jReadinessInterval', 5),
          },
        };
        if (isJar) {
          d.runtime_config.java_bin = val('jJavaBin').trim() || '/usr/bin/java';
          d.runtime_config.jvm_opts = val('jJvmOpts').trim();
          d.runtime_config.artifact_pattern = val('jArtifactPattern').trim() || 'target/*.jar';
        }
        if (d.deploy_mode === 'node_process') {
          d.runtime_config.node_bin = val('gNodeBin').trim() || '/usr/bin/node';
          d.runtime_config.start_file = val('gStartFile').trim();
        }
        if (d.deploy_mode === 'python_process') {
          d.runtime_config.requirements = val('pRequirements').trim() || 'requirements.txt';
          d.runtime_config.start_file = val('gStartFile').trim();
          d.runtime_config.python_version = val('pPythonVersion').trim();
        }
        if (!d.runtime_config.app_home.startsWith('/')) {
          H.fieldErr('jAppHome', '应用根目录必须为绝对路径（如 /opt/apps/app）'); ok = false;
        } else H.fieldErr('jAppHome', '');
        const rt = d.runtime_config.readiness.type;
        const p = d.runtime_config.port;
        if ((rt === 'http' || rt === 'tcp') && !(Number.isInteger(p) && p > 0 && p < 65536)) {
          H.fieldErr('jPort', 'HTTP/TCP 就绪方式必须填写 1-65535 的端口'); ok = false;
        } else H.fieldErr('jPort', '');
        if ((d.deploy_mode === 'node_process' || d.deploy_mode === 'python_process')
            && (!d.runtime_config.start_file || d.runtime_config.start_file.startsWith('/'))) {
          H.fieldErr('gStartFile', '入口文件必填，且为 CURRENT 下的相对路径'); ok = false;
        } else if (document.getElementById('gStartFile')) H.fieldErr('gStartFile', '');
      }
```

payload（约 268 行）：`runtime_config: isProc ? d.runtime_config : {},`（isProc 以 `d.deploy_mode` 现算）。

必填校验（约 335-341 行）：`isJar` 分支扩展为进程族——`checks` 中非容器非静态只要求服务名（现有 jar 分支行为），保持 `[['sName', 'name', '服务名必填']]` 分支条件改为 `: isProc ? [[...]] : [[...]]`。

- [ ] **Step 6: JS 检查**

Run: `node --check webapp/static/js/pages/config/config-service-form.js`
Expected: 无输出。

- [ ] **Step 7: 提交**

```bash
git add webapp/static/js/pages/config/config-service-form.js
git commit -m "feat(ui): process family options in service form (go/rust)"
```

---

### Task 7: 批 A 文档、全量回归与质量门禁

**Files:**
- Modify: `README.md`（发布方式条目）
- Modify: `docs/usage.md`（发布方式说明与前提）

- [ ] **Step 1: 更新 README**

「发布方式」相关条目（jar_service 说明附近）追加：

```text
Go/Rust 单二进制（`deploy_mode=go_binary`/`rust_binary`）：平台机交叉编译（Go，`CGO_ENABLED=0 GOOS=linux`）或 cargo 构建出 Linux 二进制，与 jar 共用 releases/CURRENT 切换与 systemd 托管；目标机零运行时依赖。Rust 在 Windows 平台机需配置 `build.rust_target`，建议 WSL/Linux 构建或 `build_type=none` 外部投入。
```

- [ ] **Step 2: 更新 docs/usage.md**

前提列表追加：

```md
- Go 构建需平台机安装 Go 工具链（或配置 `build.go_home`）；Rust 构建需 cargo，Windows 平台机交叉编译须配置 `build.rust_target` 与对应 target/linker，否则请用 WSL/Linux 构建或 `build_type=none`；
```

jar/进程观测章节的「进程族」说明补一句：Go/Rust 服务与 jar 同样具备实时状态、journal 日志与每主机版本视图。

- [ ] **Step 3: 全量测试与质量门禁**

Run: `python -m pytest -q`
Expected: 全部 PASS（ruff/node 缺失时相关步骤 SKIP）。

Run: `python scripts/quality_gate.py`
Expected: 无 FAIL。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/usage.md
git commit -m "docs: document go/rust binary deploy modes"
```

---

### Task 8: archive 传输路径（目录产物）

**Files:**
- Modify: `deploy_strategies/process_service.py`（transfer 增加 archive 分支 + `_transfer_archive`）
- Test: `tests/test_process_base.py`（新建）

**Interfaces:**
- Consumes: Task 2 的基类钩子（`post_extract`/`artifact_kind`）。
- Produces: `ProcessServiceStrategy.transfer` 支持 `artifact_kind="archive"`——上传 `releases/{ver}.tar.gz` → sha256 复核 → 解压 `releases/{ver}.tmp` → `post_extract` 钩子 → 写 `.sha256` → 原子 `mv` 就位 → 删压缩包；幂等（`{ver}/.sha256` 匹配跳过；不匹配视为残骸 `rm -rf` 重建）。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_process_base.py`：

```python
"""archive 传输路径：上传→复核→解压→钩子→原子就位→清理，以及幂等/残骸重建"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies.process_config import sha256_file
from deploy_strategies.process_service import ProcessServiceStrategy

APP_HOME = "/opt/apps/nsvc"


class FakeRemote:
    def __init__(self, rules=None, try_exec_ok=None):
        self.host = "10.0.0.1"
        self.exec_calls = []
        self.uploaded = []
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


class _ArchiveSvc(ProcessServiceStrategy):
    """测试用 archive 形态（不 @register，避免污染 process_modes）"""
    name = "_test_archive"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None


def make_deployer(tmp_dir: str, version="1.0.0") -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "nsvc"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0}}
    d.ssh_cfg = {}
    # 解析按 node_process 形态（archive 家族），避免未注册模式解析为空
    d.proj_cfg = {"deploy_mode": "node_process", "image_name": "nsvc",
                  "runtime_config": {"app_home": APP_HOME,
                                     "start_file": "server.js"}}
    d.dist_dir = Path(tmp_dir)
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_pkg(deployer: Deployer, version=None, payload=b"tar-bytes"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"nsvc-{version}.tar.gz"
    p.write_bytes(payload)
    p.with_name(p.name + ".sha256").write_text(sha256_file(p) + "\n",
                                               encoding="utf-8")
    return p


class ArchiveTransferTest(unittest.TestCase):
    def _svc(self, tmp):
        d = make_deployer(tmp)
        return _ArchiveSvc(d), d

    def test_upload_extract_finalize(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        extracted = []
        s.post_extract = lambda server, tmp_dir, ver: extracted.append(tmp_dir)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "",  # .sha256 标记缺失 → 不跳过
                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded[0][1], f"{APP_HOME}/releases/1.0.0.tar.gz")
        joined = "\n".join(remote.exec_calls)
        self.assertIn(f"tar -xzf {APP_HOME}/releases/1.0.0.tar.gz", joined)
        self.assertIn(f"mv -Tf {APP_HOME}/releases/1.0.0.tmp {APP_HOME}/releases/1.0.0", joined)
        self.assertIn("echo ", joined)  # .sha256 写入
        self.assertIn(f"rm -f {APP_HOME}/releases/1.0.0.tar.gz", joined)
        self.assertEqual(extracted, [f"{APP_HOME}/releases/1.0.0.tmp"])

    def test_skips_when_marker_matches(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        remote = FakeRemote(rules={".sha256": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        self.assertEqual(remote.uploaded, [])

    def test_rebuilds_on_marker_mismatch(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        pkg = make_pkg(d)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "deadbeef",  # 旧标记与本次不符
                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        joined = "\n".join(remote.exec_calls)
        self.assertIn(f"rm -rf {APP_HOME}/releases/1.0.0.tmp", joined)
        self.assertIn(f"rm -rf {APP_HOME}/releases/1.0.0 &&", joined)

    def test_upload_sha_mismatch_fails(self):
        tmp = tempfile.mkdtemp(prefix="arch_xfer_")
        s, d = self._svc(tmp)
        make_pkg(d)
        remote = FakeRemote(
            try_exec_ok={"test -f": False},
            rules={"cat ": "", "sha256sum": "0" * 64})
        with self.assertRaises(RuntimeError):
            s.transfer(remote)
        self.assertFalse(any("mv -Tf" in c for c in remote.exec_calls))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_process_base.py -q`
Expected: FAIL（transfer 无 archive 分支，直接走 file 路径 → `remote_entry()` 返回 None 拼出错误路径或 NotImplementedError）。

- [ ] **Step 3: 实现 archive 分支**

`deploy_strategies/process_service.py` 的 `transfer` 在 `local.exists()` 检查之后插入：

```python
        if self.artifact_kind == "archive":
            return self._transfer_archive(server, ver, local)
```

并在 `transfer` 之后新增：

```python
    def _transfer_archive(self, server, ver: str, local: Path) -> bool:
        """目录产物（tar.gz）传输：上传复核 → 解压暂存 → post_extract → 原子就位 → 清理

        幂等：远端 {ver}/.sha256 与本次一致则跳过；不一致说明上次发布中断留下残骸，
        rm -rf 重建（该目录的唯一写入者就是本次 transfer，不违反不可变约定）。
        """
        local_sha = sha256_file(local)
        base = f"{self.jars_dir}/{ver}"
        remote_tar = f"{base}.tar.gz"
        tmp_dir = f"{base}.tmp"
        server.exec(f"mkdir -p {shlex.quote(self.jars_dir)}", timeout=15)
        marker = server.exec(
            f"cat {shlex.quote(base + '/.sha256')} 2>/dev/null || true",
            timeout=15).strip()
        if marker == local_sha:
            log(f"[{server.host}] 远端已有相同产物，跳过上传: {ver}")
            return True
        log(f"[{server.host}] 上传 {self.artifact_label} {self.artifact_name(ver)}"
            f"（{local.stat().st_size / 1024 / 1024:.1f} MB）-> {remote_tar}")
        server.sftp_put_with_progress(
            str(local), remote_tar, label=self.artifact_name(ver),
            streams=getattr(self.d, "upload_streams", 1))
        remote_sha = server.exec(
            f"sha256sum {shlex.quote(remote_tar)} | awk '{{print $1}}'", timeout=60).strip()
        if remote_sha != local_sha:
            raise RuntimeError(
                f"[{server.host}] 上传后校验和不一致（本地 {local_sha[:12]}…，"
                f"远端 {remote_sha[:12]}…）")
        # ① 解压到暂存目录并写入压缩包校验和（解压失败时旧版本原状，未触达 CURRENT）
        server.exec(
            f"rm -rf {shlex.quote(tmp_dir)} && mkdir -p {shlex.quote(tmp_dir)} && "
            f"tar -xzf {shlex.quote(remote_tar)} -C {shlex.quote(tmp_dir)} && "
            f"echo {shlex.quote(local_sha)} > {shlex.quote(tmp_dir + '/.sha256')}",
            timeout=300)
        # ② 形态钩子（node 修 node_modules/.bin 执行位等）
        self.post_extract(server, tmp_dir, ver)
        # ③ 原子就位并清理压缩包（同文件系统 mv 原子；残骸先清除）
        server.exec(
            f"rm -rf {shlex.quote(base)} && "
            f"mv -Tf {shlex.quote(tmp_dir)} {shlex.quote(base)} && "
            f"rm -f {shlex.quote(remote_tar)}",
            timeout=120)
        return True
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_process_base.py tests/test_jar_service.py tests/test_go_rust_service.py -q`
Expected: PASS（file 路径与 jar/Go/Rust 零回归）。

- [ ] **Step 5: 提交**

```bash
git add deploy_strategies/process_service.py tests/test_process_base.py
git commit -m "feat(strategy): archive artifact transfer path for process family"
```

---

### Task 9: node_process 策略与 build 的 node 分支

**Files:**
- Create: `deploy_strategies/node_process.py`
- Modify: `deploy_strategies/__init__.py`
- Modify: `scripts/build.py`
- Test: `tests/test_node_python_service.py`（新建，node 部分）+ `tests/test_build_node_python.py`（新建，node 部分）

**Interfaces:**
- Consumes: Task 8 的 archive 传输、Task 6 的表单结构。
- Produces: 注册名 `node_process`；build_type `node` 的 `_run_node_steps(proj, project_root, version, env_vars, config)`（`npm ci` → 可选 `npm run build` → `npm prune --omit=dev` → stdlib tarfile 打包）；产物 `{service}-{version}.tar.gz` + `.sha256`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_node_python_service.py`（本任务只写 node 部分；python 用例在 Task 10 追加到同文件）：

```python
"""node_process/python_process 策略（本文件 Task 9 先覆盖 node；Task 10 追加 python）"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import _REGISTRY
from deploy_strategies.node_process import NodeProcessStrategy
from deploy_strategies.process_config import sha256_file

APP_HOME = "/opt/apps/nsvc"


class FakeRemote:
    def __init__(self, rules=None, try_exec_ok=None):
        self.host = "10.0.0.1"
        self.exec_calls = []
        self.uploaded = []
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


def make_deployer(tmp_dir: str, version="1.0.0", runtime_config=None) -> Deployer:
    d = Deployer.__new__(Deployer)
    d.project = "nsvc"
    d.version = version
    d.previous_version = None
    d.env = "test"
    d.config = {"deploy": {"canary_wait": 0}}
    d.ssh_cfg = {}
    d.proj_cfg = {"deploy_mode": "node_process", "image_name": "nsvc",
                  "runtime_config": runtime_config or {
                      "app_home": APP_HOME, "start_file": "dist/server.js"}}
    d.dist_dir = Path(tmp_dir)
    d.upload_streams = 1
    d.container_modified = False
    d.nacos = mock.Mock()
    d.nacos.configured = False
    d.nacos.group = ""
    return d


def make_pkg(deployer: Deployer, version=None, payload=b"tar-bytes"):
    version = version or deployer.version
    p = Path(deployer.dist_dir) / f"nsvc-{version}.tar.gz"
    p.write_bytes(payload)
    p.with_name(p.name + ".sha256").write_text(sha256_file(p) + "\n",
                                               encoding="utf-8")
    return p


class NodeRegistryTest(unittest.TestCase):
    def test_registered_and_capabilities(self):
        self.assertIn("node_process", _REGISTRY)
        s = create_strategy("node_process", make_deployer(tempfile.mkdtemp()))
        self.assertIsInstance(s, NodeProcessStrategy)
        self.assertEqual(s.artifact_kind, "archive")
        self.assertTrue(s.supports_nacos and s.supports_canary)

    def test_node_probe(self):
        s = create_strategy("node_process", make_deployer(tempfile.mkdtemp()))
        r = FakeRemote()
        s.runtime_probe(r)
        self.assertTrue(any("command -v /usr/bin/node" in c for c in r.exec_calls))


class NodeTransferTest(unittest.TestCase):
    def test_archive_upload_and_chmod_hook(self):
        tmp = tempfile.mkdtemp(prefix="node_svc_")
        d = make_deployer(tmp)
        pkg = make_pkg(d)
        s = create_strategy("node_process", d)
        remote = FakeRemote(try_exec_ok={"test -f": False},
                            rules={"cat ": "",
                                   "sha256sum": sha256_file(pkg)})
        self.assertTrue(s.transfer(remote))
        joined = "\n".join(remote.exec_calls)
        self.assertIn("tar -xzf", joined)
        # 解压后、就位前修 .bin 执行位（Windows 打包丢 +x 的保险）
        self.assertTrue(any("chmod -R u+x" in c and "node_modules/.bin" in c
                            for c in remote.exec_calls))
        self.assertIn(f"{APP_HOME}/releases/1.0.0/node_modules/.bin", joined)


if __name__ == "__main__":
    unittest.main()
```

新建 `tests/test_build_node_python.py`（node 部分）：

```python
"""build.py 的 node 分支：npm ci/build/prune 序列与 tar.gz 产物（Task 10 追加 python 用例）"""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build as build_mod


def base_config(root: Path) -> dict:
    return {"deploy": {"project_root": str(root), "dist_dir": str(root / "out"),
                       "docker_compose_path": "", "remote_tmp_dir": "/tmp/x"},
            "registry": {"host": "h", "namespace": "n"},
            "deploy_mode": "direct"}


def make_node_project(root: Path, with_build=False) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "index.js").write_text("console.log(1)\n", encoding="utf-8")
    scripts = {"start": "node src/index.js"}
    if with_build:
        scripts["build"] = "echo built"
    (root / "package.json").write_text(
        '{"name":"nsvc","version":"0.0.0","scripts":' +
        str(scripts).replace("'", '"') + '}', encoding="utf-8")
    (root / "package-lock.json").write_text("{}", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")


class NodeBuildTest(unittest.TestCase):
    def test_steps_and_package_content(self):
        root = Path(tempfile.mkdtemp(prefix="node_build_"))
        make_node_project(root, with_build=True)
        calls = []
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "find_node", return_value="node"), \
                mock.patch.object(build_mod, "_find_tool", return_value="npm"):
            build_mod._run_node_steps({"image_name": "nsvc"}, root, "1.0.0", {},
                                      base_config(root))
        self.assertEqual(calls[0][:2], ["npm", "ci"])
        self.assertEqual(calls[1][:3], ["npm", "run", "build"])
        self.assertEqual(calls[2][:2], ["npm", "prune"])
        out = root / "out" / "nsvc-1.0.0.tar.gz"
        self.assertTrue(out.exists())
        self.assertTrue((root / "out" / "nsvc-1.0.0.tar.gz.sha256").exists())
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        self.assertTrue(any(n == "./src/index.js" or n.endswith("src/index.js")
                            for n in names))
        self.assertFalse(any(".git/" in n or n.endswith("/.git/HEAD")
                             for n in names))
        self.assertFalse(any(n.endswith("/.env") or n == "./.env" for n in names))

    def test_no_build_script_skips_run_build(self):
        root = Path(tempfile.mkdtemp(prefix="node_build_"))
        make_node_project(root, with_build=False)
        calls = []
        with mock.patch.object(build_mod, "run",
                               side_effect=lambda cmd, cwd, **kw: calls.append(cmd)), \
                mock.patch.object(build_mod, "find_node", return_value="node"), \
                mock.patch.object(build_mod, "_find_tool", return_value="npm"):
            build_mod._run_node_steps({"image_name": "nsvc"}, root, "1.0.0", {},
                                      base_config(root))
        self.assertEqual(len(calls), 2)  # ci + prune


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_node_python_service.py tests/test_build_node_python.py -q`
Expected: FAIL（`ModuleNotFoundError: deploy_strategies.node_process`、`no attribute '_run_node_steps'`）。

- [ ] **Step 3: 实现 `deploy_strategies/node_process.py`**

```python
"""Node 进程发布策略（deploy_mode: node_process，Linux + systemd）

产物为目录包（源码 + node_modules，npm ci/prune 后 tar.gz，见 build.py node 分支），
transfer 解压平铺到 releases/{ver}/；ExecStart = {node_bin} CURRENT/{start_file}。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class NodeProcessStrategy(ProcessServiceStrategy):
    name = "node_process"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None  # archive：入口由 runtime_config.start_file 决定

    def runtime_probe(self, server):
        server.exec(f"command -v {shlex.quote(self.cfg['node_bin'])} >/dev/null",
                    timeout=15)

    def post_extract(self, server, tmp_dir: str, version: str):
        # Windows 平台机打 tar 丢失执行位：解压后就位前修 npm .bin 软链/脚本的 +x
        server.exec(
            f"chmod -R u+x {shlex.quote(tmp_dir + '/node_modules/.bin')} "
            f"2>/dev/null || true", timeout=60)
```

`deploy_strategies/__init__.py` 追加：

```python
from . import node_process  # noqa: F401 — 触发 @register NodeProcessStrategy
```

- [ ] **Step 4: 实现 build.py 的 node 分支**

`_run_build_steps` 在 rust 分支后追加：

```python
    if build_type == "node":
        _run_node_steps(proj, project_root, version, env_vars, config)
        return
```

`_run_rust_steps` 之后新增：

```python
_TAR_EXCLUDE_PARTS = {".git", ".env", "__pycache__", ".wheels-build",
                      "node_modules/.cache", ".venv", "venv"}


def _tar_filter(out_path: Path):
    """tarfile filter：排除版本库/密钥/缓存/临时目录；Windows 打包的执行位由远端 chmod 兜底"""
    def _f(ti: tarfile.TarInfo):
        name = ti.name.replace("\\", "/")
        parts = name.split("/")
        if any(seg in _TAR_EXCLUDE_PARTS for seg in parts):
            return None
        if any(seg.startswith(".env") for seg in parts):
            return None
        if name.endswith(".log"):
            return None
        if out_path and name.rstrip("/") in (out_path.name,):
            return None  # 防 dist 目录在项目根内时自包含
        return ti
    return _f


def _make_node_package(proj: dict, project_root: Path, version: str,
                       config: dict) -> Path:
    """项目目录（含 node_modules）→ dist/{service}-{version}.tar.gz + .sha256"""
    service = proj.get("image_name") or proj.get("name") or "app"
    out = _dist_dir(config) / f"{service}-{version}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(project_root, arcname=".", filter=_tar_filter(out))
    _write_sha256(out)
    log(f"Node 产物已生成: {out}（含 node_modules，sha256 已写旁文件）")
    return out


def _run_node_steps(proj: dict, project_root: Path, version: str,
                    env_vars: dict, config: dict):
    """Node 后端构建：npm ci（全量）→ 可选 npm run build → npm prune --omit=dev → tar.gz"""
    npm = _find_tool("npm", "npm_home", config,
                     "未找到 npm：请安装 Node.js 并加入 PATH（需含 npm）")
    pkg = project_root / "package.json"
    if not pkg.exists():
        fail(f"未找到 package.json: {pkg}")
    if not (project_root / "package-lock.json").exists():
        fail("未找到 package-lock.json：npm ci 需要锁定依赖（在项目内提交 lock 文件）")
    log(f"使用 npm: {npm}")
    run([npm, "ci"], project_root, env=env_vars, timeout=900)
    scripts = {}
    try:
        scripts = (json.loads(pkg.read_text(encoding="utf-8"))
                   .get("scripts") or {})
    except ValueError:
        fail(f"package.json 解析失败: {pkg}")
    if scripts.get("build"):
        run([npm, "run", "build"], project_root, env=env_vars, timeout=900)
    run([npm, "prune", "--omit=dev"], project_root, env=env_vars, timeout=900)
    _make_node_package(proj, project_root, version, config)
```

文件头部 import 区确认已有 `import tarfile`、`import json`（若无则补）。`build_project` 无需改动（Task 4 已落 `"package"` 映射与 return）。

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/test_node_python_service.py tests/test_build_node_python.py tests/test_process_base.py tests/test_build_go_rust.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add deploy_strategies/node_process.py deploy_strategies/__init__.py scripts/build.py tests/test_node_python_service.py tests/test_build_node_python.py
git commit -m "feat: node_process strategy and npm build branch"
```

---

### Task 10: python_process 策略（venv 生命周期）与 build 的 python 分支

**Files:**
- Create: `deploy_strategies/python_process.py`
- Modify: `deploy_strategies/__init__.py`
- Modify: `scripts/build.py`
- Test: `tests/test_node_python_service.py`（追加 python 用例）+ `tests/test_build_node_python.py`（追加 python 用例）

**Interfaces:**
- Consumes: Task 8 的 archive 传输、Task 2 的 `ensure_runtime` 钩子（switch/rollback 在启动前调用）。
- Produces: 注册名 `python_process`；`ensure_runtime(server, version)`——venv 缺失则 `python3 -m venv` 创建，requirements 哈希变化则 `venv/bin/pip install --no-index --find-links releases/{v}/wheels -r ...` 离线重装并更新 `venv/.req_hash`；build_type `python` 的 `_run_python_steps`（`pip download --platform manylinux2014_x86_64 --only-binary=:all:` 收 wheel）+ `_make_python_package`（源码 + requirements + wheels/ 打 tar.gz）。

- [ ] **Step 1: 写失败测试**

`tests/test_node_python_service.py` 追加：

```python
from deploy_strategies.python_process import PythonProcessStrategy  # noqa: E402


def make_py_deployer(tmp_dir: str, version="1.0.0") -> Deployer:
    d = make_deployer(tmp_dir, version=version)
    d.proj_cfg = {"deploy_mode": "python_process", "image_name": "psvc",
                  "runtime_config": {"app_home": APP_HOME,
                                     "start_file": "main.py",
                                     "python_version": "3.11"}}
    d.project = "psvc"
    return d


class PythonRegistryTest(unittest.TestCase):
    def test_registered_and_family_complete(self):
        self.assertIn("python_process", _REGISTRY)
        from deploy_strategies.base import process_modes
        self.assertEqual(process_modes(),
                         ["go_binary", "jar_service", "node_process",
                          "python_process", "rust_binary"])

    def test_python_probe_checks_ensurepip(self):
        s = create_strategy("python_process", make_py_deployer(tempfile.mkdtemp()))
        r = FakeRemote()
        s.runtime_probe(r)
        joined = "\n".join(r.exec_calls)
        self.assertIn("command -v python3", joined)
        self.assertIn("python3 -m ensurepip", joined)


class PythonVenvTest(unittest.TestCase):
    def test_first_deploy_creates_venv_and_installs(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": False},
                       rules={"sha256sum": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        joined = "\n".join(r.exec_calls)
        self.assertIn(f"python3 -m venv {APP_HOME}/venv", joined)
        self.assertIn("--no-index", joined)
        self.assertIn(f"--find-links {APP_HOME}/releases/1.0.0/wheels", joined)
        self.assertIn(f"-r {APP_HOME}/releases/1.0.0/requirements.txt", joined)
        self.assertIn(".req_hash", joined)

    def test_hash_match_skips_install(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(rules={"sha256sum": "a" * 64, "cat ": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        self.assertFalse(any("pip install" in c for c in r.exec_calls))
        self.assertFalse(any("python3 -m venv" in c for c in r.exec_calls))

    def test_requirements_change_reinstalls_without_recreate(self):
        tmp = tempfile.mkdtemp(prefix="py_svc_")
        s = create_strategy("python_process", make_py_deployer(tmp))
        r = FakeRemote(try_exec_ok={"pyvenv.cfg": True},
                       rules={"sha256sum": "b" * 64, "cat ": "a" * 64})
        s.ensure_runtime(r, "1.0.0")
        joined = "\n".join(r.exec_calls)
        self.assertNotIn("python3 -m venv", joined)
        self.assertIn("pip install", joined)
        self.assertIn("rm -f", joined)  # 先清旧标记再写新标记
```

`tests/test_build_node_python.py` 追加：

```python
class PythonBuildTest(unittest.TestCase):
    def test_pip_download_and_package(self):
        root = Path(tempfile.mkdtemp(prefix="py_build_"))
        (root / "requirements.txt").write_text("fastapi==0.111.0\n",
                                               encoding="utf-8")
        (root / "main.py").write_text("print('x')\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, cwd, env=None, timeout=None, **kw):
            calls.append(cmd)
            # 模拟 pip download 产出 wheel
            out_dir = Path(cmd[cmd.index("-d") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "fastapi-0.111.0-py3-none-any.whl").write_bytes(b"w")

        rc = {"app_home": "/opt/apps/psvc", "start_file": "main.py",
              "python_version": "3.11"}
        with mock.patch.object(build_mod, "run", side_effect=fake_run), \
                mock.patch.object(build_mod, "_find_tool", return_value="python"):
            build_mod._run_python_steps({"image_name": "psvc",
                                         "runtime_config": rc},
                                        root, "1.0.0", {}, base_config(root))
        cmd = calls[0]
        self.assertIn("pip", cmd)
        self.assertIn("--platform", cmd)
        self.assertIn("manylinux2014_x86_64", cmd)
        self.assertIn("--only-binary=:all:", cmd)
        self.assertIn("3.11", cmd)
        out = root / "out" / "psvc-1.0.0.tar.gz"
        self.assertTrue(out.exists())
        with tarfile.open(out, "r:gz") as tf:
            names = tf.getnames()
        self.assertTrue(any("wheels/" in n for n in names))
        self.assertTrue(any(n.endswith("requirements.txt") for n in names))
        self.assertTrue(any(n.endswith("main.py") for n in names))

    def test_missing_requirements_fails(self):
        root = Path(tempfile.mkdtemp(prefix="py_build_"))
        rc = {"app_home": "/opt/apps/psvc", "start_file": "main.py"}
        with self.assertRaises(SystemExit):
            build_mod._run_python_steps({"image_name": "psvc",
                                         "runtime_config": rc},
                                        root, "1.0.0", {}, base_config(root))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_node_python_service.py tests/test_build_node_python.py -q`
Expected: 新增用例 FAIL（`ModuleNotFoundError: deploy_strategies.python_process`、`no attribute '_run_python_steps'`）。

- [ ] **Step 3: 实现 `deploy_strategies/python_process.py`**

```python
"""Python 进程发布策略（deploy_mode: python_process，Linux + systemd）

产物为目录包（源码 + requirements.txt + wheels/，wheelhouse 离线依赖，见 build.py
python 分支），transfer 解压平铺到 releases/{ver}/；venv 是 app_home 级共享资产：
ExecStart = {app_home}/venv/bin/python CURRENT/{start_file}。
目标机前提：python3 + python3-venv（含 ensurepip）；安装全程 --no-index 不联网。
"""
import shlex

from .base import register
from .process_service import ProcessServiceStrategy


@register
class PythonProcessStrategy(ProcessServiceStrategy):
    name = "python_process"
    artifact_kind = "archive"
    artifact_label = "压缩包"

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.tar.gz"

    def remote_entry(self) -> str:
        return None  # archive：入口由 runtime_config.start_file 决定

    def runtime_probe(self, server):
        # ensurepip 缺失（Debian 未装 python3-venv）会让 venv 创建在切换期才失败，
        # 提前到预检拦下并给出可操作的报错
        server.exec("command -v python3 >/dev/null", timeout=15)
        server.exec("python3 -m ensurepip --version >/dev/null 2>&1 || "
                    "(echo '目标机缺少 python3-venv/ensurepip，请先安装（如 apt install python3-venv）' "
                    "&& exit 1)", timeout=15)

    def ensure_runtime(self, server, version: str):
        """venv 保障：缺失则创建；requirements 哈希变化则离线重装（switch/rollback 共用）

        哈希以 releases/{ver}/requirements.txt 的远端 sha256 为准；venv 位于
        app_home/venv（跨版本共享），.req_hash 记录上次安装的依赖指纹。
        """
        ver = version or self.d.version
        venv = f"{self.app_home}/venv"
        marker = f"{venv}/.req_hash"
        req_remote = f"{self.jars_dir}/{ver}/{self.cfg['requirements'] or 'requirements.txt'}"
        wheels_dir = f"{self.jars_dir}/{ver}/wheels"
        server.exec(f"mkdir -p {shlex.quote(self.jars_dir)}", timeout=15)
        if not server.try_exec(f"test -f {shlex.quote(venv + '/pyvenv.cfg')}", timeout=15):
            log(f"[{server.host}] 创建独立 venv: {venv}")
            server.exec(f"python3 -m venv {shlex.quote(venv)}", timeout=180)
            server.exec(f"rm -f {shlex.quote(marker)}", timeout=15)
        remote_hash = server.exec(
            f"sha256sum {shlex.quote(req_remote)} | awk '{{print $1}}'",
            timeout=15).strip()
        current = server.exec(
            f"cat {shlex.quote(marker)} 2>/dev/null || true", timeout=15).strip()
        if current == remote_hash:
            return
        log(f"[{server.host}] venv 依赖与 requirements 不一致，离线安装"
            f"（wheels: {wheels_dir}）")
        server.exec(
            f"{shlex.quote(venv + '/bin/pip')} install --no-index "
            f"--find-links {shlex.quote(wheels_dir)} -r {shlex.quote(req_remote)}",
            timeout=900)
        server.exec(f"echo {shlex.quote(remote_hash)} > {shlex.quote(marker)}",
                    timeout=15)
```

`deploy_strategies/__init__.py` 追加：

```python
from . import python_process  # noqa: F401 — 触发 @register PythonProcessStrategy
```

- [ ] **Step 4: 实现 build.py 的 python 分支**

`_run_build_steps` 在 node 分支后追加：

```python
    if build_type == "python":
        _run_python_steps(proj, project_root, version, env_vars, config)
        return
```

`_run_node_steps` 之后新增：

```python
def _make_python_package(proj: dict, project_root: Path, version: str,
                         config: dict, wheels_dir: Path, req_name: str) -> Path:
    """源码 + requirements + wheels/ → dist/{service}-{version}.tar.gz + .sha256"""
    service = proj.get("image_name") or proj.get("name") or "app"
    out = _dist_dir(config) / f"{service}-{version}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(project_root, arcname=".", filter=_tar_filter(out))
        tf.add(str(wheels_dir), arcname="wheels")
        tf.add(str(project_root / req_name), arcname=req_name)
    _write_sha256(out)
    log(f"Python 产物已生成: {out}（含 wheels 离线依赖，sha256 已写旁文件）")
    return out


def _run_python_steps(proj: dict, project_root: Path, version: str,
                      env_vars: dict, config: dict):
    """wheelhouse：平台机 pip download manylinux wheel，目标机 venv 离线安装（不联网）"""
    rc = proj.get("runtime_config") or {}
    req_name = str(rc.get("requirements") or "requirements.txt")
    req_path = project_root / req_name
    if not req_path.exists():
        fail(f"未找到依赖清单: {req_path}（runtime_config.requirements 相对项目根）")
    py_ver = str(rc.get("python_version") or
                 f"{sys.version_info.major}.{sys.version_info.minor}")
    if not rc.get("python_version"):
        log(f"警告: 未配置 runtime_config.python_version，按平台机 "
            f"{py_ver} 解析 wheel——请核对目标机 python3 版本一致")
    wheels_dir = project_root / ".wheels-build"
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir, ignore_errors=True)
    wheels_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "download", "-r", str(req_path),
           "-d", str(wheels_dir), "--platform", "manylinux2014_x86_64",
           "--only-binary=:all:", "--implementation", "cp",
           "--python-version", py_ver]
    log(f"收集 manylinux wheel（python {py_ver}）：{req_path}")
    run(cmd, project_root, env=env_vars, timeout=1800)
    _make_python_package(proj, project_root, version, config, wheels_dir, req_name)
```

注意：`build.py` 头部确认已有 `import sys`、`import shutil`、`import tarfile`（缺则补）；`_tar_filter` 已排除 `.wheels-build` 自身，但 `_make_python_package` 里 `tf.add(project_root)` 之后单独 `tf.add(wheels_dir)`——filter 对 `arcname="wheels"` 的成员同样生效（成员名不含 `.wheels-build`，不会被误排除）。

- [ ] **Step 5: 运行测试**

Run: `python -m pytest tests/test_node_python_service.py tests/test_build_node_python.py tests/test_process_base.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add deploy_strategies/python_process.py deploy_strategies/__init__.py scripts/build.py tests/test_node_python_service.py tests/test_build_node_python.py
git commit -m "feat: python_process strategy with wheelhouse and venv lifecycle"
```

---

### Task 11: 表单追加 node/python 选项

**Files:**
- Modify: `webapp/static/js/pages/config/config-service-form.js`

**Interfaces:**
- Consumes: Task 6 的 `PROCESS_BUILD`/`PROCESS_RC_DEFAULT`/`btOptions`/`procCfg`/`collect` 结构（node/python 字段组与校验已在 Task 6 一次成型，本任务只补选项与提示）。
- Produces: 表单五种发布方式齐全。

- [ ] **Step 1: 发布方式下拉追加选项**

Task 6 的 rust option 之后：

```js
              <option value="node_process" ${d.deploy_mode === 'node_process' ? 'selected' : ''}>🟩 Node 进程（systemd）</option>
              <option value="python_process" ${d.deploy_mode === 'python_process' ? 'selected' : ''}>🐍 Python 进程（wheelhouse + venv）</option>
```

f-hint 追加：`Node 进程：平台机 npm 构建（ci/build/prune）打 tar.gz（含 node_modules），目标机仅需 node；Python 进程：平台机收集 manylinux wheel，目标机 app_home/venv 离线安装，需预装 python3-venv。`

- [ ] **Step 2: JS 检查**

Run: `node --check webapp/static/js/pages/config/config-service-form.js`
Expected: 无输出。

- [ ] **Step 3: 提交**

```bash
git add webapp/static/js/pages/config/config-service-form.js
git commit -m "feat(ui): node and python process options in service form"
```

---

### Task 12: 批 B 文档、全量回归与质量门禁

**Files:**
- Modify: `README.md`
- Modify: `docs/usage.md`
- Modify: `webapp/templates/screen.html`（如 devices/jar_hosts 脚本版本号需递增：`?v=` +1）

- [ ] **Step 1: 更新 README**

jar_service 段落之后追加：

```text
Node 进程（`deploy_mode=node_process`）：平台机 `npm ci` →（可选 build）→ `npm prune --omit=dev` 打 tar.gz（含 node_modules），远端解压到 `releases/{version}/` 后切换 `CURRENT`；目标机只需 node。Python 进程（`deploy_mode=python_process`）：平台机收集 manylinux wheel，目标机在 `app_home/venv` 离线安装（不联网、不写系统目录），requirements 变化时自动重装，回滚到依赖不同的旧版本时先重装再重启。
```

- [ ] **Step 2: 更新 docs/usage.md**

前提列表追加：

```md
- Node 进程发布：目标机需预装 node（`runtime_config.node_bin`）；Python 进程发布：目标机需预装 python3 与 python3-venv（含 ensurepip），平台不代装；
- Python 依赖经 wheelhouse 离线安装：无 Linux wheel、需现场编译的 sdist 依赖会在平台构建期报错并列出包名，请改用提供 manylinux wheel 的版本；
```

观测章节补：Node/Python 服务与 jar 一样具备实时状态、journal 日志与每主机版本视图；Python 回滚触发 venv 重装时任务日志可见耗时。

- [ ] **Step 3: 前端版本号**

若 `devices.js`/`jar_hosts.js` 本批有行为改动且浏览器有缓存诉求，把 `webapp/templates/screen.html` 对应 `<script src>` 的 `?v=` 递增 1。

- [ ] **Step 4: 全量测试与质量门禁**

Run: `python -m pytest -q`
Expected: 全部 PASS（ruff/node 缺失时相关步骤 SKIP）。

Run: `python scripts/quality_gate.py`
Expected: 无 FAIL。

Run: `node tests/js/jar_hosts_test.js`
Expected: `jar hosts render tests passed`。

- [ ] **Step 5: 提交**

```bash
git add README.md docs/usage.md webapp/templates/screen.html
git commit -m "docs: document node/python process deploy modes"
```

---

## 验收清单（实现者自检，对照 spec §9）

- [ ] `python -m pytest -q` 全绿；`python scripts/quality_gate.py` 无 FAIL。
- [ ] `process_modes()` 恰为 `["go_binary", "jar_service", "node_process", "python_process", "rust_binary"]`，来自注册表自动收集。
- [ ] jar_service 既有用例（策略/编排/路由/监控/日志/单元）零改动语义通过——基类抽取未改变任何命令序列与文案语义。
- [ ] 四种新模式的五阶段命令序列断言通过（假 RemoteServer）：switch 只含「停单元→换链接→（ensure_runtime）→起单元→写标记」，零 docker 调用。
- [ ] archive 路径：上传→sha256 复核→解压→post_extract→原子 mv→清理；幂等与残骸重建用例通过。
- [ ] Python：venv 创建/requirements 变更重装/回滚重装（ensure_runtime 被 switch 与 rollback 调用）用例通过；目标机命令不含任何联网安装。
- [ ] 构建：go/rust/node/python 四分支产物 + `.sha256` 落 dist，docker 调用计数为 0；Windows 缺 rust_target 报错引导。
- [ ] 监控/journal/hosts API/重启任务对四种新模式放行；`service_host_state` 写穿与快照语义不变。
- [ ] 表单五种模式显隐/联动正确（go/rust/node/python 的 build_type 联动与 none 兜底可选）。
- [ ] 文档包含停机窗口提示，不承诺零停机。
