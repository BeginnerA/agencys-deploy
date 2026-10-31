# WAR/Tomcat 独立实例托管实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `war_tomcat` 发布方式，在 Linux + systemd 上以固定独立 Tomcat 实例完成 WAR 的接入、发布、严格恢复、回滚、重启和观测。

**Architecture:** 配置与渲染保持纯函数；远端锁、暂存提交、停止确认由 `remote_operation` 生成并执行严格命令；`WarTomcatLifecycle` 统一发布/恢复/回滚/重启，返回结构化结果；WAR 策略只做能力契约和编排接入。旧七种方式保持原代码路径。

**Tech Stack:** Python 3、Flask、SQLite、Paramiko、systemd、Tomcat 9/10.1/11、vanilla JS、unittest/pytest。

**Spec:** `docs/superpowers/specs/2026-10-30-war-tomcat-deploy-design.md`

## Global Constraints

- 只实施 WAR/Tomcat；Windows 服务与 Kubernetes/Helm 不在本计划内。
- 所有改动留在工作树；不执行 `git add/commit/push/tag`，不发布版本。
- 不连接真实目标机做开发验证；真机验收单独安排并记录“未验证”直至完成。
- 目标机仅 Linux + systemd；不安装 JDK/Tomcat，不建账号，不改 sudo/防火墙。
- 实例配置固定，WAR 版本化；应用回滚不恢复配置、JDK/Tomcat 或数据库。
- WAR 成功必须通过应用 HTTP 健康；`systemctl active` 不能替代。
- 首次业务 Connector 仅 HTTP IPv4 `0.0.0.0`；不启用 AJP、Manager、远程 shutdown。
- stop 失败、旧包缺失、恢复后健康失败、SSH 失联均不得报告成功。
- 旧七种 `deploy_mode` 的构建产物、健康检查、版本号与回滚语义不得改变。
- 敏感值只引用 `env_files` 路径；预览、审计、异常和结果不得包含环境文件内容、私钥、密码或锁令牌。
- 每个任务先写失败测试，再实现，再运行该任务测试；任务结束用 `git diff` 检查范围，不提交。

## File Structure

| 文件 | 职责 |
| --- | --- |
| `deploy_strategies/tomcat_config.py` | WAR 配置解析/校验、Context/健康路径、配置指纹、Tomcat 文件与 unit 渲染 |
| `deploy_strategies/remote_operation.py` | 实例路径、owner/gate 锁、令牌校验、暂存提交、严格停止/检查、管理员隔离 |
| `deploy_strategies/tomcat_health.py` | SSH `direct-tcpip` HTTP 单次探活与有界轮询 |
| `deploy_strategies/tomcat_instance.py` | 模板读取、实例文件清单、归属清单、预览摘要、接入与配置事务 |
| `deploy_strategies/tomcat_lifecycle.py` | 逐主机快照、发布、恢复、回滚、重启、结构化结果 |
| `deploy_strategies/war_tomcat.py` | `war_tomcat` 注册、能力标志、产物契约、编排适配 |
| `scripts/build.py` | WAR 唯一候选选择、ZIP 安全检查、dist 产物 |
| `deploy.py` | 严格主机生命周期分支，隔离旧回滚语义 |
| `webapp/task_manager.py` | WAR 重启走严格生命周期 |
| `webapp/monitor.py`、`webapp/log_collector.py` | WAR 应用健康、版本/配置状态、journal unit |
| `webapp/routes/tomcat_routes.py` | 预览、接入、受控停机更新、管理员恢复 |
| `webapp/routes/config_routes.py` | 主机行与单元预览的 WAR 字段透传 |
| `webapp/static/js/*`、`webapp/templates/*` | WAR 表单、每主机状态、维护/恢复确认 |
| `tests/test_tomcat_*.py`、`tests/test_build_war.py` | 自动化验收 |
| `docs/`、`scripts/package_portable.py` | 使用说明、模块完整性 |

---

### Task 1: WAR 配置解析、校验与健康路径

**Files:**
- Create: `deploy_strategies/tomcat_config.py`
- Test: `tests/test_tomcat_config.py`

**Interfaces:**
- Produces: `parse_war_config(runtime_config: dict) -> dict`，字段见下。
- Produces: `validate_war_config(runtime_config: dict) -> list[str]`，返回全部错误，不抛异常。
- Produces: `context_filename(context_path: str) -> str`。
- Produces: `health_request_path(cfg: dict) -> str`。
- Produces: `derive_war_unit(cfg: dict, service: str) -> str`。

> 实施补强（审查后已落地，以此为准）：`parse_war_config` 对任意输入不抛异常
> （非字符串标量按空值处理）；`validate_war_config` 对存在的字符串字段做类型检查
> （`app_home/tomcat_home/java_home/run_user/unit_name/artifact_pattern/context_path`
> 与 `readiness.path/body_contains`），拒绝 `root`、路径任一段为 `.`/`..`（含结尾）、
> 控制字符（`ord<32` 或 127）、`app_home` 与 `tomcat_home`/`java_home` 任一互相包含；
> `unit_name` 仅在显式设置时校验；bool 不作为整数接受；`jvm_opts/env_files` 返回副本。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from deploy_strategies.tomcat_config import (context_filename, health_request_path,
                                            parse_war_config, validate_war_config,
                                            derive_war_unit)

VALID = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "artifact_pattern": "target/*.war",
    "jvm_opts": ["-Xms256m", "-Dfile.encoding=UTF-8"],
    "env_files": ["/etc/smartops/orders.env"],
    "readiness": {"path": "/health", "body_contains": "UP",
                  "timeout": 90, "interval": 3},
}

class WarConfigTest(unittest.TestCase):
    def test_minimal_valid(self):
        self.assertEqual(validate_war_config(VALID), [])
        cfg = parse_war_config(VALID)
        self.assertEqual(cfg["context_path"], "/orders")
        self.assertEqual(health_request_path(cfg), "/orders/health")
        self.assertEqual(cfg["unit_name"], "orders-war")

    def test_accepts_json_string_from_db_or_import(self):
        import json
        raw = json.dumps(VALID)
        self.assertEqual(validate_war_config(raw), [])
        self.assertEqual(parse_war_config(raw)["port"], 18080)

    def test_root_context(self):
        cfg = parse_war_config(dict(VALID, context_path="/"))
        self.assertEqual(cfg["context_path"], "")
        self.assertEqual(context_filename(""), "ROOT.xml")
        self.assertEqual(health_request_path(cfg), "/health")

    def test_nested_context(self):
        cfg = parse_war_config(dict(VALID, context_path="/team/orders/"))
        self.assertEqual(context_filename(cfg["context_path"]), "team#orders.xml")

    def test_rejects_paths_and_health_injection(self):
        bad = dict(VALID, app_home="/opt/../etc",
                   env_files=["/etc/x\nExecStart=/bin/sh"],
                   readiness={"path": "/health?token=1"})
        errors = "；".join(validate_war_config(bad))
        self.assertIn("app_home", errors)
        self.assertIn("env_files", errors)
        self.assertIn("readiness.path", errors)

    def test_rejects_unsafe_context_and_options(self):
        bad = dict(VALID, context_path="/a b", jvm_opts=["-Dcatalina.base=/tmp"])
        errors = "；".join(validate_war_config(bad))
        self.assertIn("context_path", errors)
        self.assertIn("jvm_opts", errors)

    def test_rejects_unit_and_port(self):
        bad = dict(VALID, unit_name="bad/unit", port=80)
        errors = "；".join(validate_war_config(bad))
        self.assertIn("unit_name", errors)
        self.assertIn("port", errors)

    def test_rejects_scalar_lists(self):
        # CLI/YAML 可能把数组写成标量；不能静默拆成字符逐个进 argv
        bad = dict(VALID, jvm_opts="-Xms256m", env_files="/etc/x.env")
        errors = "；".join(validate_war_config(bad))
        self.assertIn("jvm_opts 必须是数组", errors)
        self.assertIn("env_files 必须是数组", errors)

    def test_derive_unit_without_explicit_name(self):
        data = dict(VALID)
        data.pop("unit_name")
        cfg = parse_war_config(data)
        self.assertEqual(derive_war_unit(cfg, "orders"), "smartops-orders-war")
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_config.py -v`
Expected: FAIL，`ModuleNotFoundError: deploy_strategies.tomcat_config`

- [ ] **Step 3: 实现配置模块**

```python
"""WAR/Tomcat 配置：纯解析、校验与命名，不做 SSH。"""
import json
import re

_SAFE_PATH = re.compile(r"^/(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+$")
_UNIT = re.compile(r"^[A-Za-z0-9_.@-]{1,120}$")
_RUN_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_CONTEXT_SEG = re.compile(r"^[A-Za-z0-9_-]+$")
_FORBIDDEN_OPTS = ("-cp", "-classpath", "@", "-Dcatalina.base=", "-Dcatalina.home=",
                   "-Djava.io.tmpdir=", "-Djava.util.logging.config.file=")


def normalize_context_path(value: str) -> str:
    value = (value or "").strip()
    if value in ("", "/"):
        return ""
    return "/" + value.strip("/")


def _as_dict(raw) -> dict:
    """DB/导入/CLI 可能给 JSON 字符串；非法输入按空配置处理，交给校验报错。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def parse_war_config(runtime_config: dict) -> dict:
    rc = _as_dict(runtime_config)
    readiness = _as_dict(rc.get("readiness"))

    def as_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    unit = (rc.get("unit_name") or "").strip()
    if unit.endswith(".service"):
        unit = unit[:-8]
    return {
        "app_home": (rc.get("app_home") or "").strip().rstrip("/"),
        "tomcat_home": (rc.get("tomcat_home") or "").strip().rstrip("/"),
        "java_home": (rc.get("java_home") or "").strip().rstrip("/"),
        "run_user": (rc.get("run_user") or "").strip(),
        "unit_name": unit,
        "port": as_int(rc.get("port"), 0),
        "context_path": normalize_context_path(rc.get("context_path")),
        "artifact_pattern": (rc.get("artifact_pattern") or "target/*.war").strip(),
        "jvm_opts": _as_list(rc.get("jvm_opts")),
        "env_files": _as_list(rc.get("env_files")),
        "stop_timeout": as_int(rc.get("stop_timeout"), 60),
        "start_timeout": as_int(rc.get("start_timeout"), 120),
        "readiness": {
            "type": "http",
            "path": (readiness.get("path") or "").strip(),
            "expected_status": as_int(readiness.get("expected_status"), 200),
            "body_contains": readiness.get("body_contains") or "",
            "timeout": as_int(readiness.get("timeout"), 120),
            "interval": as_int(readiness.get("interval"), 5),
        },
    }


def health_request_path(cfg: dict) -> str:
    ctx = cfg["context_path"]
    path = cfg["readiness"]["path"]
    return f"{ctx}{path}" if ctx else path


def context_filename(context: str) -> str:
    if not context:
        return "ROOT.xml"
    return context.strip("/").replace("/", "#") + ".xml"


def derive_war_unit(cfg: dict, service: str) -> str:
    return cfg["unit_name"] or f"smartops-{service}-war"


def _safe_path(value: str) -> bool:
    return bool(_SAFE_PATH.match(value or "")) and "/../" not in value and "/./" not in value


def validate_war_config(runtime_config: dict) -> list[str]:
    raw = _as_dict(runtime_config)
    cfg = parse_war_config(raw)
    errors = []
    for key in ("jvm_opts", "env_files"):
        if key in raw and not isinstance(raw[key], list):
            errors.append(f"{key} 必须是数组")
    for field in ("app_home", "tomcat_home", "java_home"):
        if not _safe_path(cfg[field]) or cfg[field] == "/":
            errors.append(f"{field} 必须是安全的绝对路径")
    if cfg["app_home"] and cfg["tomcat_home"] and (
            cfg["app_home"] == cfg["tomcat_home"]
            or cfg["app_home"].startswith(cfg["tomcat_home"] + "/")
            or cfg["tomcat_home"].startswith(cfg["app_home"] + "/")):
        errors.append("app_home 与 tomcat_home 不得相同或互相包含")
    if not _RUN_USER.match(cfg["run_user"]):
        errors.append("run_user 必须是已存在的非 root 账号名")
    if not _UNIT.match(derive_war_unit(cfg, "service")):
        errors.append("unit_name 只允许字母、数字、下划线、点、@ 和连字符")
    if not 1024 <= cfg["port"] <= 65535:
        errors.append("port 必须在 1024–65535")
    if cfg["context_path"]:
        for seg in cfg["context_path"].strip("/").split("/"):
            if not _CONTEXT_SEG.match(seg) or seg in (".", ".."):
                errors.append("context_path 每段只允许字母、数字、下划线、连字符")
                break
    pattern = cfg["artifact_pattern"]
    if not pattern.endswith(".war") or pattern.startswith("/") or ".." in pattern:
        errors.append("artifact_pattern 必须是相对项目根的 *.war 模式")
    if not isinstance(cfg["jvm_opts"], list) or not all(isinstance(x, str) for x in cfg["jvm_opts"]):
        errors.append("jvm_opts 必须是字符串数组")
    else:
        for opt in cfg["jvm_opts"]:
            if (not opt or "\n" in opt or "\x00" in opt or "%" in opt
                    or any(opt.startswith(x) for x in _FORBIDDEN_OPTS)):
                errors.append("jvm_opts 含换行、插值或被平台接管的参数")
                break
    if not isinstance(cfg["env_files"], list) or not all(_safe_path(p) for p in cfg["env_files"]):
        errors.append("env_files 必须是安全的绝对路径数组")
    if not isinstance(cfg["stop_timeout"], int) or cfg["stop_timeout"] <= 0:
        errors.append("stop_timeout 必须是正整数")
    if not isinstance(cfg["start_timeout"], int) or cfg["start_timeout"] <= 0:
        errors.append("start_timeout 必须是正整数")
    r = cfg["readiness"]
    path = r["path"]
    if (not path.startswith("/") or "//" in path or ".." in path
            or any(ch in path for ch in ("?", "#", "\\", "\n", "\r", "%"))):
        errors.append("readiness.path 必须是相对应用根的绝对路径，不能含查询、片段或穿越")
    if r["expected_status"] != 200:
        errors.append("首版 readiness.expected_status 仅支持 200")
    if "\n" in str(r["body_contains"]):
        errors.append("readiness.body_contains 不能含换行")
    if r["timeout"] <= 0 or r["interval"] <= 0 or r["interval"] > r["timeout"]:
        errors.append("readiness.timeout 必须大于 interval 且均为正数")
    return errors
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_tomcat_config.py -v`
Expected: PASS（7 tests）

- [ ] **Step 5: 检查范围**

Run: `git diff --stat`
Expected: 只新增 `tomcat_config.py` 与测试；不提交。

---

### Task 2: Tomcat 配置与 unit 渲染、指纹

**Files:**
- Modify: `deploy_strategies/tomcat_config.py`
- Test: `tests/test_tomcat_config_render.py`

**Interfaces:**
- Consumes: Task 1 的 `parse_war_config`。
- Produces: `render_instance_files(cfg: dict, service: str, templates: dict) -> dict[str, str]`，键为 `instance/` 下相对路径。
- Produces: `render_war_unit(cfg: dict, service: str) -> str`。
- Produces: `config_digest(cfg: dict, files: dict[str, str], template_version: str = TEMPLATE_VERSION, unit_content: str = "") -> str`。
  指纹按 spec §178 覆盖规范化配置（排除构建输入 `artifact_pattern`）、模板版本、受管文件与 unit 内容；调用方必须传入渲染后的 unit 文本。
- `server.xml` 使用 `unpackWARs="false"`：外部 WAR docBase 若允许展开，Tomcat 会把展开副本写到只读的 releases 目录，运行账号无法启动；直接以 WAR 运行同时消除旧展开缓存类问题。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from deploy_strategies.tomcat_config import (config_digest, parse_war_config,
                                            render_instance_files, render_war_unit)

CFG = parse_war_config({
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "jvm_opts": ["-Xms256m"], "env_files": ["/etc/smartops/orders.env"],
    "readiness": {"path": "/health"},
})
TEMPLATES = {"web.xml": "<web-app/>", "catalina.properties": "a=1\n",
             "logging.properties": "handlers=x\n"}

class RenderTest(unittest.TestCase):
    def test_server_xml_has_single_http_connector_and_safe_host(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        server = files["conf/server.xml"]
        self.assertIn('port="18080"', server)
        self.assertIn('address="0.0.0.0"', server)
        self.assertIn('autoDeploy="false"', server)
        self.assertIn('deployOnStartup="true"', server)
        self.assertIn('unpackWARs="true"', server)
        self.assertIn('<Server port="-1"', server)
        for forbidden in ("AJP", "Manager", "HostManager", "8080"):
            self.assertNotIn(forbidden, server)

    def test_context_filename_and_docbase(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        ctx = files["conf/Catalina/localhost/orders.xml"]
        self.assertIn('docBase="/opt/apps/orders/CURRENT/app.war"', ctx)
        self.assertIn('reloadable="false"', ctx)
        self.assertNotIn('path="', ctx)

    def test_templates_required(self):
        with self.assertRaises(ValueError):
            render_instance_files(CFG, "orders", {"web.xml": "<web-app/>"})

    def test_unit_direct_java_and_no_restart(self):
        unit = render_war_unit(CFG, "orders")
        self.assertIn("/usr/lib/jvm/java-17/bin/java", unit)
        self.assertIn("-Dcatalina.home=/opt/tomcat", unit)
        self.assertIn("-Dcatalina.base=/opt/apps/orders/instance", unit)
        self.assertIn("org.apache.catalina.startup.Bootstrap start", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("User=appuser", unit)
        self.assertNotIn("catalina.sh", unit)

    def test_digest_changes_with_file_or_config(self):
        files = render_instance_files(CFG, "orders", TEMPLATES)
        a = config_digest(CFG, files, "tomcat-template-v1")
        b = config_digest(dict(CFG, port=18081), files, "tomcat-template-v1")
        c = config_digest(CFG, dict(files, **{"conf/web.xml": "<web-app>1</web-app>"}),
                          "tomcat-template-v1")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_config_render.py -v`
Expected: FAIL，`ImportError: cannot import name 'render_instance_files'`

- [ ] **Step 3: 实现渲染与指纹**

```python
import hashlib
import shlex
from xml.sax.saxutils import escape

TEMPLATE_VERSION = "tomcat-template-v1"


def _sd(value: str) -> str:
    return shlex.quote(value)


def _xml_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def render_instance_files(cfg: dict, service: str, templates: dict) -> dict:
    missing = [k for k in ("web.xml", "catalina.properties", "logging.properties")
               if not (templates.get(k) or "").strip()]
    if missing:
        raise ValueError(f"Tomcat 模板缺失: {', '.join(missing)}")
    app = cfg["app_home"]
    server = f"""<?xml version="1.0" encoding="UTF-8"?>
<Server port="-1" shutdown="SHUTDOWN">
  <Listener className="org.apache.catalina.startup.VersionLoggerListener"/>
  <Listener className="org.apache.catalina.core.JreMemoryLeakPreventionListener"/>
  <Listener className="org.apache.catalina.core.ThreadLocalLeakPreventionListener"/>
  <Service name="Catalina">
    <Connector port="{cfg['port']}" protocol="org.apache.coyote.http11.Http11NioProtocol"
               address="0.0.0.0" connectionTimeout="20000"/>
    <Engine name="Catalina" defaultHost="localhost">
      <Host name="localhost" appBase="{_xml_attr(app)}/instance/webapps"
            unpackWARs="true" autoDeploy="false" deployOnStartup="true"
            copyXML="false"/>
    </Engine>
  </Service>
</Server>
"""
    from .tomcat_config import context_filename
    ctx = f"""<?xml version="1.0" encoding="UTF-8"?>
<Context docBase="{_xml_attr(app)}/CURRENT/app.war" reloadable="false" copyXML="false"/>
"""
    return {
        "conf/server.xml": server,
        "conf/web.xml": templates["web.xml"],
        "conf/catalina.properties": templates["catalina.properties"],
        "conf/logging.properties": templates["logging.properties"],
        f"conf/Catalina/localhost/{context_filename(cfg['context_path'])}": ctx,
        "lib/.keep": "",
        "webapps/.keep": "",
        "work/.keep": "",
        "temp/.keep": "",
        "logs/.keep": "",
    }


def render_war_unit(cfg: dict, service: str) -> str:
    app = cfg["app_home"]
    base = f"{app}/instance"
    cp = f"{cfg['tomcat_home']}/bin/bootstrap.jar:{cfg['tomcat_home']}/bin/tomcat-juli.jar"
    argv = [
        f"{cfg['java_home']}/bin/java",
        f"-Dcatalina.home={cfg['tomcat_home']}",
        f"-Dcatalina.base={base}",
        f"-Djava.io.tmpdir={base}/temp",
        f"-Djava.util.logging.config.file={base}/conf/logging.properties",
        "-Djava.util.logging.manager=org.apache.juli.ClassLoaderLogManager",
        "-classpath", cp,
        *cfg["jvm_opts"],
        "org.apache.catalina.startup.Bootstrap", "start",
    ]
    exec_start = " ".join(_sd(x) for x in argv)
    lines = [
        "[Unit]", f"Description={service}", "After=network.target", "",
        "[Service]", "Type=simple", f"User={cfg['run_user']}",
        f"WorkingDirectory={base}", f"ExecStart={exec_start}",
        "SuccessExitStatus=143", "KillMode=control-group", "Restart=no",
        f"TimeoutStopSec={cfg['stop_timeout']}",
        f"TimeoutStartSec={cfg['start_timeout']}",
        *[f"EnvironmentFile={p}" for p in cfg["env_files"]],
        "", "[Install]", "WantedBy=multi-user.target", "",
    ]
    return "\n".join(line for line in lines if line)


def config_digest(cfg: dict, files: dict, template_version: str = TEMPLATE_VERSION,
                  unit_content: str = "") -> str:
    payload = {
        "config": cfg,
        "files": {k: files[k] for k in sorted(files)},
        "template_version": template_version,
        "unit_sha256": hashlib.sha256(unit_content.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

`tomcat_config.py` 顶部已有 `import json`；Task 2 只需追加 `hashlib`、`shlex`、`xml.sax.saxutils`，不要在文件中间重复 import。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_tomcat_config.py tests/test_tomcat_config_render.py -v`
Expected: PASS（12 tests）

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat && git status --short`
Expected: 仅新增/修改本任务文件与测试。

---

### Task 3: WAR 构建产物与唯一候选

**Files:**
- Modify: `scripts/build.py:748-786`（仅新增分支与调用）
- Test: `tests/test_build_war.py`

**Interfaces:**
- Produces: `_select_war(proj: dict, project_root: Path) -> Path`。
- Produces: `_make_war_artifact(proj: dict, project_root: Path, version: str, config: dict) -> Path`。
- Produces: `_validate_war_zip(path: Path) -> None`，坏包抛 `ValueError`。
- `build_project` 对 `war_tomcat` 使用 `artifact="war"`，构建后调用 `_make_war_artifact` 并返回。

- [ ] **Step 1: 写失败测试**

```python
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import build


def _war(path: Path, name="WEB-INF/web.xml"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, "<web-app/>")
    return path


class BuildWarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proj = {"name": "orders", "image_name": "orders",
                     "deploy_mode": "war_tomcat", "build_order": [],
                     "runtime_config": {"artifact_pattern": "target/*.war"}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_candidate(self):
        expected = _war(self.root / "target" / "orders-1.0.0.war")
        self.assertEqual(build._select_war(self.proj, self.root), expected)

    def test_multiple_candidates_rejected(self):
        _war(self.root / "target" / "a.war")
        _war(self.root / "target" / "b.war")
        with self.assertRaises(SystemExit):
            build._select_war(self.proj, self.root)

    def test_missing_rejected(self):
        with self.assertRaises(SystemExit):
            build._select_war(self.proj, self.root)

    def test_make_artifact_sha(self):
        _war(self.root / "target" / "orders.war")
        dist = self.root / "dist"
        out = build._make_war_artifact(self.proj, self.root, "1.0.0",
                                       {"deploy": {"dist_dir": str(dist)}})
        self.assertEqual(out.name, "orders-1.0.0.war")
        self.assertTrue(out.with_name(out.name + ".sha256").is_file())

    def test_reject_traversal_member(self):
        bad = self.root / "target" / "bad.war"
        _war(bad, "../../etc/passwd")
        with self.assertRaises(ValueError):
            build._validate_war_zip(bad)
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_build_war.py -v`
Expected: FAIL，`AttributeError: module 'build' has no attribute '_select_war'`

- [ ] **Step 3: 实现 WAR 选择与制品**

在 `scripts/build.py` 的 `_jar_search_dirs` 之后追加：

```python
def _validate_war_zip(path: Path) -> None:
    """安全检查 WAR：可读 ZIP、成员不穿越、无符号链接。"""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise ValueError(f"WAR 成员路径非法: {info.filename}")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"WAR 不允许符号链接: {info.filename}")
    except zipfile.BadZipFile as e:
        raise ValueError(f"不是有效 WAR/ZIP: {path}") from e


def _select_war(proj: dict, project_root: Path) -> Path:
    pattern = (proj.get("runtime_config") or {}).get("artifact_pattern") or "target/*.war"
    candidates = []
    for base in _jar_search_dirs(proj, project_root):
        for p in base.glob(pattern):
            if p.is_file() and p.suffix == ".war":
                resolved = p.resolve()
                if resolved not in candidates:
                    candidates.append(resolved)
    if not candidates:
        fail(f"未找到 WAR（pattern={pattern}，基准目录 {project_root}）")
    if len(candidates) > 1:
        names = "、".join(str(p) for p in sorted(candidates))
        fail(f"WAR 候选不唯一（{len(candidates)} 个），请收窄 artifact_pattern: {names}")
    _validate_war_zip(candidates[0])
    log(f"选中构建产物: {candidates[0]}")
    return candidates[0]


def _make_war_artifact(proj: dict, project_root: Path, version: str, config: dict) -> Path:
    src = _select_war(proj, project_root)
    service = proj.get("image_name") or proj.get("name") or "app"
    dist_dir = Path(config["deploy"].get("dist_dir", "dist"))
    if not dist_dir.is_absolute():
        dist_dir = Path(__file__).resolve().parent.parent / dist_dir
    dist_dir.mkdir(parents=True, exist_ok=True)
    out = dist_dir / f"{service}-{version}.war"
    shutil.copy2(src, out)
    h = hashlib.sha256()
    with open(out, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    out.with_name(out.name + ".sha256").write_text(h.hexdigest() + "\n", encoding="utf-8")
    log(f"WAR 产物已生成: {out}（sha256={h.hexdigest()[:12]}…）")
    return out
```

在 `build_project` 中修改映射并追加分支：

```python
artifact = {"static_archive": "archive", "jar_service": "jar",
            "go_binary": "binary", "rust_binary": "binary",
            "node_process": "package", "python_process": "package",
            "war_tomcat": "war"}.get(deploy_mode, "image")
```

```python
if artifact == "war":
    _make_war_artifact(proj, project_root, version, config)
    return
```

- [ ] **Step 4: 运行通过并确认 JAR 回归**

Run: `python -m pytest tests/test_build_war.py tests/test_build_jar_artifact.py -v`
Expected: PASS；JAR 仍按最大候选选择，既有断言不变。

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff -- scripts/build.py tests/test_build_war.py`

---

### Task 4: 远端锁、暂存提交与严格停止命令

**Files:**
- Create: `deploy_strategies/remote_operation.py`
- Test: `tests/test_remote_operation.py`

**Interfaces:**
- Produces: `LOCK_ROOT = "/var/lib/smartops/tomcat"`。
- Produces: `lock_paths(instance_id: str) -> dict`。
- Produces: `acquire(server, instance_id, token, operation, phase) -> None`；占用时抛 `LockBusy`。
- Produces: `release(server, instance_id, token, result: dict) -> None`。
- Produces: `guarded(server, instance_id, token, command: str, timeout: int = 120,
  *, stdin_rest: str = "") -> str`——内部执行 `server.exec` 并返回 stdout；
  令牌经 `stdin_data` 送达（脚本先 `read` 再 `grep -F -q -f -` 校验归属），
  命令文本与平台日志中不得出现令牌；`stdin_rest` 供 `release` 把
  `last_result.json` 内容与令牌同批传入。

> 实施补强（审查后已落地，以此为准）：`acquire`/`release` 的 owner/结果负载
> 一律经 stdin 写入（base64 上命令行同样算泄漏）；`__cause__` 链必须整体脱敏；
> `inspect` 的不可解析分支只返回 `raw_digest/raw_len`，不回传原文；
> `commit_staged` 校验 `version` 白名单 `^[A-Za-z0-9._-]+$`，校验和行只认
> `^[0-9a-f]{64}  ` 精确形式；`release` 用 `&&` 保证结果写失败时保留 owner。
> 下文 Step 3 的参考实现是**旧形态**（令牌在命令行），以补强为准。
- Produces: `commit_staged(server, instance_id, token, version, incoming_dir, sha256) -> str`，返回 `"created"` 或 `"reused"`。
- Produces: `stop_and_confirm(server, instance_id, token, unit) -> None`。
- Produces: `set_phase(server, instance_id, token, phase) -> None`（best-effort，
  只更新持有同一令牌的 owner 的阶段文件，供监控显示变更阶段）。
- Produces: `inspect(server, instance_id) -> dict`，含 `owner`、`phase`、`last_result`。
- Produces: `revoke(server, instance_id, confirm_digest) -> None`。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from deploy_strategies import remote_operation as ro

class FakeRemote:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}
    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        for key, err in self.fail.items():
            if key in cmd:
                raise RuntimeError(err)
        return ""

class RemoteOperationTest(unittest.TestCase):
    def test_guarded_checks_token_and_gate(self):
        server = FakeRemote(stdout="ok")
        out = ro.guarded(server, "abc123", "deadbeef",
                         "systemctl start x.service")
        self.assertEqual(out, "ok")
        cmd = _one_call(server)
        self.assertIn("flock -x", cmd)
        self.assertIn("IFS= read -r __TOK || exit 71", cmd)
        self.assertIn('grep -F -q -f - "$OWNER"', cmd)
        self.assertIn("|| exit 73", cmd)
        self.assertIn("systemctl start x.service", cmd)
        self.assertNotIn("deadbeef", cmd)  # 令牌不进命令文本（exec 会 log 整条命令）

    def test_acquire_writes_owner_and_maps_busy(self):
        server = FakeRemote()
        ro.acquire(server, "abc123", "deadbeef", "deploy", "preflight")
        self.assertIn("test ! -e", server.calls[0])
        self.assertIn("base64 -d", server.calls[0])
        with self.assertRaises(ro.LockBusy):
            ro.acquire(FakeRemote({"test ! -e": "LOCKED"}), "abc123", "t", "deploy", "x")

    def test_release_writes_result_then_removes_owner(self):
        server = FakeRemote()
        ro.release(server, "abc123", "deadbeef", {"operation_status": "ok"})
        joined = "\n".join(server.calls)
        self.assertIn("last-result.json", joined)
        self.assertIn("rm -f", joined)
        self.assertIn("owner.json", joined)

    def test_commit_reuses_same_sha_rejects_other(self):
        server = FakeRemote()
        self.assertEqual(ro.commit_staged(server, "abc", "t", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32), "created")
        self.assertIn("mv -T", server.calls[0])
        reuse = FakeRemote({"sha256sum": "aa  /opt/a/releases/1.0.0/app.war\n"})
        self.assertEqual(ro.commit_staged(reuse, "abc", "t", "1.0.0",
                                          "/opt/a/.incoming/op1", "aa" * 32), "reused")
        with self.assertRaises(ro.VersionConflict):
            ro.commit_staged(FakeRemote({"sha256sum": "bb  x\n"}), "abc", "t",
                             "1.0.0", "/opt/a/.incoming/op1", "aa" * 32)

    def test_stop_rejects_active_or_jobs(self):
        with self.assertRaises(ro.StopFailed):
            ro.stop_and_confirm(FakeRemote({"is-active": "active"}), "a", "t", "u")
        with self.assertRaises(ro.StopFailed):
            ro.stop_and_confirm(FakeRemote({"list-jobs": "1 u.service start running"}),
                                "a", "t", "u")

    def test_set_phase_is_token_guarded(self):
        server = FakeRemote()
        ro.set_phase(server, "abc", "deadbeef", "health")
        cmd = _one_call(server)
        self.assertIn("owner.phase", cmd)
        self.assertIn('grep -F -q -f - "$OWNER"', cmd)
        self.assertIn("exit 73", cmd)
        self.assertNotIn("deadbeef", cmd)
        self.assertEqual(server.stdis[0], "deadbeef\n")

    def test_inspect_returns_phase_without_token(self):
        class S:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ("@@OWNER@@\n{\"token\": \"t\", \"phase\": \"starting\"}\n"
                        "@@PHASE@@\nstarting\n@@RESULT@@\n{}")
        out = ro.inspect(S(), "abc")
        self.assertEqual(out["phase"], "starting")
        self.assertEqual(out["owner"]["phase"], "starting")
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_remote_operation.py -v`
Expected: FAIL，`ModuleNotFoundError: deploy_strategies.remote_operation`

- [ ] **Step 3: 实现锁与命令**

```python
"""远端实例锁与严格命令：HTTP 之外只做进程/文件操作。"""
import base64
import json
import shlex
import time

LOCK_ROOT = "/var/lib/smartops/tomcat"


class LockBusy(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    pass


class StopFailed(RuntimeError):
    pass


def shq(value: str) -> str:
    return shlex.quote(str(value))


def lock_paths(instance_id: str) -> dict:
    d = f"{LOCK_ROOT}/{instance_id}"
    return {"dir": d, "gate": f"{d}/gate.lock", "owner": f"{d}/owner.json",
            "phase": f"{d}/owner.phase",
            "result": f"{d}/last-result.json", "revoked": f"{d}/owner.revoked.json"}


def guarded(server, instance_id: str, token: str, command: str,
            timeout: int = 120, *, stdin_rest: str = "") -> str:
    p = lock_paths(instance_id)
    script = (f"IFS= read -r __TOK || exit 71; "
              f"OWNER={shq(p['owner'])}; "
              f'printf %s "$__TOK" | grep -F -q -f - "$OWNER" || exit 73; '
              f"{command}")
    cmd = f"flock -x {shq(p['gate'])} bash -c {shq(script)}"
    return server.exec(cmd, timeout=timeout, stdin_data=token + "\n" + stdin_rest)


def acquire(server, instance_id: str, token: str, operation: str, phase: str) -> None:
    p = lock_paths(instance_id)
    payload = json.dumps({"token": token, "operation": operation, "phase": phase,
                          "started_at": int(time.time())}, sort_keys=True)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    script = (f"test ! -e {shq(p['owner'])} || {{ echo LOCKED; exit 75; }}; "
              f"printf %s {shq(b64)} | base64 -d > {shq(p['owner'] + '.tmp')} && "
              f"mv -f {shq(p['owner'] + '.tmp')} {shq(p['owner'])} && "
              f"printf %s {shq(phase)} > {shq(p['phase'] + '.tmp')} && "
              f"mv -f {shq(p['phase'] + '.tmp')} {shq(p['phase'])}")
    try:
        server.exec(f"mkdir -p {shq(p['dir'])} && "
                    f"flock -x {shq(p['gate'])} bash -c {shq(script)}", timeout=30)
    except RuntimeError as e:
        if "LOCKED" in str(e):
            raise LockBusy(f"实例 {instance_id} 已被占用") from e
        raise


def release(server, instance_id: str, token: str, result: dict) -> None:
    p = lock_paths(instance_id)
    payload = base64.b64encode(json.dumps(result, ensure_ascii=False,
                                          sort_keys=True).encode("utf-8")).decode("ascii")
    script = (f"grep -F -q {shq(token)} {shq(p['owner'])} || exit 73; "
              f"printf %s {shq(payload)} | base64 -d > {shq(p['result'] + '.tmp')} && "
              f"mv -f {shq(p['result'] + '.tmp')} {shq(p['result'])}; "
              f"rm -f {shq(p['owner'])} {shq(p['phase'])}")
    server.exec(f"flock -x {shq(p['gate'])} bash -c {shq(script)}", timeout=30)


def set_phase(server, instance_id: str, token: str, phase: str) -> None:
    """best-effort 更新当前操作阶段；只允许 owner 持有者写。"""
    p = lock_paths(instance_id)
    script = (f"grep -F -q {shq(token)} {shq(p['owner'])} || exit 73; "
              f"printf %s {shq(phase)} > {shq(p['phase'] + '.tmp')} && "
              f"mv -f {shq(p['phase'] + '.tmp')} {shq(p['phase'])}")
    server.exec(f"flock -x {shq(p['gate'])} bash -c {shq(script)}", timeout=15)


def commit_staged(server, instance_id: str, token: str, version: str,
                  incoming_dir: str, sha256: str) -> str:
    app = incoming_dir.rsplit("/.incoming/", 1)[0]
    release_dir = f"{app}/releases/{version}"
    script = (
        f"test -f {shq(incoming_dir + '/app.war')} || exit 81; "
        f"echo {shq(sha256 + '  ' + incoming_dir + '/app.war')} | sha256sum -c - >/dev/null || exit 82; "
        f"if [ -e {shq(release_dir)} ]; then "
        f"  test \"$(sha256sum {shq(release_dir + '/app.war')} | awk '{{print $1}}')\" = {shq(sha256)} || exit 76; "
        f"  rm -rf {shq(incoming_dir)}; echo REUSED; exit 0; "
        f"fi; "
        f"mkdir -p {shq(app + '/releases')} && mv -T {shq(incoming_dir)} {shq(release_dir)} && echo CREATED"
    )
    out = guarded(server, instance_id, token, script, timeout=120).strip()
    if out.endswith("REUSED"):
        return "reused"
    if out.endswith("CREATED"):
        return "created"
    raise VersionConflict(f"版本 {version} 提交结果不明: {out!r}")


def stop_and_confirm(server, instance_id: str, token: str, unit: str) -> None:
    script = (
        f"systemctl stop {shq(unit)}.service || exit 11; "
        f"test \"$(systemctl is-active {shq(unit)}.service 2>/dev/null || true)\" = inactive || exit 12; "
        f"test -z \"$(systemctl list-jobs --no-legend --unit={shq(unit)}.service)\" || exit 13; "
        f"cg=$(systemctl show -p ControlGroup --value {shq(unit)}.service); "
        f"if [ -n \"$cg\" ] && [ \"$cg\" != / ] && [ -d \"/sys/fs/cgroup$cg\" ]; then "
        f"  find \"/sys/fs/cgroup$cg\" -name cgroup.procs -type f -exec grep -q '[0-9]' {{}} \\; "
        f"    -print -quit | grep -q . && exit 14; "
        f"fi"
    )
    try:
        guarded(server, instance_id, token, script, timeout=60)
    except RuntimeError as e:
        raise StopFailed(f"{unit} 未确认停止: {e}") from e


def inspect(server, instance_id: str) -> dict:
    p = lock_paths(instance_id)
    out = server.exec(
        f"echo @@OWNER@@; cat {shq(p['owner'])} 2>/dev/null || true; "
        f"echo @@PHASE@@; cat {shq(p['phase'])} 2>/dev/null || true; "
        f"echo @@RESULT@@; cat {shq(p['result'])} 2>/dev/null || true", timeout=15)
    owner = out.split("@@OWNER@@", 1)[-1].split("@@PHASE@@", 1)[0].strip()
    phase = out.split("@@PHASE@@", 1)[-1].split("@@RESULT@@", 1)[0].strip()
    result = out.split("@@RESULT@@", 1)[-1].strip()
    def load(raw):
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {"raw": raw[:200]}
    loaded = load(owner)
    return {"owner": loaded, "phase": phase or str(loaded.get("phase") or ""),
            "last_result": load(result)}


def revoke(server, instance_id: str, confirm_digest: str) -> None:
    p = lock_paths(instance_id)
    script = (
        f"test \"$(cat {shq(p['owner'])} | sha256sum | awk '{{print $1}}')\" = {shq(confirm_digest)} || exit 77; "
        f"mv -f {shq(p['owner'])} {shq(p['revoked'])}; rm -f {shq(p['phase'])}"
    )
    server.exec(f"flock -x {shq(p['gate'])} bash -c {shq(script)}", timeout=30)
```

注意：`commit_staged` 的 `incoming_dir` 必须是平台派生路径，调用方不得直接接受用户输入。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_remote_operation.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`
Expected: 新增 `remote_operation.py` 与测试；无其它模块变化。

---

### Task 5: SSH HTTP 探活与有界轮询

**Files:**
- Create: `deploy_strategies/tomcat_health.py`
- Test: `tests/test_tomcat_health.py`

**Interfaces:**
- Produces: `probe_http(server, port: int, path: str, expected_status: int = 200,
  body_contains: str = "", timeout: float = 5.0) -> dict`，返回
  `{"ok": bool, "ms": int, "status": int|None, "error": str}`。
- Produces: `poll_http(server, port, path, timeout: int, interval: int,
  expected_status=200, body_contains="") -> dict`，最后一次结果附加 `attempts`。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from deploy_strategies import tomcat_health as th

class FakeChannel:
    def __init__(self, payload, fail=None):
        self.payload = payload
        self.fail = fail
        self.sent = b""
    def settimeout(self, value):
        self.timeout = value
    def sendall(self, data):
        self.sent += data
    def makefile(self, mode):
        import io
        if self.fail:
            raise OSError(self.fail)
        return io.BytesIO(self.payload)
    def close(self):
        self.closed = True

class FakeTransport:
    def __init__(self, channel):
        self.channel = channel
        self.args = None
    def open_channel(self, kind, dest, src, timeout=None):
        self.args = (kind, dest, src, timeout)
        return self.channel

class FakeClient:
    def __init__(self, channel):
        self.transport = FakeTransport(channel)
    def get_transport(self):
        return self.transport

class FakeServer:
    def __init__(self, channel):
        self.client = FakeClient(channel)

class HealthTest(unittest.TestCase):
    def test_200_and_body_ok(self):
        channel = FakeChannel(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nUP")
        out = th.probe_http(FakeServer(channel), 18080, "/orders/health", body_contains="UP")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], 200)
        self.assertEqual(channel.sent,
                         b"GET /orders/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Connection: close\r\n\r\n")
        self.assertEqual(FakeServer(channel).client.transport.args[0], "direct-tcpip")

    def test_redirect_and_wrong_body_fail_without_following(self):
        for payload, body in ((b"HTTP/1.1 302 Found\r\nLocation: /x\r\n\r\n", ""),
                              (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nNO", "UP")):
            out = th.probe_http(FakeServer(FakeChannel(payload)), 1, "/h",
                                body_contains=body)
            self.assertFalse(out["ok"])

    def test_forwarding_denied(self):
        server = FakeServer(FakeChannel(b"", fail="Administratively prohibited"))
        out = th.probe_http(server, 1, "/h")
        self.assertFalse(out["ok"])
        self.assertIn("forward", out["error"])

    def test_body_limit(self):
        payload = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (th.MAX_BODY + 1)
        out = th.probe_http(FakeServer(FakeChannel(payload)), 1, "/h")
        self.assertFalse(out["ok"])
        self.assertIn("响应", out["error"])

    def test_poll_stops_at_deadline(self):
        calls = []
        original = th.probe_http
        th.probe_http = lambda *a, **k: calls.append(1) or {"ok": False, "ms": 1, "status": None, "error": "x"}
        try:
            out = th.poll_http(FakeServer(FakeChannel(b"")), 1, "/h", timeout=0, interval=1)
        finally:
            th.probe_http = original
        self.assertFalse(out["ok"])
        self.assertEqual(len(calls), 1)
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_health.py -v`
Expected: FAIL，`ModuleNotFoundError: deploy_strategies.tomcat_health`

- [ ] **Step 3: 实现探活**

```python
"""SSH direct-tcpip HTTP 探活：不要求目标机安装 curl，不跟随重定向。"""
import socket
import time

MAX_BODY = 1024 * 1024


def _read_all(fileobj, limit: int) -> bytes:
    data = b""
    while len(data) <= limit:
        chunk = fileobj.read(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data += chunk
    return data


def probe_http(server, port: int, path: str, expected_status: int = 200,
               body_contains: str = "", timeout: float = 5.0) -> dict:
    start = time.time()
    channel = None
    try:
        transport = server.client().get_transport()
        channel = transport.open_channel(
            "direct-tcpip", ("127.0.0.1", int(port)), ("127.0.0.1", 0),
            timeout=min(float(timeout), 5.0))
        channel.settimeout(min(float(timeout), 5.0))
        request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   f"Connection: close\r\n\r\n").encode("ascii")
        channel.sendall(request)
        f = channel.makefile("rb")
        raw = _read_all(f, MAX_BODY)
        if len(raw) > MAX_BODY:
            return {"ok": False, "ms": int((time.time() - start) * 1000),
                    "status": None, "error": "响应超过 1 MiB 上限"}
        head, _, body = raw.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first.split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            return {"ok": False, "ms": int((time.time() - start) * 1000),
                    "status": None, "error": "无效 HTTP 响应"}
        status = int(parts[1])
        ok = status == expected_status and (not body_contains
                                            or body_contains in body.decode("utf-8", "ignore"))
        return {"ok": ok, "ms": int((time.time() - start) * 1000),
                "status": status, "error": "" if ok else "状态或响应正文不匹配"}
    except (OSError, socket.timeout, EOFError) as e:
        text = str(e)
        if "prohibited" in text.lower() or "forward" in text.lower():
            text = "SSH 端口转发被禁止或不可用"
        return {"ok": False, "ms": int((time.time() - start) * 1000),
                "status": None, "error": text}
    except Exception as e:
        return {"ok": False, "ms": int((time.time() - start) * 1000),
                "status": None, "error": str(e)}
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass


def poll_http(server, port: int, path: str, timeout: int, interval: int,
              expected_status: int = 200, body_contains: str = "") -> dict:
    deadline = time.monotonic() + max(0, timeout)
    attempts = 0
    last = {"ok": False, "ms": 0, "status": None, "error": "未执行"}
    while True:
        attempts += 1
        last = probe_http(server, port, path, expected_status=expected_status,
                          body_contains=body_contains)
        if last["ok"] or time.monotonic() >= deadline:
            break
        time.sleep(max(0.1, min(interval, deadline - time.monotonic())))
    last["attempts"] = attempts
    return last
```

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_tomcat_health.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`

---

### Task 6: 实例文件清单、归属清单与预览摘要

**Files:**
- Create: `deploy_strategies/tomcat_instance.py`
- Test: `tests/test_tomcat_instance.py`

**Interfaces:**
- Consumes: Task 1/2 的配置与渲染。
- Produces: `read_templates(server, tomcat_home: str) -> dict`。
- Produces: `build_manifest(cfg, service, project_id, env_name, unit, instance_id,
  template_version, digest, files=None, unit_content="") -> dict`；含
  `managed_files`、逐文件 `file_digests` 与 `unit_digest`。
- Produces: `verify_instance(server, app_home, unit, manifest) -> list[str]`，
  返回与登记摘要不一致的文件/unit；空列表表示未漂移。
- Produces: `preview(server, cfg, service, project_id, env_name, unit, instance_id) -> dict`，
  返回 `{"digest", "unit", "unit_content", "files", "manifest", "deletes", "templates"}`；
  `manifest` 是接入时要原样写入的归属清单，`templates` 只列命中的模板文件名。
- Produces: `manifest_path(app_home) -> str`。
- Produces: `read_manifest(server, app_home) -> dict`，缺失或解析失败返回 `{}`。

- [ ] **Step 1: 写失败测试**

```python
import base64
import json
import unittest
from deploy_strategies import tomcat_instance as ti
from deploy_strategies.tomcat_config import parse_war_config

CFG = parse_war_config({
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
})
TEMPLATES = {"web.xml": "<web-app/>", "catalina.properties": "a=1\n",
             "logging.properties": "h=x\n"}

class FakeRemote:
    def __init__(self, reads=None):
        self.reads = reads or {}
        self.calls = []
    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        for key, value in self.reads.items():
            if key in cmd:
                return value
        return ""

TEMPLATE_B64 = base64.b64encode(b"<web-app/>").decode()

class InstanceTest(unittest.TestCase):
    def test_preview_is_deterministic_and_has_no_env_values(self):
        server = FakeRemote({"base64 -w0": TEMPLATE_B64})
        a = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        b = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        self.assertEqual(a["digest"], b["digest"])
        self.assertIn("instance/conf/server.xml", a["files"])
        self.assertIn("org.apache.catalina.startup.Bootstrap start", a["unit_content"])
        self.assertNotIn("password", json.dumps(a).lower())

    def test_manifest_roundtrip_and_drift(self):
        manifest = ti.build_manifest(CFG, "orders", 3, "test", "orders-war",
                                     "i-123", "v1", "d" * 64)
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote({"base64 -w0": encoded})
        loaded = ti.read_manifest(server, CFG["app_home"])
        self.assertEqual(loaded["instance_id"], "i-123")
        self.assertEqual(loaded["config_digest"], "d" * 64)

    def test_missing_manifest_is_empty(self):
        self.assertEqual(ti.read_manifest(FakeRemote(), CFG["app_home"]), {})

    def test_manifest_files_and_unit_digests_verify(self):
        import hashlib
        files = {"instance/conf/server.xml": "<Server/>"}
        manifest = ti.build_manifest(CFG, "orders", 3, "test", "orders-war",
                                     "i-123", "v1", "d" * 64,
                                     files=files, unit_content="[Service]\n")
        self.assertIn("instance/conf/server.xml", manifest["file_digests"])
        self.assertEqual(len(manifest["unit_digest"]), 64)
        server = FakeRemote({
            "server.xml": hashlib.sha256(b"<Server/>").hexdigest(),
            "unit.service": hashlib.sha256(b"[Service]\n").hexdigest(),
        })
        self.assertEqual(ti.verify_instance(
            server, CFG["app_home"], "orders-war", manifest), [])
        drift = dict(manifest, file_digests={"instance/conf/server.xml": "0" * 64})
        self.assertIn("instance/conf/server.xml", ti.verify_instance(
            server, CFG["app_home"], "orders-war", drift))

    def test_delete_scope_is_only_runtime_cache(self):
        server = FakeRemote({"base64 -w0": TEMPLATE_B64})
        out = ti.preview(server, CFG, "orders", 3, "test", "orders-war", "i-123")
        self.assertEqual(out["deletes"],
                         ["instance/webapps/*", "instance/work/*", "instance/temp/*"])
        self.assertNotIn("logs", " ".join(out["deletes"]))
        self.assertNotIn("releases", " ".join(out["deletes"]))
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_instance.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现清单与预览**

```python
"""实例文件、归属清单与管理员预览；不做发布切换。"""
import base64
import hashlib
import json

from .remote_operation import shq
from .tomcat_config import (TEMPLATE_VERSION, config_digest, parse_war_config,
                            render_instance_files, render_war_unit)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_path(app_home: str) -> str:
    return f"{app_home}/.smartops-instance.json"


def read_templates(server, tomcat_home: str) -> dict:
    result = {}
    for name in ("web.xml", "catalina.properties", "logging.properties"):
        raw = server.exec(
            f"base64 -w0 {tomcat_home}/conf/{name} 2>/dev/null || true", timeout=15).strip()
        if raw:
            result[name] = base64.b64decode(raw).decode("utf-8", "replace")
    return result


def read_manifest(server, app_home: str) -> dict:
    raw = server.exec(
        f"base64 -w0 {manifest_path(app_home)} 2>/dev/null || true", timeout=15).strip()
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def build_manifest(cfg, service, project_id, env_name, unit, instance_id,
                   template_version, digest, files=None, unit_content="") -> dict:
    files = files or {}
    managed = sorted(files) if files else sorted(render_instance_files(
        cfg, service, {"web.xml": "x", "catalina.properties": "x",
                       "logging.properties": "x"}))
    return {
        "schema_version": 1,
        "instance_id": instance_id,
        "project_id": project_id,
        "service": service,
        "env": env_name,
        "unit": unit,
        "app_home": cfg["app_home"],
        "port": cfg["port"],
        "context_path": cfg["context_path"],
        "run_user": cfg["run_user"],
        "template_version": template_version,
        "config_digest": digest,
        "managed_files": managed,
        "file_digests": {k: _sha(v) for k, v in sorted(files.items())},
        "unit_digest": _sha(unit_content),
        "maintenance": {"active": False, "traffic_snapshot": None},
        "status": "ready",
    }


def verify_instance(server, app_home: str, unit: str, manifest: dict) -> list:
    """按归属清单核对实际受管文件与 unit；返回不一致项，空列表表示未漂移。"""
    expected = manifest.get("file_digests") or {}
    if not expected:
        return ["归属清单缺少文件摘要"]
    bad = []
    for rel, digest in sorted(expected.items()):
        try:
            actual = server.exec(
                f"sha256sum {shq(app_home + '/' + rel)} 2>/dev/null | "
                f"awk '{{print $1}}'", timeout=15).strip()
        except Exception:
            actual = ""
        if actual != digest:
            bad.append(rel)
    expected_unit = manifest.get("unit_digest") or ""
    try:
        actual_unit = server.exec(
            f"sha256sum {shq('/etc/systemd/system/' + unit + '.service')} "
            f"2>/dev/null | awk '{{print $1}}'", timeout=15).strip()
    except Exception:
        actual_unit = ""
    if not expected_unit or actual_unit != expected_unit:
        bad.append("unit.service")
    return bad


def preview(server, cfg, service, project_id, env_name, unit, instance_id) -> dict:
    templates = read_templates(server, cfg["tomcat_home"])
    files = render_instance_files(cfg, service, templates)
    unit_content = render_war_unit(cfg, service)
    digest = config_digest(cfg, files, TEMPLATE_VERSION, unit_content)
    manifest = build_manifest(cfg, service, project_id, env_name, unit,
                              instance_id, TEMPLATE_VERSION, digest,
                              files={f"instance/{k}": v for k, v in files.items()},
                              unit_content=unit_content)
    return {
        "digest": digest,
        "unit": unit,
        "unit_content": unit_content,
        "files": {f"instance/{k}": v for k, v in files.items()},
        "manifest": manifest,
        "deletes": ["instance/webapps/*", "instance/work/*", "instance/temp/*"],
        "templates": sorted(templates),
    }
```

`preview` 的 `manifest` 含调用方传入的 `instance_id`。路由预览时先读归属
文件：已接入则复用其中的 `instance_id`；未接入才生成 `uuid4().hex`，并在接入
确认时原样回传。发布、重启、配置和恢复一律从归属文件读取该 ID，禁止按项目/
环境/服务/主机重新推导，也不能每次预览换 ID。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_tomcat_instance.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`

---

### Task 7: 接入与配置事务（受锁、可恢复）

**Files:**
- Modify: `deploy_strategies/tomcat_instance.py`
- Test: `tests/test_tomcat_onboard.py`

**Interfaces:**
- Produces: `onboard(server, plan: dict, project_id: int, env_name: str,
  instance_id: str, preview_digest: str) -> dict`；plan 来自 Task 6 的
  `preview()`，摘要不符抛 `StalePreview`。
- Produces: `maintenance_path(app_home: str) -> str`。
- Produces: `read_maintenance(server, app_home) -> dict`，缺失或解析失败返回 `{}`。
- Produces: `maintenance_stop(server, app_home, unit, instance_id, token,
  traffic_snapshot: dict) -> dict`：先持久化维护标志与流量快照（清单 +
  `.smartops-maintenance.json`），再严格停止；停止失败时保留标志并抛出，
  由调用方核验旧实例与原流量后才可撤销。
- Produces: `clear_maintenance(server, app_home, instance_id, token) -> None`，
  仅在重启成功且流量恢复后调用。
- Produces: `update_manifest(server, app_home, instance_id, token, **changes) -> dict`，
  先读后写，`instance_id` 不匹配抛 `RuntimeError`。
- Produces: `apply_config(server, app_home, unit, instance_id, token,
  files: dict, unit_content: str, digest: str, manifest_changes: dict = None) -> None`；
  备份旧配置与清单，成功后用 `manifest_changes` 更新清单（含新 `config_digest`），
  任一步失败恢复旧配置、旧清单与旧 unit，实例保持停止。
- 所有写动作使用 Task 4 的 `guarded`；接入用 `acquire/release`。

- [ ] **Step 1: 写失败测试**

```python
import base64
import json
import unittest
from deploy_strategies import tomcat_instance as ti
from deploy_strategies.tomcat_config import parse_war_config

CFG = parse_war_config({
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
})
PLAN = {
    "digest": "a" * 64, "unit": "orders-war", "unit_content": "[Service]\n",
    "files": {"instance/conf/server.xml": "<Server/>"},
    "manifest": {"schema_version": 1, "instance_id": "i1",
                 "app_home": CFG["app_home"], "unit": "orders-war",
                 "config_digest": "a" * 64, "maintenance":
                 {"active": False, "traffic_snapshot": None}},
}

class FakeRemote:
    def __init__(self):
        self.calls = []
    def exec(self, cmd, timeout=60, stdin_data=None):
        self.calls.append(cmd)
        return ""

class OnboardTest(unittest.TestCase):
    def test_stale_preview_rejected_before_write(self):
        server = FakeRemote()
        with self.assertRaises(ti.StalePreview):
            ti.onboard(server, PLAN, 1, "test", "i1", "b" * 64)
        self.assertEqual(server.calls, [])

    def test_onboard_refuses_existing_other_owner(self):
        import base64
        import json

        class Existing(FakeRemote):
            def exec(self, cmd, timeout=60, stdin_data=None):
                self.calls.append(cmd)
                if "base64 -w0" in cmd:
                    return base64.b64encode(json.dumps(
                        {"instance_id": "other"}).encode("utf-8")).decode("ascii")
                return ""

        server = Existing()
        with self.assertRaises(RuntimeError):
            ti.onboard(server, PLAN, 1, "test", "i1", "a" * 64)
        self.assertNotIn("owner.json", "\n".join(server.calls))

    def test_onboard_writes_files_unit_and_never_starts(self):
        server = FakeRemote()
        out = ti.onboard(server, PLAN, 1, "test", "i1", "a" * 64)
        joined = "\n".join(server.calls)
        self.assertIn("owner.json", joined)
        self.assertIn("instance/conf/server.xml", joined)
        self.assertIn(".smartops-instance.json", joined)
        self.assertIn("daemon-reload", joined)
        self.assertIn("enable", joined)
        self.assertNotIn("systemctl start", joined)
        self.assertEqual(out["status"], "ready")

    def test_maintenance_flag_is_written_before_stop(self):
        server = FakeRemote()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or "inactive")
        snap = ti.maintenance_stop(server, CFG["app_home"], "orders-war", "i1",
                                   "t", {"orders|G1": [{"port": 8080,
                                                        "enabled": True,
                                                        "weight": 10}]})
        self.assertTrue(snap["active"])
        self.assertEqual(snap["traffic_snapshot"]["orders|G1"][0]["weight"], 10)
        joined = "\n".join(server.calls)
        self.assertIn(".smartops-maintenance.json", joined)
        write_at = joined.index(".smartops-maintenance.json")
        stop_at = joined.index("systemctl stop")
        self.assertLess(write_at, stop_at, "维护标志必须先于停止落盘")

    def test_maintenance_stop_failure_keeps_flag(self):
        server = FakeRemote()
        def failing(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "systemctl stop" in cmd:
                raise RuntimeError("stop failed")
            return "inactive"
        server.exec = failing
        with self.assertRaises(RuntimeError):
            ti.maintenance_stop(server, CFG["app_home"], "orders-war", "i1",
                                "t", {})
        self.assertIn(".smartops-maintenance.json", "\n".join(server.calls))

    def test_clear_maintenance_clears_manifest_and_file(self):
        manifest = {"schema_version": 1, "instance_id": "i1",
                    "maintenance": {"active": True, "traffic_snapshot": {}}}
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or
            (encoded if "smartops-instance.json" in cmd else "inactive"))
        ti.clear_maintenance(server, CFG["app_home"], "i1", "t")
        joined = "\n".join(server.calls)
        self.assertIn("rm -f", joined)
        self.assertIn(".smartops-instance.json", joined)

    def test_apply_config_updates_digest_after_reload(self):
        manifest = {"schema_version": 1, "instance_id": "i1",
                    "config_digest": "a" * 64,
                    "maintenance": {"active": True, "traffic_snapshot": {}}}
        encoded = base64.b64encode(json.dumps(manifest).encode()).decode()
        server = FakeRemote()
        server.exec = lambda cmd, timeout=60, stdin_data=None: (
            server.calls.append(cmd) or
            (encoded if "base64 -w0" in cmd and "smartops-instance" in cmd
             else ""))
        ti.apply_config(server, CFG["app_home"], "orders-war", "i1", "t",
                        {"instance/conf/server.xml": "new"}, "[Service]\n",
                        "b" * 64, manifest_changes={"config_digest": "b" * 64})
        joined = "\n".join(server.calls)
        self.assertIn("daemon-reload", joined)
        self.assertIn(".smartops-instance.json", joined)
        # 清单写入必须晚于 daemon-reload，避免 reload 失败时留下新摘要
        self.assertGreater(joined.index(".smartops-instance.json"),
                           joined.index("daemon-reload"))

    def test_apply_config_restores_backup_on_reload_failure(self):
        server = FakeRemote()
        def failing(cmd, timeout=60, stdin_data=None):
            server.calls.append(cmd)
            if "daemon-reload" in cmd:
                raise RuntimeError("reload failed")
            return ""
        server.exec = failing
        with self.assertRaises(RuntimeError):
            ti.apply_config(server, CFG["app_home"], "orders-war", "i1", "t",
                            {"instance/conf/server.xml": "new"}, "[Service]\n",
                            "b" * 64)
        joined = "\n".join(server.calls)
        self.assertIn(".config-backup", joined)
        self.assertIn("cp -a", joined)
        self.assertIn("rm -rf", joined)
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_onboard.py -v`
Expected: FAIL，`AttributeError: module ... has no attribute 'StalePreview'`

- [ ] **Step 3: 实现接入与配置事务**

```python
import secrets
import time

from .remote_operation import (acquire, guarded, release, shq,
                               stop_and_confirm)

class StalePreview(ValueError):
    pass


def maintenance_path(app_home: str) -> str:
    return f"{app_home}/.smartops-maintenance.json"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _write_file(server, instance_id, token, target, content):
    # 内容经 stdin_rest 随令牌同行进入 stdin：令牌占首行，其余由 cat 消费，
    # 配置文本不进命令行（超长/特殊字符也安全）
    guarded(server, instance_id, token,
            f"mkdir -p {shq(target.rsplit('/', 1)[0])} && "
            f"cat > {shq(target)}.tmp && mv -f {shq(target)}.tmp {shq(target)}",
            timeout=30, stdin_rest=content)


def _read_json_file(server, path):
    raw = server.exec(f"base64 -w0 {shq(path)} 2>/dev/null || true",
                      timeout=15).strip()
    if not raw:
        return {}
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def read_maintenance(server, app_home: str) -> dict:
    return _read_json_file(server, maintenance_path(app_home))


def update_manifest(server, app_home, instance_id, token, **changes) -> dict:
    manifest = read_manifest(server, app_home)
    if not manifest:
        raise RuntimeError("实例归属清单不存在，拒绝更新")
    if manifest.get("instance_id") != instance_id:
        raise RuntimeError("实例归属标识不一致，拒绝更新")
    manifest.update(changes)
    _write_file(server, instance_id, token, manifest_path(app_home),
                json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return manifest


def _assert_instance_rel(rel: str) -> None:
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "instance" or parts[1] != "conf" \
            or ".." in parts or "" in parts:
        raise ValueError(f"配置事务拒绝非 instance/conf/ 相对路径: {rel!r}")


def onboard(server, plan, project_id, env_name, instance_id, preview_digest):
    if plan["digest"] != preview_digest:
        raise StalePreview("预览摘要已变化，请重新预览")
    existing = read_manifest(server, plan["manifest"]["app_home"])
    if existing and existing.get("instance_id") != instance_id:
        raise RuntimeError("实例已有其它归属，拒绝覆盖")
    token = secrets.token_hex(32)
    acquire(server, instance_id, token, "onboard", "preflight")
    try:
        app = plan["manifest"]["app_home"]
        for rel, content in sorted(plan["files"].items()):
            _write_file(server, instance_id, token, f"{app}/{rel}", content)
        unit = plan["unit"]
        _write_file(server, instance_id, token,
                    f"/etc/systemd/system/{unit}.service", plan["unit_content"])
        manifest = dict(plan["manifest"], status="ready")
        _write_file(server, instance_id, token, manifest_path(app),
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        guarded(server, instance_id, token,
                f"systemctl daemon-reload && systemctl enable {shq(unit)}.service",
                timeout=60)
    except Exception as e:
        try:
            release(server, instance_id, token,
                    {"status": "failed", "operation": "onboard", "error": str(e)})
        except Exception:
            pass
        raise
    release(server, instance_id, token, {"status": "ready", "operation": "onboard"})
    return manifest


def maintenance_stop(server, app_home, unit, instance_id, token, traffic_snapshot):
    """维护标志先于停止落盘；停止失败保留标志，交人工核验后再撤销。"""
    snapshot = {"active": True, "traffic_snapshot": traffic_snapshot or {},
                "started_at": int(time.time())}
    _write_file(server, instance_id, token, maintenance_path(app_home),
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
    update_manifest(server, app_home, instance_id, token,
                    maintenance=snapshot, status="maintenance")
    stop_and_confirm(server, instance_id, token, unit)
    return snapshot


def clear_maintenance(server, app_home, instance_id, token) -> None:
    """重启成功且流量恢复后才调用；文件与清单同时回到非维护态。"""
    guarded(server, instance_id, token,
            f"rm -f {shq(maintenance_path(app_home))}", timeout=15)
    update_manifest(server, app_home, instance_id, token,
                    maintenance={"active": False, "traffic_snapshot": None},
                    status="ready")


def apply_config(server, app_home, unit, instance_id, token, files,
                 unit_content, digest, manifest_changes=None):
    revision = digest[:12]
    backup = f"{app_home}/.config-backup/{revision}"
    manifest_backup = f"{backup}/instance.json"
    try:
        guarded(server, instance_id, token,
                f"mkdir -p {shq(backup)} && "
                f"cp -a {shq(app_home + '/instance/conf')} {shq(backup + '/conf')} && "
                f"cp -a {shq(manifest_path(app_home))} {shq(manifest_backup)} && "
                f"cp -a {shq('/etc/systemd/system/' + unit + '.service')} "
                f"{shq(backup + '/unit.service')}", timeout=60)
        for rel, content in sorted(files.items()):
            _assert_instance_rel(rel)
            _write_file(server, instance_id, token, f"{app_home}/{rel}", content)
        _write_file(server, instance_id, token,
                    f"/etc/systemd/system/{unit}.service", unit_content)
        guarded(server, instance_id, token,
                f"systemctl daemon-reload && systemctl enable {shq(unit)}.service",
                timeout=60)
        if manifest_changes:
            update_manifest(
                server, app_home, instance_id, token,
                **dict(manifest_changes, config_digest=digest,
                       file_digests={k: _sha(v) for k, v in files.items()},
                       unit_digest=_sha(unit_content)))
    except Exception:
        guarded(server, instance_id, token,
                f"rm -rf {shq(app_home + '/instance/conf')} && "
                f"cp -a {shq(backup + '/conf')} {shq(app_home + '/instance/conf')} && "
                f"cp -a {shq(manifest_backup)} {shq(manifest_path(app_home))} && "
                f"cp -a {shq(backup + '/unit.service')} "
                f"{shq('/etc/systemd/system/' + unit + '.service')} && "
                f"systemctl daemon-reload", timeout=60)
        raise
```

`files` 的键是 `instance/conf/...`，值来自 `preview["files"]`；`plan["manifest"]["unit"]` 决定 unit 文件名。不要接受调用方传入的任意绝对 `files` 路径。`apply_config` 不启动、只更新清单摘要；维护标志仍由 `maintenance_stop` 持有。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_tomcat_onboard.py tests/test_tomcat_instance.py -v`
Expected: PASS；接入命令无 `systemctl start`。

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`

---

### Task 8: 严格生命周期与逐主机结果

**Files:**
- Create: `deploy_strategies/tomcat_lifecycle.py`
- Test: `tests/test_tomcat_lifecycle.py`

**Interfaces:**
- Produces: `HostResult` dataclass，字段
  `ok, operation_status, phase, current_version, recovery_target, process_state,
  app_health, recovery_status, traffic_status, lock_state, error`。
- Produces: `WarTomcatLifecycle(deployer, cfg, service, unit, instance_id=None)`；
  `instance_id(server)` 由外部注入（WAR 从归属文件解析，测试可替换）。
- Produces: `deploy(server, server_entry=None) -> HostResult`。
- Produces: `rollback(server, target_version, server_entry=None, explicit=True) -> HostResult`。
- Produces: `restart(server, server_entry=None, release_maintenance=False) -> HostResult`；
  `release_maintenance=True` 时按维护快照恢复原流量权重，健康后清除维护标志。
- ~~Produces: `build_result(...)`，把阶段与恢复状态映射为上述字段。~~（T8 评审裁定移除：
  映射直接内联在 `HostResult` 构造 + `_finish` 中，仓库无其它引用）
- 失败恢复枚举只允许 `not_needed/succeeded/blocked/failed/unavailable/unknown`。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from deploy_strategies.tomcat_lifecycle import HostResult, WarTomcatLifecycle

class FakeLifecycle(WarTomcatLifecycle):
    def __init__(self, probe_failures=0, stop_ok=True):
        self.probe_failures = probe_failures
        self.stop_ok = stop_ok
        self.calls = []
        self.manifest = {"instance_id": "i1", "maintenance": {"active": False}}
        self.current = "1.0.0"
    def _preflight(self, server, **kwargs):
        self.calls.append("preflight")
    def _snapshot(self, server):
        self.calls.append("snapshot")
        return {"version": self.current, "sha": "a" * 64, "healthy": True}
    def _stage(self, server, version, sha, **kw):
        self.calls.append("stage")
        return "created"
    def _stop(self, server, instance_id, token):
        self.calls.append("stop")
        if not self.stop_ok:
            raise RuntimeError("stop failed")
    def _activate(self, server, instance_id, token, version):
        self.calls.append("activate")
        self.current = version
    def _clean(self, server, instance_id, token):
        self.calls.append("clean")
    def _start(self, server, instance_id, token):
        self.calls.append("start")
    def _probe(self, server):
        self.calls.append("probe")
        if self.probe_failures:
            self.probe_failures -= 1
            raise RuntimeError("health failed")
        return {"ok": True, "status": 200}
    def _traffic(self, server, action, **kw):
        self.calls.append(f"traffic:{action}")
        return {"ok": True}
    def _acquire(self, server, instance_id):
        self.calls.append("acquire")
        return "token"
    def _release(self, server, instance_id, token, result):
        self.calls.append("release")

class LifecycleTest(unittest.TestCase):
    def make(self, **kw):
        d = type("D", (), {"version": "2.0.0", "previous_version": "",
                           "nacos": type("N", (), {"configured": True})(),
                           "_nacos_ip": lambda entry: "10.0.0.1"})()
        lc = FakeLifecycle(**kw)
        lc.d = d
        lc.cfg = {"readiness": {"path": "/health", "timeout": 1, "interval": 1,
                                "expected_status": 200, "body_contains": ""},
                  "context_path": "", "app_home": "/opt/app", "port": 8080}
        lc.service = "orders"
        lc.unit = "orders-war"
        lc.instance_id = lambda server: "i1"
        lc._read_maintenance = lambda server: (lc.manifest.get("maintenance") or {})
        lc._clear_maintenance = lambda server, iid, token: lc.calls.append(
            "clear_maintenance")
        return lc

    def test_success_releases_with_published(self):
        lc = self.make()
        out = lc.deploy(object())
        self.assertTrue(out.ok)
        self.assertEqual(out.operation_status, "published")
        self.assertEqual(out.recovery_status, "not_needed")
        self.assertEqual(lc.calls[-1], "release")

    def test_health_fail_then_recover(self):
        lc = self.make(probe_failures=1)
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertEqual(out.recovery_status, "succeeded")
        self.assertEqual(out.current_version, "1.0.0")
        self.assertIn("release", lc.calls)

    def test_first_deploy_failure_stops_and_reports_unavailable(self):
        lc = self.make(probe_failures=1)
        lc.current = ""
        lc._snapshot = lambda server: {"version": "", "sha": "", "healthy": False}
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertEqual(out.recovery_status, "unavailable")
        self.assertEqual(out.process_state, "stopped")

    def test_stop_failure_is_not_success(self):
        lc = self.make(stop_ok=False)
        out = lc.deploy(object())
        self.assertFalse(out.ok)
        self.assertNotEqual(out.operation_status, "published")

    def test_restart_requires_maintenance_release(self):
        lc = self.make()
        snap = {"orders|G1": [{"port": 8080, "enabled": True, "weight": 7}]}
        lc.manifest["maintenance"] = {"active": True, "traffic_snapshot": snap}
        lc._read_manifest = lambda server: lc.manifest
        with self.assertRaises(RuntimeError):
            lc.restart(object(), release_maintenance=False)
        seen = []
        lc.d.restore_traffic = lambda ip, snapshot: seen.append(snapshot)
        out = lc.restart(object(), release_maintenance=True)
        self.assertTrue(out.ok)
        self.assertEqual(seen, [snap])
        self.assertIn("clear_maintenance", lc.calls)

    def test_restart_clear_failure_is_not_success(self):
        lc = self.make()
        lc.manifest["maintenance"] = {"active": True, "traffic_snapshot": {}}
        lc._read_manifest = lambda server: lc.manifest
        def fail(*args, **kwargs):
            raise RuntimeError("clear failed")
        lc._clear_maintenance = fail
        out = lc.restart(object(), release_maintenance=True)
        self.assertFalse(out.ok)
        self.assertEqual(out.process_state, "running")
        self.assertEqual(out.app_health, "healthy")
        self.assertIn("维护标志清除失败", out.error)

    def test_explicit_rollback_missing_source_blocks_before_stop(self):
        lc = self.make()
        lc._release_exists = lambda server, version: False
        out = lc.rollback(object(), "1.0.0", explicit=True)
        self.assertEqual(out.recovery_status, "blocked")
        self.assertNotIn("stop", lc.calls)

    def test_preflight_rejects_missing_manifest(self):
        class EmptyServer:
            def exec(self, cmd, timeout=60, stdin_data=None):
                return ""
        lc = WarTomcatLifecycle(
            type("D", (), {"version": "2.0.0", "previous_version": ""})(),
            {"app_home": "/opt/app", "port": 8080, "context_path": "",
             "run_user": "u", "readiness": {"path": "/h"}},
            "orders", "orders-war", instance_id=lambda server: "i1")
        with self.assertRaises(RuntimeError):
            lc._preflight(EmptyServer())
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_lifecycle.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现生命周期**

```python
"""WAR 发布/恢复/回滚/重启：所有入口都返回结构化结果。"""
import secrets
from dataclasses import asdict, dataclass

from . import tomcat_instance
from .remote_operation import (LOCK_ROOT, acquire, guarded, release, set_phase,
                               shq, stop_and_confirm)
from .tomcat_health import poll_http


@dataclass
class HostResult:
    ok: bool
    operation_status: str
    phase: str
    current_version: str = ""
    recovery_target: str = ""
    process_state: str = "unknown"
    app_health: str = "unknown"
    recovery_status: str = "not_needed"
    traffic_status: str = "unknown"
    lock_state: str = "released"
    error: str = ""

    def to_dict(self):
        return asdict(self)


class WarTomcatLifecycle:
    def __init__(self, deployer, cfg, service, unit, instance_id=None):
        self.d = deployer
        self.cfg = cfg
        self.service = service
        self.unit = unit
        self._instance_id = instance_id or (lambda server: "")
        self._last_current = ""

    def instance_id(self, server):
        return self._instance_id(server)

    def _phase(self, server, instance_id, token, name):
        """阶段只用于观测：写失败不能打断真实发布，监控显示旧阶段也胜过无阶段。"""
        self.phase = name
        try:
            set_phase(server, instance_id, token, name)
        except Exception:
            pass

    def _state(self, server):
        return {
            "owner": f"{LOCK_ROOT}/{self.instance_id(server)}/owner.json",
            "app_home": self.cfg["app_home"],
        }

    def _snapshot(self, server):
        app = self.cfg["app_home"]
        text = server.exec(
            f"echo @@LINK@@; readlink -f {shq(app + '/CURRENT')} 2>/dev/null || true; "
            f"echo @@SHA@@; sha256sum {shq(app + '/CURRENT/app.war')} 2>/dev/null || true",
            timeout=15)
        link = text.split("@@LINK@@", 1)[-1].split("@@SHA@@", 1)[0].strip()
        sha = text.split("@@SHA@@", 1)[-1].strip().split(" ", 1)[0]
        version = link.rsplit("/", 1)[-1] if link else ""
        return {"version": version, "sha": sha, "healthy": False}

    def _read_manifest(self, server):
        return tomcat_instance.read_manifest(server, self.cfg["app_home"])

    def _read_maintenance(self, server):
        return tomcat_instance.read_maintenance(server, self.cfg["app_home"])

    def _clear_maintenance(self, server, instance_id, token):
        tomcat_instance.clear_maintenance(server, self.cfg["app_home"],
                                          instance_id, token)

    def _restore_traffic(self, server, server_entry, snapshot):
        """结束维护时按停机前快照恢复原权重；原停用流量不强制开启。"""
        if not getattr(self.d, "nacos", None) or not getattr(
                self.d.nacos, "configured", False):
            return {"ok": True, "status": "not_configured"}
        if snapshot and hasattr(self.d, "restore_traffic"):
            try:
                self.d.restore_traffic(self.d._nacos_ip(server_entry), snapshot)
                return {"ok": True, "status": "restored"}
            except Exception as e:
                return {"ok": False, "status": "failed", "error": str(e)}
        return self._traffic(server, "online", server_entry)

    def _preflight(self, server, allow_maintenance=False):
        """归属、维护状态与固定配置一致性：不一致时必须在停机前拒绝"""
        manifest = self._read_manifest(server)
        if not manifest:
            raise RuntimeError("实例尚未由管理员接入，拒绝发布")
        if manifest.get("instance_id") != self.instance_id(server):
            raise RuntimeError("实例归属标识不一致，拒绝发布")
        if ((manifest.get("maintenance") or {}).get("active")
                and not allow_maintenance):
            raise RuntimeError("实例处于维护状态，需先完成配置更新并结束维护")
        plan = tomcat_instance.preview(
            server, self.cfg, manifest.get("service") or self.service,
            manifest.get("project_id"), manifest.get("env"), self.unit,
            manifest["instance_id"])
        if plan["digest"] != manifest.get("config_digest"):
            raise RuntimeError("实例固定配置已漂移，请重新预览并更新配置")
        drift = tomcat_instance.verify_instance(
            server, self.cfg["app_home"], self.unit, manifest)
        if drift:
            raise RuntimeError("实例受管文件或 unit 已漂移: " + ", ".join(drift))
        return manifest

    def _release_exists(self, server, version):
        try:
            server.exec(f"test -f {shq(self.cfg['app_home'] + '/releases/' + version + '/app.war')}",
                        timeout=15)
            return True
        except Exception:
            return False

    def _stage(self, server, version, sha, operation_id):
        app = self.cfg["app_home"]
        incoming = f"{app}/.incoming/{operation_id}"
        server.exec(f"mkdir -p {shq(incoming)}", timeout=15)
        local = self.d.strategy.local_artifact(version)
        server.sftp_put_with_progress(str(local), f"{incoming}/app.war",
                                      label=f"{self.service}-{version}.war")
        server.exec(f"echo {shq(sha + '  ' + incoming + '/app.war')} | sha256sum -c -",
                    timeout=60)
        from .remote_operation import commit_staged
        return commit_staged(server, self.instance_id(server), self._token,
                             version, incoming, sha)

    def _stop(self, server, instance_id, token):
        stop_and_confirm(server, instance_id, token, self.unit)

    def _activate(self, server, instance_id, token, version):
        app = self.cfg["app_home"]
        command = (f"ln -sfn {shq(app + '/releases/' + version)} {shq(app + '/.CURRENT.tmp')} "
                   f"&& mv -Tf {shq(app + '/.CURRENT.tmp')} {shq(app + '/CURRENT')} && "
                   f"printf '%s\\n' {shq(version)} > {shq(app + '/.deploy_version')} && "
                   f"chmod 0644 {shq(app + '/.deploy_version')}")
        guarded(server, instance_id, token, command, timeout=30)

    def _clean(self, server, instance_id, token):
        app = self.cfg["app_home"]
        targets = [app + "/instance/webapps", app + "/instance/work",
                   app + "/instance/temp"]
        command = "; ".join(
            f"find {shq(p)} -mindepth 1 -maxdepth 1 -exec rm -rf -- {{}} +"
            for p in targets)
        guarded(server, instance_id, token, command, timeout=60)

    def _start(self, server, instance_id, token):
        guarded(server, instance_id, token,
                f"systemctl start {shq(self.unit)}.service && "
                f"systemctl is-active {shq(self.unit)}.service", timeout=60)

    def _probe(self, server):
        r = self.cfg["readiness"]
        return poll_http(server, self.cfg["port"], _health_path(self.cfg),
                         timeout=r["timeout"], interval=r["interval"],
                         expected_status=r["expected_status"],
                         body_contains=r["body_contains"])

    def _traffic(self, server, action, server_entry=None):
        if not getattr(self.d, "nacos", None) or not getattr(self.d.nacos, "configured", False):
            return {"ok": True, "status": "not_configured"}
        try:
            if action == "offline":
                self.d.nacos_offline(self.d._nacos_ip(server_entry))
            else:
                self.d.nacos_online(self.d._nacos_ip(server_entry), weight=1.0)
            return {"ok": True, "status": action}
        except Exception as e:
            return {"ok": False, "status": "failed", "error": str(e)}

    def _acquire(self, server, instance_id):
        token = secrets.token_hex(32)
        acquire(server, instance_id, token, "war_tomcat", "preflight")
        self._token = token
        return token

    def _release(self, server, instance_id, token, result):
        release(server, instance_id, token, result.to_dict())

    def deploy(self, server, server_entry=None):
        instance_id = self.instance_id(server)
        token = self._acquire(server, instance_id)
        phase = "preflight"
        modified = False
        target = getattr(self.d, "version", "")
        previous = ""
        self._phase(server, instance_id, token, "preflight")
        try:
            snap = self._snapshot(server)
            previous = snap["version"]
            self._last_current = previous
            self._preflight(server)
            self._stage(server, target, _artifact_sha(self.d.strategy, target),
                        secrets.token_hex(8))
            phase = "traffic"
            self._phase(server, instance_id, token, "traffic")
            off = self._traffic(server, "offline", server_entry)
            if not off["ok"]:
                # 摘流结果未知时不得停机；外部请求可能仍在执行，保留占用等人工核验
                return HostResult(False, "failed", phase, previous, previous,
                                  "running", "unknown", "not_needed", "unknown",
                                  "held", f"摘流失败: {off.get('error', '')}")
            phase = "stopping"
            self._phase(server, instance_id, token, "stopping")
            try:
                self._stop(server, instance_id, token)
            except Exception as stop_error:
                return self._stop_unknown(server, instance_id, token, target,
                                          previous, phase, stop_error,
                                          server_entry)
            modified = True
            phase = "switching"
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, target)
            self._clean(server, instance_id, token)
            phase = "starting"
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            phase = "health"
            self._phase(server, instance_id, token, "health")
            self._probe(server)
            tr = self._traffic(server, "online", server_entry)
            result = HostResult(True, "published", "done", target, previous,
                                "running", "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            if not tr["ok"]:
                result.ok = False
                result.operation_status = "traffic_failed"
                result.error = tr.get("error", "")
            self._release(server, instance_id, token, result)
            return result
        except Exception as e:
            if not modified:
                # 停机前失败：CURRENT 与运行实例未动，不触发恢复，也不改流量
                result = HostResult(False, "failed", phase, previous, previous,
                                    "running", "unknown", "not_needed",
                                    "unknown", "released", str(e))
                self._release(server, instance_id, token, result)
                return result
            return self._recover(server, instance_id, token, target, previous,
                                 phase, e, server_entry)

    def _stop_unknown(self, server, instance_id, token, target, previous,
                      phase, error, server_entry):
        """stop 未确认：只有旧版重新探活成功才能恢复流量并释放；否则保留 owner"""
        try:
            self._probe(server)
        except Exception:
            return HostResult(False, "unknown", phase, target, previous,
                              "unknown", "unknown", "unknown", "unknown",
                              "held", f"{error}；旧版本状态未确认")
        tr = self._traffic(server, "online", server_entry)
        result = HostResult(False, "failed", phase, target, previous,
                            "running", "healthy", "not_needed",
                            "restored" if tr["ok"] else "failed",
                            "released", str(error))
        self._release(server, instance_id, token, result)
        return result

    def _recover(self, server, instance_id, token, target, previous, phase,
                 error, server_entry):
        self._phase(server, instance_id, token, "recovery")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", phase, target, previous,
                              "unknown", "unknown", "unknown", "unknown",
                              "held", f"{error}; 停止失败: {stop_error}")
        if not previous:
            self._traffic(server, "offline", server_entry)
            result = HostResult(False, "failed", phase, target, "", "stopped",
                                "unavailable", "unavailable", "offline",
                                "released", str(error))
            self._release(server, instance_id, token, result)
            return result
        if not self._release_exists(server, previous):
            self._traffic(server, "offline", server_entry)
            result = HostResult(False, "failed", "recovery", previous, previous,
                                "stopped", "unavailable", "blocked", "offline",
                                "released", f"{error}；恢复源缺失")
            self._release(server, instance_id, token, result)
            return result
        try:
            self._traffic(server, "offline", server_entry)
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, previous)
            self._clean(server, instance_id, token)
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe(server)
            tr = self._traffic(server, "online", server_entry)
            result = HostResult(False, "failed", "recovered", previous, previous,
                                "running", "healthy", "succeeded",
                                "restored" if tr["ok"] else "failed",
                                "released", str(error))
        except Exception as recovery_error:
            try:
                self._stop(server, instance_id, token)
                result = HostResult(False, "failed", "recovery", previous,
                                    previous, "stopped", "unhealthy", "failed",
                                    "offline", "released",
                                    f"{error}; 恢复失败: {recovery_error}")
            except Exception as stop_error:
                result = HostResult(False, "unknown", "recovery", previous,
                                    previous, "unknown", "unknown", "unknown",
                                    "unknown", "held",
                                    f"{error}; 恢复失败: {recovery_error}; "
                                    f"停止失败: {stop_error}")
                return result
        self._release(server, instance_id, token, result)
        return result

    def rollback(self, server, target_version, server_entry=None, explicit=True):
        instance_id = self.instance_id(server)
        token = self._acquire(server, instance_id)
        self._phase(server, instance_id, token, "rollback_preflight")
        try:
            snap = self._snapshot(server)
            self._preflight(server)
        except Exception as e:
            return HostResult(False, "unknown", "rollback_preflight", "",
                              target_version, "unknown", "unknown", "unknown",
                              "unknown", "held", f"预检失败: {e}")
        if not self._release_exists(server, target_version):
            result = HostResult(False, "failed", "rollback_preflight",
                                snap["version"], target_version, "running",
                                "unknown", "blocked", "unknown", "released",
                                "回滚源缺失或校验失败")
            self._release(server, instance_id, token, result)
            return result
        self._phase(server, instance_id, token, "traffic")
        off = self._traffic(server, "offline", server_entry)
        if not off["ok"]:
            return HostResult(False, "failed", "traffic", snap["version"],
                              target_version, "running", "unknown",
                              "blocked", "unknown", "held",
                              f"摘流失败: {off.get('error', '')}")
        self._phase(server, instance_id, token, "rollback_stop")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "rollback_stop",
                              snap["version"], target_version, "unknown",
                              "unknown", "unknown", "unknown", "held",
                              f"停止失败: {stop_error}")
        try:
            self._phase(server, instance_id, token, "switching")
            self._activate(server, instance_id, token, target_version)
            self._clean(server, instance_id, token)
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe(server)
            tr = self._traffic(server, "online", server_entry)
            result = HostResult(True, "rolled_back", "done",
                                target_version, snap["version"], "running",
                                "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            self._release(server, instance_id, token, result)
            return result
        except Exception as e:
            return self._rollback_failed(server, instance_id, token,
                                         target_version, e)

    def _rollback_failed(self, server, instance_id, token, target_version,
                         error):
        """显式回滚自身失败：停止不健康实例并如实报告，不反向恢复另一个版本"""
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "rollback", "", target_version,
                              "unknown", "unknown", "failed", "unknown", "held",
                              f"{error}；停止失败: {stop_error}")
        snap = self._snapshot(server)
        result = HostResult(False, "failed", "rollback", snap["version"],
                            target_version, "stopped", "unhealthy", "failed",
                            "offline", "released", str(error))
        self._release(server, instance_id, token, result)
        return result

    def restart(self, server, server_entry=None, release_maintenance=False):
        instance_id = self.instance_id(server)
        manifest = self._read_manifest(server)
        maintenance = manifest.get("maintenance") or {}
        if maintenance.get("active") and not release_maintenance:
            raise RuntimeError("实例处于维护状态，需明确结束维护后再启动")
        if release_maintenance:
            maintenance = self._read_maintenance(server) or maintenance
        snapshot = ((maintenance.get("traffic_snapshot") or {})
                    if release_maintenance else {})
        token = self._acquire(server, instance_id)
        current = ""
        self._phase(server, instance_id, token, "restart_preflight")
        try:
            current = self._snapshot(server)["version"]
            self._preflight(server, allow_maintenance=release_maintenance)
        except Exception as e:
            return HostResult(False, "unknown", "restart_preflight", current,
                              "", "unknown", "unknown", "not_needed", "unknown",
                              "held", f"预检失败: {e}")
        self._phase(server, instance_id, token, "traffic")
        off = self._traffic(server, "offline", server_entry)
        if not off["ok"]:
            return HostResult(False, "failed", "traffic", current, "",
                              "running", "unknown", "not_needed", "unknown",
                              "held", f"摘流失败: {off.get('error', '')}")
        self._phase(server, instance_id, token, "restart_stop")
        try:
            self._stop(server, instance_id, token)
        except Exception as stop_error:
            return HostResult(False, "unknown", "restart_stop", current, "",
                              "unknown", "unknown", "not_needed", "unknown",
                              "held", f"停止失败: {stop_error}")
        try:
            self._phase(server, instance_id, token, "starting")
            self._start(server, instance_id, token)
            self._phase(server, instance_id, token, "health")
            self._probe(server)
            if release_maintenance:
                tr = self._restore_traffic(server, server_entry, snapshot)
                if tr["ok"]:
                    try:
                        self._clear_maintenance(server, instance_id, token)
                    except Exception as clear_error:
                        result = HostResult(
                            False, "restarted", "done", current, "",
                            "running", "healthy", "not_needed", "restored",
                            "released", f"维护标志清除失败: {clear_error}")
                        self._release(server, instance_id, token, result)
                        return result
            else:
                tr = self._traffic(server, "online", server_entry)
            result = HostResult(True, "restarted", "done", current, "",
                                "running", "healthy", "not_needed",
                                "restored" if tr["ok"] else "failed")
            if not tr["ok"]:
                result.ok = False
                result.operation_status = "traffic_failed"
                result.error = tr.get("error", "")
            self._release(server, instance_id, token, result)
            return result
        except Exception as e:
            try:
                self._stop(server, instance_id, token)
            except Exception as stop_error:
                return HostResult(False, "unknown", "restart", current, "",
                                  "unknown", "unknown", "not_needed",
                                  "unknown", "held",
                                  f"{e}；停止失败: {stop_error}")
            result = HostResult(False, "failed", "restart", current, "",
                                "stopped", "unhealthy", "not_needed",
                                "offline", "released", str(e))
            self._release(server, instance_id, token, result)
            return result
```

在文件底部补齐两个纯 helper：

```python
def _health_path(cfg):
    ctx = cfg["context_path"]
    return (ctx + cfg["readiness"]["path"]) if ctx else cfg["readiness"]["path"]


def _artifact_sha(strategy, version):
    local = strategy.local_artifact(version)
    import hashlib
    h = hashlib.sha256()
    with open(local, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
```

`_activate`/`_clean` 全部通过 Task 4 的 `guarded(server, instance_id, token, command, timeout=…)` 执行，不存在绕过令牌的裸命令。`_stage` 必须由调用方持有锁；`_snapshot` 的 `healthy` 字段只是基线占位，不能当作已验证，发布和恢复都必须重新 probe。

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_tomcat_lifecycle.py -v`
Expected: PASS（8 tests）

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`

---

### Task 9: WAR 策略注册、配置分派与 Deployer 严格接入

**Files:**
- Modify: `deploy_strategies/base.py`
- Create: `deploy_strategies/war_tomcat.py`
- Modify: `deploy_strategies/__init__.py`
- Modify: `deploy_strategies/process_config.py`
- Modify: `deploy_strategies/tomcat_instance.py`
- Modify: `service_unit.py`
- Modify: `webapp/importer.py`
- Modify: `deploy.py`
- Test: `tests/test_war_tomcat_strategy.py`
- Test: `tests/test_deploy_strict_integration.py`
- Test: `tests/test_config_roundtrip_jar.py`

**Interfaces:**
- Produces: `DeployStrategy.strict_host_lifecycle = False` 与
  `deploy_host/rollback_host/restart_host` 默认 `NotImplementedError`；旧七种策略不覆盖即走原路径。
- Produces: `WarTomcatStrategy`，`name="war_tomcat"`、`is_process=True`、
  `strict_host_lifecycle=True`、`supports_canary/restart/nacos=True`、
  `supports_prefetch=False`、`version_scheme="semver"`。
- Produces: `tomcat_instance.resolve_instance_id(server, app_home) -> str`：
  从归属文件读取随机 `instance_id`，缺失或为空抛 `RuntimeError`；发布、重启、
  配置、恢复一律通过它取同一个远端身份，禁止本地重新推导。
- Produces: `Deployer._strict_deploy`、`_rollback_all_strict`、
  `_rollback_first_host_strict`、`_log_host_result`。
- `process_config.parse_runtime_config/validate_runtime_config/derive_unit_name`
  对 `war_tomcat` 显式分派；`service_unit.render_unit` 返回 WAR unit，
  `onboard_unit` 对 WAR 拒绝，避免绕过实例接入事务。
- `webapp/importer.py` 的 `_import_projects` 对 `war_tomcat` 调用同一
  `validate_runtime_config`；校验失败跳过该服务并在结果消息中列出，不写入半套配置。

- [ ] **Step 1: 写失败测试**

```python
import unittest
from pathlib import Path
from unittest import mock

import service_unit
import deploy as deploy_mod
from deploy import Deployer
from deploy_strategies import create_strategy
from deploy_strategies.base import DeployStrategy, process_modes, registered_modes
from deploy_strategies.process_config import (derive_unit_name,
                                              parse_runtime_config,
                                              validate_runtime_config)
from deploy_strategies.tomcat_lifecycle import HostResult
from deploy_strategies.war_tomcat import WarTomcatStrategy

RUNTIME = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health"},
}


def make_deployer():
    d = Deployer.__new__(Deployer)
    d.project = "orders"
    d.project_id = 3
    d.version = "2.0.0"
    d.previous_version = "1.0.0"
    d.env = "test"
    d.dist_dir = Path("/tmp/dist")
    d.ssh_cfg = {}
    d.config = {"deploy": {"canary_wait": 0}}
    d.proj_cfg = {"deploy_mode": "war_tomcat", "image_name": "orders",
                  "runtime_config": RUNTIME}
    d._strategy = None
    d.target_modified = False
    d._record_host_state = mock.Mock()
    d._nacos_ip = lambda entry: (entry or {}).get("ip", "")
    d.nacos = mock.Mock(configured=False)
    return d


class WarStrategyTest(unittest.TestCase):
    def test_registry_capabilities_and_unit(self):
        self.assertIn("war_tomcat", registered_modes())
        self.assertIn("war_tomcat", process_modes())
        d = make_deployer()
        s = create_strategy("war_tomcat", d)
        self.assertIsInstance(s, WarTomcatStrategy)
        self.assertTrue(s.strict_host_lifecycle)
        self.assertTrue(s.is_process)
        self.assertTrue(s.supports_canary and s.supports_restart)
        self.assertFalse(s.supports_prefetch)
        self.assertEqual(s.unit, "orders-war")
        self.assertEqual(s.artifact_name("2.0.0"), "orders-2.0.0.war")

    def test_package_import_registers_war_mode(self):
        """生产启动只导入包 `deploy_strategies`；漏掉 __init__ 的导入时本用例必须红。
        用子进程隔离，避免本文件顶部的 war_tomcat 导入把注册副作用带进来。"""
        import subprocess
        import sys

        code = (
            "import deploy_strategies\n"
            "from deploy_strategies.base import process_modes, registered_modes\n"
            "assert 'war_tomcat' in registered_modes(), registered_modes()\n"
            "assert 'war_tomcat' in process_modes(), process_modes()\n"
            "print('ok')\n")
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "ok")

    def test_old_modes_do_not_enable_strict_lifecycle(self):
        self.assertFalse(DeployStrategy.strict_host_lifecycle)
        d = make_deployer()
        d.proj_cfg = {"deploy_mode": "jar_service", "image_name": "orders",
                      "runtime_config": {"app_home": "/opt/apps/orders",
                                         "java_bin": "/usr/bin/java"}}
        s = create_strategy("jar_service", d)
        self.assertFalse(s.strict_host_lifecycle)

    def test_config_and_unit_dispatch(self):
        cfg = parse_runtime_config(RUNTIME, "war_tomcat")
        self.assertEqual(cfg["tomcat_home"], "/opt/tomcat")
        self.assertEqual(validate_runtime_config(RUNTIME, "war_tomcat"), [])
        no_unit = {k: v for k, v in RUNTIME.items() if k != "unit_name"}
        self.assertEqual(derive_unit_name(no_unit, "orders", "orders",
                                          "war_tomcat"),
                         "smartops-orders-war")
        unit = service_unit.render_unit(RUNTIME, "orders", "war_tomcat")
        self.assertIn("org.apache.catalina.startup.Bootstrap start", unit)
        self.assertNotIn("catalina.sh", unit)

    def test_onboard_unit_refuses_war(self):
        with self.assertRaises(RuntimeError):
            service_unit.onboard_unit(object(), "orders-war", "[Service]\n",
                                      parse_runtime_config(RUNTIME, "war_tomcat"),
                                      "war_tomcat")

    def test_resolve_instance_id_reads_manifest_only(self):
        import base64

        from deploy_strategies import tomcat_instance as ti

        class Server:
            def __init__(self, payload):
                self.payload = payload

            def exec(self, cmd, timeout=60, stdin_data=None):
                return base64.b64encode(
                    self.payload.encode("utf-8")).decode("ascii")

        server = Server('{"instance_id": "abc123"}')
        self.assertEqual(ti.resolve_instance_id(server, "/opt/apps/orders"),
                         "abc123")
        with self.assertRaises(RuntimeError):
            ti.resolve_instance_id(Server("{}"), "/opt/apps/orders")


class StrictIntegrationTest(unittest.TestCase):
    def make(self, results):
        d = make_deployer()
        strategy = mock.Mock()
        strategy.strict_host_lifecycle = True
        strategy.deploy_host.side_effect = lambda server, entry=None: results[server.host]
        strategy.rollback_host.side_effect = (
            lambda server, target, entry=None: results[server.host])
        d._strategy = strategy
        return d, strategy

    def test_deploy_server_uses_strict_result_without_old_nacos(self):
        d, strategy = self.make({"a": HostResult(True, "published", "done",
                                                 "2.0.0", "1.0.0", "running",
                                                 "healthy")})
        server = mock.Mock(host="a")
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=server):
            self.assertTrue(d.deploy_server("a", {"ip": "a"}))
        strategy.deploy_host.assert_called_once()
        d._record_host_state.assert_called_once_with("a", "2.0.0", "1.0.0")
        self.assertFalse(d.nacos.online_ip.called)

    def test_deploy_server_failure_returns_false_and_keeps_recovery_status(self):
        d, strategy = self.make({"a": HostResult(False, "failed", "recovered",
                                                 "1.0.0", "1.0.0", "running",
                                                 "healthy", "succeeded")})
        with mock.patch.object(deploy_mod, "RemoteServer", return_value=mock.Mock(host="a")):
            self.assertFalse(d.deploy_server("a", {"ip": "a"}))
        d._record_host_state.assert_not_called()

    def test_rollback_all_strict_stops_after_first_failure(self):
        d, strategy = self.make({
            "a": HostResult(True, "rolled_back", "done", "1.0.0", "2.0.0"),
            "b": HostResult(False, "failed", "rollback", "2.0.0", "1.0.0"),
            "c": HostResult(True, "rolled_back", "done", "1.0.0", "2.0.0"),
        })
        servers = [{"ip": "a"}, {"ip": "b"}, {"ip": "c"}]
        with mock.patch.object(deploy_mod, "RemoteServer",
                               side_effect=lambda host, cfg: mock.Mock(host=host)):
            out = d._rollback_all_strict(servers)
        self.assertEqual(out, {"a": True, "b": False})
        self.assertEqual(strategy.rollback_host.call_count, 2)

    def test_first_host_rollback_uses_recorded_baseline(self):
        d, strategy = self.make({"a": HostResult(True, "rolled_back", "done")})
        with mock.patch.object(deploy_mod, "RemoteServer",
                               return_value=mock.Mock(host="a")):
            self.assertTrue(d._rollback_first_host_strict("a", {"ip": "a"}, "1.2.3"))
        strategy.rollback_host.assert_called_once()
        self.assertEqual(strategy.rollback_host.call_args.args[1], "1.2.3")


class WarImportTest(unittest.TestCase):
    def test_yaml_import_rejects_invalid_war(self):
        import shutil
        import tempfile
        import time

        from webapp import db as dbm
        from webapp.importer import _import_projects

        tmp = tempfile.mkdtemp(prefix="war_import_")
        self.addCleanup(shutil.rmtree, tmp, True)
        db = str(Path(tmp) / "t.db")
        dbm.init_db(db)
        dbm.create_record(db, "projects", dict(
            name="p", code="p", description="", git_org="",
            created_at=int(time.time())))
        path = Path(tmp) / "projects.yaml"
        path.write_text(
            "projects:\n"
            "  war:\n"
            "    deploy_mode: war_tomcat\n"
            "    runtime_config:\n"
            "      app_home: /opt/war\n", encoding="utf-8")
        ok, msg = _import_projects(db, path)
        self.assertTrue(ok)
        self.assertIn("war", msg)
        self.assertIsNone(dbm.get_service_by_name(db, 1, "war"))
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_war_tomcat_strategy.py tests/test_deploy_strict_integration.py -v`
Expected: FAIL，`ModuleNotFoundError: deploy_strategies.war_tomcat`

- [ ] **Step 3: 实现策略、分派与严格接入**

`deploy_strategies/base.py` 增加显式 opt-in 接口（放在能力标志与 `rollback_source_missing` 之后）：

```python
    # 严格主机生命周期：WAR/Tomcat 显式 opt-in，旧七种策略保持 False
    strict_host_lifecycle = False

    def deploy_host(self, server, server_entry: dict = None):
        raise NotImplementedError("当前发布方式未实现严格主机发布")

    def rollback_host(self, server, target_version: str,
                      server_entry: dict = None):
        raise NotImplementedError("当前发布方式未实现严格主机回滚")

    def restart_host(self, server, server_entry: dict = None,
                     release_maintenance: bool = False):
        raise NotImplementedError("当前发布方式未实现严格主机重启")
```

`deploy_strategies/tomcat_instance.py` 增加归属身份读取（随机 ID 只来自归属
文件，不在本地推导）：

```python
def resolve_instance_id(server, app_home: str) -> str:
    manifest = read_manifest(server, app_home)
    instance_id = (manifest or {}).get("instance_id") or ""
    if not instance_id:
        raise RuntimeError("实例未接入或归属清单缺少 instance_id")
    return instance_id
```

`deploy_strategies/__init__.py` 必须新增一行导入，否则生产启动路径
（`from deploy_strategies import create_strategy`）不会执行 `@register`，
`war_tomcat` 不会出现在 `registered_modes()` / `process_modes()` 中；只有直接
`import deploy_strategies.war_tomcat` 的测试会通过，掩盖该缺陷。在现有
`from . import python_process ...` 之后追加：

```python
from . import war_tomcat  # noqa: F401 — 触发 @register WarTomcatStrategy
```

`deploy_strategies/war_tomcat.py`：

```python
"""WAR/Tomcat 严格发布策略：所有主机动作走结构化生命周期。"""
import hashlib
import re
import shlex
from pathlib import Path

from .base import DeployStrategy, register
from . import tomcat_instance
from .tomcat_config import (derive_war_unit, parse_war_config,
                            validate_war_config)
from .tomcat_lifecycle import HostResult, WarTomcatLifecycle


@register
class WarTomcatStrategy(DeployStrategy):
    name = "war_tomcat"
    is_process = True
    artifact_label = "WAR"
    supports_nacos = True
    supports_canary = True
    supports_prefetch = False
    supports_restart = True
    strict_host_lifecycle = True
    version_scheme = "semver"

    def __init__(self, deployer):
        super().__init__(deployer)
        proj = getattr(deployer, "proj_cfg", None) or {}
        raw = proj.get("runtime_config")
        errors = validate_war_config(raw)
        if errors:
            raise ValueError("WAR 配置不合法: " + "；".join(errors))
        self.cfg = parse_war_config(raw)
        self.app_home = self.cfg["app_home"]
        self.service = proj.get("image_name") or deployer.project
        self.unit = derive_war_unit(self.cfg, self.service)
        self.last_results = {}
        self._ids = {}
        self.lifecycle = WarTomcatLifecycle(
            deployer, self.cfg, self.service, self.unit,
            instance_id=self.instance_id)

    def instance_id(self, server):
        """每台主机只从归属文件解析一次；未接入时直接失败，不生成临时身份。"""
        host = getattr(server, "host", "")
        if host not in self._ids:
            self._ids[host] = tomcat_instance.resolve_instance_id(
                server, self.app_home)
        return self._ids[host]

    def artifact_name(self, version: str) -> str:
        return f"{self.service}-{version or self.d.version}.war"

    def local_artifact(self, version: str = None) -> Path:
        return Path(getattr(self.d, "dist_dir", Path("dist"))) / \
            self.artifact_name(version or self.d.version)

    def check_local(self):
        p = self.local_artifact()
        side = p.with_name(p.name + ".sha256")
        if not p.is_file():
            raise RuntimeError(f"本地 WAR 不存在: {p}（先执行构建）")
        if not side.is_file():
            raise RuntimeError(f"缺少校验文件: {side}（构建会自动生成）")
        expected = side.read_text(encoding="utf-8").strip().split()[0]
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError(f"WAR 校验和不一致: {p}（请重新构建）")

    def query_current_version(self):
        import deploy as deploy_mod
        from common import (default_servers_file, get_ssh_config, load_yaml,
                            merge_server_ssh)
        servers = load_yaml(Path(default_servers_file(env=self.d.env))).get(
            "servers", [])
        if not servers:
            raise RuntimeError("服务器清单为空，无法查询当前版本")
        first = servers[0]
        server = deploy_mod.RemoteServer(
            first["ip"], merge_server_ssh(get_ssh_config(self.d.config), first))
        try:
            link = server.exec(
                f"readlink -f {shlex.quote(self.app_home + '/CURRENT')} "
                f"2>/dev/null || true", timeout=15).strip()
            if link:
                return link.rstrip("/").rsplit("/", 1)[-1]
            mark = server.exec(
                f"cat {shlex.quote(self.app_home + '/.deploy_version')} "
                f"2>/dev/null || true", timeout=15).strip()
            return mark or None
        finally:
            server.close()

    def next_version(self, current: str) -> str:
        import deploy as deploy_mod
        return deploy_mod.bump_patch_version(current)

    def prefetch(self, servers: list) -> list:
        return []  # 严格路径在持锁后暂存上传，不做无锁预传输

    def rollback_source_missing(self, server) -> bool:
        return False  # 缺源由 rollback_host 在停机前返回 blocked

    def _record(self, server, result):
        self.last_results[server.host] = result.to_dict()
        return result

    def deploy_host(self, server, server_entry: dict = None):
        return self._record(server, self.lifecycle.deploy(server, server_entry))

    def rollback_host(self, server, target_version: str,
                      server_entry: dict = None):
        # T8 评审 carry-forward：版本名是 API 入口参数，必须先校验字符集，
        # 防 `../` 形状越过 releases/ 解析（shq 只防注入，不防路径穿越）
        if not re.fullmatch(r"[A-Za-z0-9._-]+", target_version or ""):
            return HostResult(False, "failed", "rollback_preflight", "",
                              target_version, "running", "unknown", "blocked",
                              "unknown", "released", "回滚版本名非法")
        return self._record(
            server, self.lifecycle.rollback(server, target_version, server_entry))

    def restart_host(self, server, server_entry: dict = None,
                     release_maintenance: bool = False):
        return self._record(
            server, self.lifecycle.restart(server, server_entry,
                                           release_maintenance))
```

`deploy_strategies/process_config.py` 的三个函数改为以下完整实现（旧分支逐行保留，只增加 WAR 分派）：

```python
def parse_runtime_config(raw, deploy_mode: str = "jar_service") -> dict:
    """按 deploy_mode 分派；未知模式返回 {}（container/static 不消费该配置）"""
    mode = deploy_mode or "container"
    if mode == "war_tomcat":
        from .tomcat_config import parse_war_config
        return parse_war_config(raw)
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


def validate_runtime_config(raw, deploy_mode: str) -> list:
    """按 deploy_mode 分派；非进程族方式配置原样忽略（返回 []）"""
    mode = deploy_mode or "container"
    if mode == "war_tomcat":
        from .tomcat_config import validate_war_config
        return validate_war_config(raw)
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
            elif any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F
                     for c in node_bin):
                errors.append("runtime_config.node_bin 必须是绝对路径，"
                              "且不含空白与控制字符")
        else:
            pv = cfg.get("python_version")
            if pv is not None and str(pv) and not _PYVER_RE.match(str(pv)):
                errors.append("runtime_config.python_version 形如 3.11（major.minor）")
            req = cfg.get("requirements")
            if req is not None:
                if not isinstance(req, str) or not req.strip() or req.startswith("/"):
                    errors.append("runtime_config.requirements 须为项目内相对路径")
                elif (".." in req.replace("\\", "/").split("/")
                      or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F
                             for c in req)):
                    errors.append("runtime_config.requirements 须为项目内相对路径"
                                  "（不含空白、控制字符与 .. 段）")
    return errors


def derive_unit_name(runtime_config, image_name: str = "",
                     service_name: str = "", deploy_mode: str = "") -> str:
    """平台统一口径：unit_name → image_name → 服务名 → app（不含 .service）"""
    cfg = _as_dict(runtime_config)
    if deploy_mode == "war_tomcat":
        from .tomcat_config import derive_war_unit, parse_war_config
        return derive_war_unit(parse_war_config(cfg),
                               image_name or service_name or "app")
    return str(cfg.get("unit_name") or image_name or service_name or "app")
```

`service_unit.py` 的两个函数改为以下完整实现（`render_unit` 最前面分派；`onboard_unit` 在读取任何远端状态前拒绝）：

```python
def render_unit(runtime_config, service: str, deploy_mode: str = "jar_service") -> str:
    """按运行时配置渲染 unit：ExecStart 固定引用 CURRENT（或 venv），发布只换链接不改 unit"""
    if deploy_mode == "war_tomcat":
        from deploy_strategies.tomcat_config import parse_war_config, render_war_unit
        return render_war_unit(parse_war_config(runtime_config), service)
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
    """在单台服务器接入 unit（幂等）：拒绝接管、写文件、reload、enable"""
    if deploy_mode == "war_tomcat":
        raise RuntimeError("WAR/Tomcat 实例必须通过管理员实例接入事务，不能直接写 unit")
    expected = exec_start_marker(cfg, deploy_mode)
    existing = server.exec(
        f"systemctl show -p ExecStart --value {shlex.quote(unit)}.service "
        f"2>/dev/null || true", timeout=15)
    if existing.strip() and expected not in existing:
        raise RuntimeError(
            f"拒绝接管非平台创建的单元 {unit}.service（ExecStart 未指向 {expected}）")
    path = f"/etc/systemd/system/{unit}.service"
    payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
    server.exec(
        f"printf %s {shlex.quote(payload)} | base64 -d > {shlex.quote(path)} && "
        f"systemctl daemon-reload && systemctl enable {shlex.quote(unit)}.service",
        timeout=60)
    log(f"[{getattr(server, 'host', '?')}] 单元已接入: {unit}.service")
```

`webapp/importer.py` 的 `_import_projects` 改为以下完整实现（插入前先按同一
WAR 校验器拦截；旧服务导入路径不变）：

```python
def _import_projects(db_path: str, path: Path) -> tuple:
    """projects.yaml → 1 项目 + N 服务；返回 (ok, message)"""
    data = load_yaml(path)
    projects = data.get("projects", {})
    if not projects:
        return False, "projects.yaml 无 projects 段"
    pid = _first_project_id(db_path)
    if pid is None:
        code = "legacy"
        pid = dbm.create_record(db_path, "projects", dict(
            name="存量项目", code=code, description="从旧配置导入", git_org="",
            created_at=int(time.time())))
    else:
        code = dbm.get_record(db_path, "projects", pid)["code"]
    n = 0
    skipped = []
    for name, cfg in projects.items():
        if dbm.get_service_by_name(db_path, pid, name):
            continue
        deploy_mode = cfg.get("deploy_mode") or "container"
        if deploy_mode == "war_tomcat":
            from deploy_strategies.process_config import validate_runtime_config
            errs = validate_runtime_config(cfg.get("runtime_config"), deploy_mode)
            if errs:
                skipped.append(f"{name}（{'；'.join(errs)}）")
                continue
        build_order = cfg.get("build_order", []) or []
        normalised_build_order = []
        for b in build_order:
            if "commands" in b:
                normalised_build_order.append({"name": b.get("name", ""),
                                               "path": b.get("path", ""),
                                               "commands": b["commands"],
                                               **({"env": b["env"]} if b.get("env") else {})})
            else:
                normalised_build_order.append({"name": b.get("name", ""),
                                               "path": b.get("path", ""),
                                               "goal": b.get("goal", "package"),
                                               "extra_profile": b.get("extra_profile", ""),
                                               **({"env": b["env"]} if b.get("env") else {})})
        dbm.create_record(db_path, "services", dict(
            project_id=pid, name=name,
            image_name=cfg.get("image_name", name),
            service_name=cfg.get("service_name", name),
            docker_context=cfg.get("docker_context", ""),
            dockerfile=cfg.get("dockerfile", ""),
            build_order=normalised_build_order,
            build_type=cfg.get("build_type", "maven"),
            deploy_mode=deploy_mode,
            runtime_config=cfg.get("runtime_config") or {},
            site_root=cfg.get("site_root", ""),
            backup_dir=cfg.get("backup_dir", ""),
            static_dist_dir=cfg.get("static_dist_dir", ""),
            zip_prefix=cfg.get("zip_prefix", ""),
            entry_file=cfg.get("entry_file", ""),
            file_mode=cfg.get("file_mode", ""),
            nacos_groups=cfg.get("nacos_groups", []) or [],
            dubbo_services=cfg.get("dubbo_services", []) or [],
            snailjob_group=cfg.get("snailjob_group", ""),
            health_url=cfg.get("health_url", ""),
            health_port=cfg.get("health_port", 0),
            log_error_keywords=cfg.get("log_error_keywords", []) or [],
            clear_log_dir=cfg.get("clear_log_dir", ""),
            git_repo=cfg.get("git_repo", ""),
            git_branch=cfg.get("git_branch", ""),
            git_pull_before_build=1 if cfg.get("git_pull_before_build") else 0,
            git_tag_enabled=1 if cfg.get("git_tag_enabled", True) else 0,
            enabled=1))
        n += 1
    msg = f"项目 code={code}，导入服务 {n} 个"
    if skipped:
        msg += "，跳过校验失败: " + ", ".join(skipped)
    return True, msg
```

`deploy.py` 增加三个辅助方法（放在 `deploy_server` 之前）：

```python
    def _log_host_result(self, host: str, result):
        log(f"[{host}] 操作={result.operation_status} 阶段={result.phase} "
            f"版本={result.current_version or '-'} "
            f"恢复目标={result.recovery_target or '-'} "
            f"进程={result.process_state} 应用={result.app_health} "
            f"恢复={result.recovery_status} 流量={result.traffic_status} "
            f"锁={result.lock_state}" +
            (f" 错误={result.error}" if result.error else ""))

    def _strict_deploy(self, host: str, server, server_entry: dict = None) -> bool:
        try:
            result = self.strategy.deploy_host(server, server_entry)
        except Exception as e:
            log(f"[{host}] 严格发布异常: {e}")
            return False
        self._log_host_result(host, result)
        if result.ok:
            self._record_host_state(host, result.current_version or self.version,
                                    result.recovery_target)
        return bool(result.ok)

    def _rollback_first_host_strict(self, host: str, server_entry: dict,
                                    baseline: str) -> bool:
        if not baseline:
            log(f"[{host}] 没有该主机本次发布记录的恢复基线，拒绝首台回滚")
            return False
        server = RemoteServer(host, merge_server_ssh(self.ssh_cfg, server_entry))
        try:
            result = self.strategy.rollback_host(server, baseline, server_entry)
            self._log_host_result(host, result)
            if result.ok:
                self._record_host_state(host, result.current_version, baseline)
            return bool(result.ok)
        except Exception as e:
            log(f"[{host}] 首台严格回滚异常: {e}")
            return False
        finally:
            server.close()

    def _rollback_all_strict(self, servers: list) -> dict:
        """显式批量回滚：按绑定顺序逐台执行，一台失败/未知即停止后续"""
        results = {}
        for srv in servers:
            host = srv["ip"]
            server = RemoteServer(host, merge_server_ssh(self.ssh_cfg, srv))
            try:
                result = self.strategy.rollback_host(server,
                                                     self.previous_version, srv)
                self._log_host_result(host, result)
                results[host] = bool(result.ok)
                if result.ok:
                    self._record_host_state(host, result.current_version,
                                            self.previous_version)
            except Exception as e:
                log(f"[{host}] 严格回滚异常: {e}")
                results[host] = False
            finally:
                server.close()
            if not results[host]:
                skipped = [s["ip"] for s in servers
                           if s["ip"] not in results]
                if skipped:
                    log(f"回滚失败，停止后续主机: {skipped}")
                break
        return results
```

`deploy_server` 在创建 `RemoteServer` 后、任何 Nacos/旧五阶段之前插入：

```python
        if getattr(self.strategy, "strict_host_lifecycle", False):
            try:
                return self._strict_deploy(host, server, server_entry)
            finally:
                server.close()
```

金丝雀首台回滚分支（`decision == "rollback"`）在严格能力下改为：

```python
                                if self.strategy.strict_host_lifecycle:
                                    recorded = self.strategy.last_results.get(host) or {}
                                    baseline = recorded.get("recovery_target") or ""
                                    self._rollback_first_host_strict(host, srv, baseline)
                                    success_all = False
                                    break
```

其余旧分支保持原样。`rollback_all` 在暂停 SnailJob 之后，把原来的
`log(">>> 步骤 2/3: 并行回滚所有服务器...")` 替换为：

```python
        if self.strategy.strict_host_lifecycle:
            log(">>> 步骤 2/3: WAR 逐台严格回滚（失败即停止）")
            rollback_results = self._rollback_all_strict(servers)
        else:
            log(">>> 步骤 2/3: 并行回滚所有服务器（各服务器独立 SSH 连接，传输+回滚并行）")
```

然后把原函数中从 `def _rollback_one(srv: dict) -> tuple:` 到本次替换点之后的全部
原有并行回滚代码整体缩进 4 个空格，放进上面的 `else:` 分支；逻辑、日志与汇总
结构不改。严格分支返回的 `{host: bool}` 与旧分支汇总结构一致，后续结果输出
代码无需改动。

- [ ] **Step 4: 运行通过并确认旧模式隔离**

Run: `python -m pytest tests/test_war_tomcat_strategy.py tests/test_deploy_strict_integration.py tests/test_config_roundtrip_jar.py tests/test_jar_service.py tests/test_static_archive.py -v`
Expected: PASS；旧策略 `strict_host_lifecycle` 为 False，部署与 YAML 往返测试仍走原分支。

- [ ] **Step 5: 检查范围，不提交**

Run: `git diff --stat`
Expected: 仅本任务列出的文件与测试；未提交。

---

### Task 10: 重启任务、监控与日志采集的 WAR 接入

**Files:**
- Modify: `scripts/nacos_manager.py`
- Create: `deploy_strategies/tomcat_restart.py`
- Modify: `deploy_strategies/tomcat_instance.py`
- Modify: `deploy_strategies/process_service.py`
- Modify: `webapp/task_manager.py`
- Modify: `webapp/monitor.py`
- Modify: `webapp/log_collector.py`
- Modify: `webapp/routes/config_routes.py`
- Modify: `webapp/routes/deploy_routes.py`
- Test: `tests/test_war_restart.py`
- Test: `tests/test_war_observability.py`

**Interfaces:**
- Consumes: Task 4 `inspect/lock_paths`、Task 5 `probe_http`、Task 6
  `read_manifest`、Task 8 `WarTomcatLifecycle.restart`、Task 9
  `resolve_instance_id`。
- Produces: `NacosClient(env=None, config=None)`；`config` 非空时不再读 YAML。
- Produces: `RestartContext(env_obj, rec)`，提供 `nacos`、`nacos_offline`、
  `nacos_online`、`capture_traffic`、`restore_traffic`、`_nacos_ip`，作为
  `WarTomcatLifecycle` 的最小 `d`。
- Produces: `tomcat_instance.config_status(manifest, instance_id, project_id,
  env_name, service) -> str`，取值 `unmanaged/owner_mismatch/maintenance/managed`。
- Produces: `aggregate_config_status(rows) -> str`、`aggregate_operation_state(rows) -> str`。
- Produces: `_run_war_restart(f, rec, env_obj, servers, handle) -> str`，逐台调用
  严格重启；健康超时/停止失败/锁未知都计入失败。
- 旧七种模式的重启、监控、日志路径不得改变；WAR 只在显式分支生效。

- [ ] **Step 1: 写失败测试**

`tests/test_war_restart.py`：

```python
import os
import tempfile
import unittest
from unittest import mock

from deploy_strategies.tomcat_lifecycle import HostResult
from deploy_strategies.tomcat_restart import RestartContext
from webapp import db as dbm

WAR_CFG = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}


def _make_war_svc(db, pid):
    return dbm.create_record(db, "services", dict(
        project_id=pid, name="orders", image_name="orders", service_name="orders",
        deploy_mode="war_tomcat", runtime_config=dict(WAR_CFG),
        docker_context="", dockerfile="", build_order="[]", nacos_groups="[]",
        dubbo_services="[]", snailjob_group="", health_url="", health_port=0,
        log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))


class FakeServer:
    def __init__(self, host, ssh_cfg):
        self.host = host
        self.ssh_cfg = ssh_cfg

    def close(self):
        pass


class WarRestartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.svc = _make_war_svc(self.db, self.pid)
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh='{"user":"root"}',
            build="{}", deploy="{}", monitor_enabled=1))
        for i, host in enumerate(["10.0.0.1", "10.0.0.2"]):
            sid = dbm.create_record(self.db, "servers", dict(
                host=host, ssh_port=22, ssh_user="", ssh_password="",
                ssh_key_file="", nacos_ip="", remark="", enabled=1))
            dbm.create_record(self.db, "env_servers", dict(
                env_id=self.env, server_id=sid, order_no=i))
        self.log = os.path.join(self.tmp, "restart.log")
        from webapp.task_manager import _RestartHandle
        self.handle = _RestartHandle()
        self.task = dict(id=1, project_id=self.pid, ref_id=self.svc,
                         env="prod", type="restart")

    def _run(self):
        from webapp.task_manager import _run_restart
        return _run_restart(self.db, self.task, self.log, self.handle)

    def _result(self, ok=True):
        return HostResult(ok, "restarted" if ok else "failed",
                          "done" if ok else "restart", "1.0.0", "",
                          "running" if ok else "stopped",
                          "healthy" if ok else "unhealthy", "not_needed",
                          "restored" if ok else "offline", "released",
                          "" if ok else "健康检查超时")

    def test_success_uses_strict_lifecycle(self):
        fake = mock.Mock()
        fake.restart.return_value = self._result(True)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer), \
                mock.patch("webapp.monitor._ssh_exec_multi") as generic:
            status = self._run()
        self.assertEqual(status, "success")
        self.assertEqual(fake.restart.call_count, 2)
        generic.assert_not_called()
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("2 台成功", text)
        self.assertIn("应用健康", text)

    def test_release_maintenance_flag_is_propagated(self):
        self.task["extra"] = ["--release-maintenance"]
        fake = mock.Mock()
        fake.restart.return_value = self._result(True)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "success")
        self.assertTrue(fake.restart.call_args.kwargs["release_maintenance"])

    def test_health_timeout_is_task_failure(self):
        fake = mock.Mock()
        fake.restart.return_value = self._result(False)
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("应用=unhealthy", text)
        self.assertIn("健康检查超时", text)
        self.assertNotIn("仍视为成功", text)

    def test_lock_unknown_is_not_success(self):
        fake = mock.Mock()
        fake.restart.return_value = HostResult(
            False, "unknown", "restart_stop", "1.0.0", "",
            "unknown", "unknown", "not_needed", "unknown", "held",
            "停止失败: timeout")
        with mock.patch("webapp.task_manager._war_lifecycle",
                        return_value=fake), \
                mock.patch("deploy.RemoteServer", FakeServer):
            status = self._run()
        self.assertEqual(status, "failed")
        with open(self.log, encoding="utf-8") as f:
            self.assertIn("锁=held", f.read())


class RestartContextTest(unittest.TestCase):
    def test_offline_and_online_use_db_nacos_and_service_groups(self):
        with mock.patch("deploy_strategies.tomcat_restart.NacosClient") as nc:
            client = nc.return_value
            client.configured = True
            client.group = "DEFAULT_GROUP"
            client.online_ip.return_value = True
            ctx = RestartContext(
                {"name": "prod", "nacos": {"url": "http://nacos"}},
                {"name": "orders", "service_name": "orders-svc",
                 "nacos_groups": '["G1"]'})
        nc.assert_called_once_with(config={"nacos": {"url": "http://nacos"}})
        ctx.nacos_offline("10.0.0.1")
        ctx.nacos_online("10.0.0.1", 1.0)
        client.offline_ip.assert_called_once_with(
            "orders-svc", "10.0.0.1", group="G1")
        client.wait_for_absence.assert_called_once()
        client.online_ip.assert_called_once_with(
            "orders-svc", "10.0.0.1", 1.0, group="G1")
        client.wait_for_healthy.assert_called_once()
        self.assertEqual(ctx._nacos_ip(
            {"host": "10.0.0.1", "nacos_ip": "10.1.1.1"}), "10.1.1.1")
        self.assertEqual(ctx._nacos_ip({"host": "10.0.0.1"}), "10.0.0.1")

    def test_capture_and_restore_preserve_weight_and_skip_disabled(self):
        with mock.patch("deploy_strategies.tomcat_restart.NacosClient") as nc:
            client = nc.return_value
            client.configured = True
            client.group = "DEFAULT_GROUP"
            client.list_instances.return_value = [
                {"ip": "10.0.0.1", "port": 8080, "enabled": True,
                 "weight": 7, "clusterName": "D1", "ephemeral": True},
                {"ip": "10.0.0.1", "port": 8081, "enabled": False,
                 "weight": 0, "clusterName": "D1", "ephemeral": True},
                {"ip": "10.0.0.2", "port": 8080, "enabled": True,
                 "weight": 3, "clusterName": "D1", "ephemeral": True},
            ]
            ctx = RestartContext({"name": "prod", "nacos": {}},
                                 {"name": "orders", "nacos_groups": "[]"})
            snap = ctx.capture_traffic("10.0.0.1")
            self.assertEqual(len(snap["orders|DEFAULT_GROUP"]), 2)
            ctx.restore_traffic("10.0.0.1", snap)
        client.set_instance.assert_called_once_with(
            "orders", "10.0.0.1", 8080, enabled=True, weight=7.0,
            cluster="D1", group="DEFAULT_GROUP", ephemeral=True)
        client.wait_for_healthy.assert_called_once()
```

`tests/test_war_observability.py`：

```python
import os
import tempfile
import unittest
from unittest import mock

from deploy_strategies import tomcat_instance as ti
from webapp import db as dbm
from webapp.log_collector import LogCollector
from webapp.monitor import MonitorScheduler

WAR_CFG = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}


def _raw(active="active"):
    return {"load_state": "loaded", "active_state": active,
            "sub_state": "running" if active == "active" else "dead",
            "pid": 4242 if active == "active" else 0, "restarts": 1,
            "active_enter_mono": 9600_000_000.0, "version": "1.0.0",
            "link_version": "", "cpu_ticks": 1000, "rss_pages": 25600}


class WarObservabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        dbm.init_db(self.db)
        self.pid = dbm.create_record(self.db, "projects", dict(
            name="p", code="p", description="", git_org="", created_at=0))
        self.env = dbm.create_record(self.db, "environments", dict(
            project_id=self.pid, name="prod", is_default=1, registry="{}",
            nacos="{}", mysql_snailjob="{}", ssh="{}", build="{}",
            deploy="{}", monitor_enabled=1))
        dbm.create_record(self.db, "services", dict(
            project_id=self.pid, name="orders", image_name="orders",
            service_name="orders", deploy_mode="war_tomcat",
            runtime_config=dict(WAR_CFG), docker_context="", dockerfile="",
            build_order="[]", nacos_groups="[]", dubbo_services="[]",
            snailjob_group="", health_url="", health_port=0,
            log_error_keywords="[]", clear_log_dir="", git_repo="", enabled=1))
        srv = dbm.create_record(self.db, "servers", dict(
            host="1.2.3.4", ssh_port=22, ssh_user="root", ssh_password="pw",
            ssh_key_file="", nacos_ip="", remark="", enabled=1))
        dbm.bind_server(self.db, self.env, srv)
        self.iid = "a" * 32

    def _fake_probe(self, host, port, ssh_cfg, service_names, timeout=10,
                    skip_extended=False, units=None):
        return {"ssh_ok": True, "containers": {}, "containers_stopped": {},
                "container_list": [], "container_status": {}, "resources": {},
                "host_metrics": {}, "extended": {}, "error": "",
                "jar_probe": {"uptime": 10000.0, "clk_tck": 100,
                              "units": {"orders-war": _raw()}}}

    def _evidence(self, instance_id=None, maintenance=False, owner=None,
                  phase=""):
        manifest = {"instance_id": instance_id or self.iid,
                    "project_id": self.pid, "env": "prod", "service": "orders",
                    "maintenance": {"active": maintenance}}
        return {"orders": {"instance_id": manifest["instance_id"],
                           "manifest": manifest, "owner": owner or {},
                           "phase": phase}}

    def _cycle(self, evidence, health_ok=True):
        with mock.patch("webapp.monitor._probe_server_full",
                        side_effect=self._fake_probe), \
                mock.patch("webapp.monitor._probe_war_evidence",
                           return_value=evidence), \
                mock.patch("webapp.monitor._probe_war_health",
                           return_value=("1.2.3.4", [
                               ("orders", {"ok": health_ok, "ms": 5,
                                           "method": "http_strict"})])), \
                mock.patch("webapp.db.save_response_time"):
            sched = MonitorScheduler(self.db, 30)
            sched._cycle()
        return sched.get_overview(self.pid, "prod")

    def test_war_unit_and_unmanaged_status(self):
        ov = self._cycle({})
        st = ov["services"]["orders"]
        # 进程在跑、应用健康，但未接入归属 → 不能显示为 online
        self.assertEqual(st["status"], "unmanaged")
        self.assertEqual(st["app_health"], "ok")
        self.assertEqual(st["unit"], "orders-war")
        self.assertEqual(st["config_status"], "unmanaged")
        self.assertEqual(st["operation_state"], "idle")
        self.assertEqual(st["process_status"], "online")
        self.assertEqual(st["recovery_status"], "not_needed")
        self.assertEqual(st["hosts"][0]["app_health"], "ok")
        self.assertEqual(st["hosts"][0]["process_status"], "online")
        self.assertEqual(st["hosts"][0]["recovery_status"], "not_needed")
        self.assertTrue(ov["health_checks"]["orders"]["ok"])

    def test_recovery_status_comes_from_last_result(self):
        ev = self._evidence()
        ev["orders"]["last_result"] = {"recovery_status": "succeeded"}
        st = self._cycle(ev)["services"]["orders"]
        self.assertEqual(st["recovery_status"], "succeeded")
        self.assertEqual(st["hosts"][0]["recovery_status"], "succeeded")
        # 未知不能等同于成功
        ev["orders"]["last_result"] = {}
        st = self._cycle(ev)["services"]["orders"]
        self.assertEqual(st["recovery_status"], "unknown")
        self.assertNotEqual(st["recovery_status"], "succeeded")

    def test_war_version_uses_current_link_and_reports_drift(self):
        ev = self._evidence()
        ev["orders"].update({"current_version": "2.0.0",
                             "deploy_version": "1.0.0",
                             "version_drift": True})
        row = self._cycle(ev)["services"]["orders"]["hosts"][0]
        # CURRENT 是权威；.deploy_version 落后时只报漂移，不静默采用旧值
        self.assertEqual(row["version"], "2.0.0")
        self.assertTrue(row["version_drift"])

        ev["orders"].update({"current_version": "2.0.0",
                             "deploy_version": "2.0.0",
                             "version_drift": False})
        row = self._cycle(ev)["services"]["orders"]["hosts"][0]
        self.assertEqual(row["version"], "2.0.0")
        self.assertFalse(row["version_drift"])

    def test_maintenance_and_in_progress(self):
        ov = self._cycle(self._evidence(maintenance=True,
                                        owner={"token": "t"},
                                        phase="health"))
        st = ov["services"]["orders"]
        self.assertEqual(st["config_status"], "maintenance")
        self.assertEqual(st["operation_state"], "in_progress")
        self.assertEqual(st["operation_phase"], "health")
        self.assertEqual(st["hosts"][0]["operation_phase"], "health")

    def test_idle_has_no_operation_phase(self):
        st = self._cycle(self._evidence(phase="health"))["services"]["orders"]
        self.assertEqual(st["operation_state"], "idle")
        self.assertEqual(st["operation_phase"], "")

    def test_owner_mismatch_is_not_managed(self):
        ov = self._cycle(self._evidence(instance_id="other"))
        self.assertEqual(ov["services"]["orders"]["config_status"],
                         "owner_mismatch")

    def test_war_health_failure_is_recorded(self):
        ov = self._cycle(self._evidence(), health_ok=False)
        self.assertNotIn("orders", ov["health_checks"])
        st = ov["services"]["orders"]
        self.assertEqual(st["status"], "unhealthy")
        self.assertEqual(st["app_health"], "failed")
        # 进程仍在运行，只是应用健康失败：process_status 保持 online
        self.assertEqual(st["process_status"], "online")
        self.assertEqual(st["hosts"][0]["app_health"], "failed")
        self.assertEqual(st["hosts"][0]["process_status"], "online")
        self.assertEqual(st["hosts"][0]["status"], "unhealthy")

    def test_config_status_precedence(self):
        self.assertEqual(ti.config_status({}, "i", 1, "prod", "orders"),
                         "unmanaged")
        self.assertEqual(ti.aggregate_config_status([
            {"config_status": "managed"},
            {"config_status": "owner_mismatch"},
            {"config_status": "maintenance"}]), "owner_mismatch")
        self.assertEqual(ti.aggregate_config_status([
            {"config_status": "managed"}, {"config_status": "maintenance"}]),
            "maintenance")

    def test_log_collector_derives_war_unit(self):
        collector = LogCollector(self.db, os.path.join(self.tmp, "logs"),
                                 ssh_func=lambda *a, **k: "")
        data = collector._load_process_services()
        self.assertEqual(data[self.pid][0]["unit"], "orders-war")

    def test_default_war_unit_without_unit_name(self):
        # 未显式配置 unit_name 时，监控与日志都必须按 WAR 规则得
        # smartops-{service}-war；漏传 deploy_mode 会退回服务名并长期误报
        cfg = dict(WAR_CFG)
        cfg.pop("unit_name")
        rec = dbm.get_service_by_name(self.db, self.pid, "orders")
        dbm.update_record(self.db, "services", rec["id"],
                          {"runtime_config": cfg})
        collector = LogCollector(self.db, os.path.join(self.tmp, "logs"),
                                 ssh_func=lambda *a, **k: "")
        data = collector._load_process_services()
        self.assertEqual(data[self.pid][0]["unit"], "smartops-orders-war")

        orig = self._fake_probe

        def fake(host, port, ssh_cfg, service_names, timeout=10,
                 skip_extended=False, units=None):
            r = orig(host, port, ssh_cfg, service_names, timeout,
                     skip_extended, units)
            r["jar_probe"]["units"] = {"smartops-orders-war": _raw()}
            return r

        with mock.patch("webapp.monitor._probe_server_full", side_effect=fake), \
                mock.patch("webapp.monitor._probe_war_evidence",
                           return_value=self._evidence()), \
                mock.patch("webapp.monitor._probe_war_health",
                           return_value=("1.2.3.4", [
                               ("orders", {"ok": True, "ms": 5,
                                           "method": "http_strict"})])), \
                mock.patch("webapp.db.save_response_time"):
            sched = MonitorScheduler(self.db, 30)
            sched._cycle()
        ov = sched.get_overview(self.pid, "prod")
        self.assertEqual(ov["services"]["orders"]["unit"],
                         "smartops-orders-war")
        self.assertEqual(ov["services"]["orders"]["app_health"], "ok")
        self.assertEqual(ov["services"]["orders"]["status"], "unmanaged")
```

- [ ] **Step 2: 运行并确认失败**

Run:
`python -m pytest tests/test_war_restart.py tests/test_war_observability.py -v`
Expected: FAIL，`ModuleNotFoundError: deploy_strategies.tomcat_restart` /
`AttributeError: config_status`。

- [ ] **Step 3: 实现 Nacos 显式配置与重启上下文**

`scripts/nacos_manager.py` 仅改构造签名，默认行为不变：

```python
class NacosClient:
    def __init__(self, env: str = None, config: dict = None):
        config = config if config is not None else load_config(env=env)
        auth = get_nacos_auth(config)
        self.base = auth["url"]
        self.configured = bool(self.base)
        self.namespace = auth["namespace"]
        self.group = auth["group"]
        self.username = auth["username"]
        self.password = auth["password"]
        self._token = None
        self._token_expire = 0
        self._snapshots = {}
        self._snapshotted_keys = set()
```

新建 `deploy_strategies/tomcat_restart.py`：

```python
"""重启任务的最小发布上下文：只提供 Nacos 摘挂流与版本字段。"""
import json

from scripts.nacos_manager import NacosClient


class RestartContext:
    def __init__(self, env_obj, rec):
        env_obj = env_obj or {}
        self.version = ""
        self.previous_version = ""
        self.nacos = NacosClient(config={"nacos": env_obj.get("nacos") or {}})
        self.services = [rec.get("service_name") or rec["name"]]
        groups = rec.get("nacos_groups") or []
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except ValueError:
                groups = []
        self.groups = groups or ([self.nacos.group] if self.nacos.configured
                                 else [])

    def _nacos_ip(self, server_entry=None):
        if not server_entry:
            return ""
        return server_entry.get("nacos_ip") or server_entry.get("host") or ""

    def nacos_offline(self, host):
        for svc in self.services:
            for grp in self.groups:
                self.nacos.offline_ip(svc, host, group=grp)
        for svc in self.services:
            for grp in self.groups:
                self.nacos.wait_for_absence(svc, host, timeout=60, group=grp)

    def nacos_online(self, host, weight=1.0):
        restored = False
        for svc in self.services:
            for grp in self.groups:
                if self.nacos.online_ip(svc, host, weight, group=grp):
                    restored = True
        if not restored:
            return
        for svc in self.services:
            for grp in self.groups:
                self.nacos.wait_for_healthy(svc, host, timeout=120, group=grp)

    def capture_traffic(self, host):
        """摘流前快照：记录该 IP 在各服务/分组下的端口、开关、权重与集群。"""
        snapshot = {}
        for svc in self.services:
            for grp in self.groups:
                rows = []
                for h in self.nacos.list_instances(svc, group=grp):
                    if h.get("ip") != host:
                        continue
                    rows.append({
                        "port": h.get("port"),
                        "enabled": bool(h.get("enabled")),
                        "weight": float(h.get("weight", 0) or 0),
                        "cluster": h.get("clusterName", "DEFAULT"),
                        "ephemeral": h.get("ephemeral"),
                    })
                if rows:
                    snapshot[f"{svc}|{grp}"] = rows
        return snapshot

    def restore_traffic(self, host, snapshot):
        """按快照恢复；原本 enabled=false 的实例不强制开启。"""
        restored = set()
        for key, rows in (snapshot or {}).items():
            svc, _, grp = key.partition("|")
            for r in rows:
                if not r.get("enabled"):
                    continue
                self.nacos.set_instance(
                    svc, host, r.get("port"), enabled=True,
                    weight=float(r.get("weight", 1) or 1),
                    cluster=r.get("cluster") or "DEFAULT",
                    group=grp or None, ephemeral=r.get("ephemeral"))
                restored.add((svc, grp))
        for svc, grp in sorted(restored):
            self.nacos.wait_for_healthy(svc, host, timeout=120,
                                        group=grp or None)
```

- [ ] **Step 4: 实现监控配置状态纯函数**

在 `deploy_strategies/tomcat_instance.py` 末尾追加：

```python
_CONFIG_STATUS_ORDER = ("owner_mismatch", "unmanaged", "maintenance", "managed")


def config_status(manifest: dict, instance_id: str, project_id, env_name: str,
                  service: str) -> str:
    if not manifest:
        return "unmanaged"
    if (manifest.get("instance_id") != instance_id
            or manifest.get("project_id") != project_id
            or manifest.get("env") != env_name
            or manifest.get("service") != service):
        return "owner_mismatch"
    if (manifest.get("maintenance") or {}).get("active"):
        return "maintenance"
    return "managed"


def aggregate_config_status(rows: list) -> str:
    present = {(row or {}).get("config_status") or "unmanaged" for row in rows}
    for value in _CONFIG_STATUS_ORDER:
        if value in present:
            return value
    return "unmanaged"


def aggregate_operation_state(rows: list) -> str:
    if any((row or {}).get("operation_state") == "in_progress" for row in rows):
        return "in_progress"
    return "idle"
```

- [ ] **Step 5: 接重启执行器**

`webapp/task_manager.py` 在 `_run_restart` 之前增加：

```python
def _war_lifecycle(rec, env_obj, cfg, unit):
    from deploy_strategies.tomcat_instance import resolve_instance_id
    from deploy_strategies.tomcat_lifecycle import WarTomcatLifecycle
    from deploy_strategies.tomcat_restart import RestartContext
    service = rec.get("service_name") or rec["name"]
    ctx = RestartContext(env_obj, rec)
    return WarTomcatLifecycle(
        ctx, cfg, service, unit,
        instance_id=lambda server: resolve_instance_id(
            server, cfg["app_home"]))


def _run_war_restart(f, rec, env_obj, servers, handle,
                     release_maintenance=False) -> str:
    """WAR 严格重启：健康超时/停止失败/锁未知都计入任务失败。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from deploy import RemoteServer
    from deploy_strategies.tomcat_config import derive_war_unit, parse_war_config

    from . import monitor as monitor_mod

    cfg = parse_war_config(rec.get("runtime_config"))
    unit = derive_war_unit(cfg, rec.get("image_name") or rec["name"])
    f.write(f"[platform] 严格重启 WAR 实例「{rec['name']}」（单元 {unit}），"
            f"环境「{env_obj['name']}」，共 {len(servers)} 台服务器"
            f"{'，并结束维护' if release_maintenance else ''}\n")
    f.flush()
    env_ssh = env_obj.get("ssh") or {}
    results = []

    def restart_one(s):
        host = s["host"]
        if handle.cancel.is_set():
            return host, "cancelled", "任务已终止，跳过"
        server = None
        try:
            ssh_cfg = monitor_mod._merge_server_ssh(env_ssh, s)
            server = RemoteServer(host, ssh_cfg)
            lifecycle = _war_lifecycle(rec, env_obj, cfg, unit)
            res = lifecycle.restart(server, dict(s, ip=host),
                                    release_maintenance=release_maintenance)
            if res.ok:
                return host, "ok", (f"重启成功（应用健康；流量 {res.traffic_status}；"
                                    f"锁 {res.lock_state}）")
            return host, "failed", (
                f"重启失败[{res.phase}] 操作={res.operation_status} "
                f"进程={res.process_state} 应用={res.app_health} "
                f"流量={res.traffic_status} 锁={res.lock_state} "
                f"{res.error}").strip()
        except Exception as e:
            return host, "failed", f"SSH/执行失败: {e}"
        finally:
            if server is not None:
                server.close()

    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        futures = [pool.submit(restart_one, s) for s in servers]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append(("?", "failed", f"执行异常: {e}"))
    order = {s["host"]: i for i, s in enumerate(servers)}
    results.sort(key=lambda r: order.get(r[0], len(servers)))
    for host, status, msg in results:
        f.write(f"[{host}] {msg}\n")
        f.flush()
    ok_count = sum(1 for _, st, _ in results if st == "ok")
    fail_count = sum(1 for _, st, _ in results if st == "failed")
    skip_count = sum(1 for _, st, _ in results if st == "cancelled")
    if handle.cancel.is_set():
        f.write("[platform] 任务已被终止\n")
        return "failed"
    if fail_count:
        f.write(f"[platform] 汇总：{ok_count} 台成功，{fail_count} 台失败，"
                f"{skip_count} 台跳过 → 任务失败\n")
        return "failed"
    if ok_count == 0:
        f.write("[platform] 汇总：无服务器重启成功 → 任务失败\n")
        return "failed"
    f.write(f"[platform] 汇总：{ok_count} 台成功，{skip_count} 台跳过 → 任务成功\n")
    return "success"
```

在 `_run_restart` 的 `if not servers:` 校验之后、`target_desc` 之前插入：

```python
        if deploy_mode == "war_tomcat":
            extra = task.get("extra") or []
            if isinstance(extra, str):
                extra = [extra]
            return _run_war_restart(f, rec, env_obj, servers, handle,
                                    "--release-maintenance" in extra)
```

重启请求新增布尔字段 `release_maintenance`（默认 false）；`deploy_routes.py`
在 `type_ == "restart"` 分支校验其为布尔后，为 true 时向 `extra` 追加
`--release-maintenance`。WAR 之外的模式忽略该标记。前端“结束维护并重启”
必须显式勾选并二次确认，不得默认开启。

- [ ] **Step 6: 接监控与日志采集**

`webapp/monitor.py` 的四处改动：

1. 模块级新增（放在 `_probe_health_via_ssh` 之后）：

```python
def _probe_war_evidence(host: str, port: int, ssh_cfg: dict, instances: list) -> dict:
    """读取 WAR 归属清单与 owner；只读，不触发任何写操作。"""
    from deploy_strategies import tomcat_instance
    from deploy_strategies.remote_operation import inspect

    class _ExecAdapter:
        def exec(self, cmd, timeout=60, stdin_data=None):
            return _ssh_exec(host, port, ssh_cfg, cmd, timeout)

    import shlex

    adapter = _ExecAdapter()
    result = {}
    for info in instances or []:
        app_home = info["app_home"]
        try:
            manifest = tomcat_instance.read_manifest(adapter, app_home)
            iid = (manifest or {}).get("instance_id") or ""
            state = inspect(adapter, iid) if iid else {}
        except Exception:
            manifest, iid, state = {}, "", {}
        # CURRENT 是版本权威，.deploy_version 只是兼容镜像；两者不一致即漂移
        try:
            out = adapter.exec(
                f"readlink -f {shlex.quote(app_home + '/CURRENT')} 2>/dev/null;"
                f" printf '\\n---\\n';"
                f" cat {shlex.quote(app_home + '/.deploy_version')} 2>/dev/null")
            link_raw, _, ver_raw = out.partition("---")
            link = link_raw.strip().rstrip("/").rsplit("/", 1)[-1]
            depver = ver_raw.strip()
        except Exception:
            link, depver = "", ""
        result[info["service"]] = {
            "instance_id": iid,
            "manifest": manifest,
            "owner": state.get("owner") or {},
            "phase": state.get("phase") or "",
            "last_result": state.get("last_result") or {},
            "current_version": link,
            "deploy_version": depver,
            "version_drift": bool(
                (depver and not link)
                or (link and depver and link != depver)),
        }
    return result


def _probe_war_health(host: str, ssh_port: int, ssh_cfg: dict,
                      checks: list) -> tuple:
    """WAR 单次有界探活：SSH direct-tcpip，不依赖目标机 curl。"""
    from deploy_strategies.tomcat_health import probe_http

    class _PooledClient:
        def client(self):
            return _ssh_pool.get_client(host, ssh_port, ssh_cfg, 10)

    out = []
    for name, port, path, expected, contains in checks:
        r = probe_http(_PooledClient(), port, path, expected_status=expected,
                       body_contains=contains, timeout=4)
        out.append((name, {"ok": r["ok"], "ms": r["ms"],
                           "method": "http_strict"}))
    return host, out
```

2. `_probe_envs` 组装进程信息时把 `deploy_mode` 一并传入派单口径，带上 WAR 标记；`host_jobs` 增加 `war_instances`：

```python
            for svc in services:
                deployment = svc.get("deploy_mode") or "container"
                if deployment not in process_modes():
                    continue
                cfg = parse_runtime_config(svc.get("runtime_config"), deployment)
                unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"],
                                        deploy_mode=deployment)
                info = {"unit": unit, "app_home": cfg["app_home"],
                        "service": svc.get("service_name") or svc["name"],
                        "war": deployment == "war_tomcat"}
                process_by_project.setdefault(proj["id"], []).append(info)
```

> 注意：`derive_unit_name` 不传 `deploy_mode` 时 WAR 会退回 `image_name`，与真实
> 单元 `smartops-{image}-war` 不一致，监控会长期报 `not_deployed`；三处调用
> （`_probe_envs`、`_merge_process_services`、重启处理）必须统一传 `deployment`。

```python
                    job = {"host": s["host"], "port": key[1],
                           "ssh_cfg": _merge_server_ssh(env_ssh, s),
                           "service_names": set(), "jar_units": {},
                           "war_instances": []}
```

```python
                for info in process_by_project.get(ctx["proj"]["id"], []):
                    job["jar_units"].setdefault(
                        info["unit"], {"app_home": info["app_home"]})
                    if info["war"]:
                        # 实例身份在主机上的归属文件里，探测时不按平台字段推导
                        job["war_instances"].append({
                            "service": info["service"],
                            "app_home": info["app_home"]})
```

3. `_probe_hosts` 在 `if r.get("ssh_ok"):` 分支内追加：

```python
                    if job.get("war_instances"):
                        try:
                            r["war_evidence"] = _probe_war_evidence(
                                job["host"], job["port"], job["ssh_cfg"],
                                job["war_instances"])
                        except Exception:
                            r["war_evidence"] = {}
```

4. `_merge_process_services`：函数开头补 `from deploy_strategies import tomcat_instance`；
传入 `deploy_mode`，WAR 行附加归属/维护/操作状态：

```python
            deployment = svc.get("deploy_mode") or "container"
            war = deployment == "war_tomcat"
            cfg = parse_runtime_config(svc.get("runtime_config"), deployment)
            unit = derive_unit_name(cfg, svc.get("image_name"), svc["name"],
                                    deploy_mode=deployment)
```

每个 `rows.append` 的行对象统一带默认字段（WAR 专属字段只在 `war` 为真时追加，
旧进程族的行结构与字段保持不变）：

```python
                base = {"host": s["host"], "unit": unit, "version": "",
                        "pid": 0, "uptime_hours": None, "restarts": 0,
                        "cpu": None, "rss_mb": None}
                if war:
                    ev = ((r.get("war_evidence") or {}).get(
                        svc.get("service_name") or svc["name"]) or {})
                    manifest = ev.get("manifest") or {}
                    iid = ev.get("instance_id") or ""
                    last = ev.get("last_result") or {}
                    base.update({
                        "instance_id": iid,
                        "config_status": tomcat_instance.config_status(
                            manifest, iid, env.get("project_id"), env["name"],
                            svc.get("service_name") or svc["name"]),
                        "operation_state": ("in_progress" if ev.get("owner")
                                            else "idle"),
                        "operation_phase": ((ev.get("phase") or "")
                                            if ev.get("owner") else ""),
                        "maintenance": bool(
                            (manifest.get("maintenance") or {}).get("active")),
                        "app_health": "unknown",
                        # 进程视角在健康/归属判定前固化，展示层再据此区分
                        # “进程在跑但应用不健康”与“进程本身停了”
                        "process_status": "unknown",
                        # 有历史结果但缺字段才记 unknown；从未操作过是 not_needed
                        "recovery_status": ((last.get("recovery_status")
                                             or "unknown") if last
                                            else "not_needed"),
                        "version_drift": bool(ev.get("version_drift")),
                    })
```

`probe_failed`/`not_deployed` 行使用 `{**base, "status": ...}`；`build_view` 成功后
`base.update(view)` 再 `rows.append(base)`。所有 WAR 行（含 `stopped`/
`not_deployed`/`probe_failed`）在 append 前都补
`base["process_status"] = base.get("status") or "unknown"`，保证 UI 的「进程」
列不会在非 online 主机上显示未知；健康循环只对 online 行重算并随后降级
`status`，不改写 `process_status`。WAR 行的版本以 CURRENT 为目标：
`build_view` 合并后，若 `ev.get("current_version")` 非空则覆盖
`base["version"] = ev["current_version"]`（`.deploy_version` 只作兼容镜像，
不作为 WAR 权威）；`version_drift` 为真时只标记展示，不回写数据库、不自行修复。
覆盖必须发生在既有 `version = view.get("version")` 写穿
`record_host_state_live` 之前：在 `view` 合并时就把
`if war and ev.get("current_version"): view["version"] = ev["current_version"]`
放进去，保证 `service_host_state.current_version` 也是现场观察值，而不是落后的
`.deploy_version`。`jar_probe.aggregate_hosts(rows)` 随后按行版本聚合，无需另改。
服务聚合追加：

```python
            if war:
                st.update({
                    "config_status": tomcat_instance.aggregate_config_status(rows),
                    "operation_state": tomcat_instance.aggregate_operation_state(rows),
                    "operation_phase": next(
                        (x.get("operation_phase") for x in rows
                         if x.get("operation_phase")), ""),
                    "instance_home": f"{cfg['app_home']}/instance",
                })
```

5. `_probe_env` 健康检查：在 `host_checks` 组装循环（`for svc in online:`，
   函数体首行是 `hp = svc.get("health_port") or 0`）的第一行跳过 WAR，避免旧
   curl `/dev/tcp` 路径先给 WAR 一个假健康结论：

```python
            for svc in online:
                if (svc.get("deploy_mode") or "") == "war_tomcat":
                    continue
                hp = svc.get("health_port") or 0
```

在现有健康检查块之后追加 WAR 直连探活：

```python
        if online:
            from deploy_strategies.process_config import parse_runtime_config
            from deploy_strategies.tomcat_config import health_request_path
            war_checks = {}
            for svc in online:
                if (svc.get("deploy_mode") or "") != "war_tomcat":
                    continue
                cfg = parse_runtime_config(svc.get("runtime_config"),
                                           "war_tomcat")
                path = health_request_path(cfg)
                for host in health_hosts.get(svc["name"], []):
                    war_checks.setdefault(host, []).append(
                        (svc["name"], cfg["port"], path,
                         cfg["readiness"]["expected_status"],
                         cfg["readiness"]["body_contains"]))
            env_ssh = env.get("ssh") or {}
            server_by_host = {s["host"]: s for s in servers}
            war_health = {}   # {svc_name: {host: ok}}
            for svc in online:
                if (svc.get("deploy_mode") or "") == "war_tomcat":
                    # 即使本轮探活异常也要有键，避免 app_health 缺字段
                    war_health.setdefault(svc["name"], {})
                    st = result["services"].get(svc["name"])
                    if st is not None:
                        # 在任何降级前固化进程视角，之后的 unhealthy/unmanaged
                        # 只改展示聚合，不覆盖“进程是否在跑”
                        st["process_status"] = st.get("status") or "unknown"
            if war_checks:
                def _run_war_checks(host, checks):
                    s = server_by_host.get(host)
                    if not s:
                        return host, []
                    return _probe_war_health(
                        host, int(s.get("ssh_port") or 22),
                        _merge_server_ssh(env_ssh, s), checks)

                with ThreadPoolExecutor(max_workers=min(8, len(war_checks))) as pool:
                    futs = [pool.submit(_run_war_checks, h, cs)
                            for h, cs in war_checks.items()]
                    for fut in futs:
                        try:
                            host, hr_list = fut.result(timeout=30)
                        except Exception:
                            continue
                        for svc_name, hr in hr_list:
                            try:
                                dbm.save_response_time(
                                    self._db_path, env["id"], svc_name, host,
                                    hr["ms"] if hr["ms"] > 0 else -1, hr["ok"])
                            except Exception:
                                pass
                            war_health.setdefault(svc_name, {})[host] = hr["ok"]
                            if hr["ok"] and svc_name not in result["health_checks"]:
                                result["health_checks"][svc_name] = {
                                    **hr, "host": host}
            for svc_name, per_host in war_health.items():
                st = result["services"].get(svc_name)
                if not st:
                    continue
                for row in st.get("hosts") or []:
                    ok = per_host.get(row.get("host"))
                    row["process_status"] = row.get("status") or "unknown"
                    row["app_health"] = ("unknown" if ok is None
                                         else ("ok" if ok else "failed"))
                    if row.get("status") == "online" and ok is False:
                        row["status"] = "unhealthy"
                st["app_health"] = ("unknown" if not per_host
                                    else ("ok" if all(per_host.values())
                                          else "failed"))
                st["recovery_status"] = next(
                    (x.get("recovery_status")
                     for x in st.get("hosts") or []
                     if x.get("recovery_status") not in (None, "not_needed")),
                    "not_needed")
                # 整体 online 只在进程运行、应用健康且实例受管时成立
                if st.get("status") == "online" and st["app_health"] == "failed":
                    st["status"] = "unhealthy"
                if (st.get("status") == "online"
                        and st.get("config_status") in ("owner_mismatch",
                                                        "unmanaged")):
                    st["status"] = "unmanaged"
```

`_merge_process_services` 的行默认字段只在 WAR 分支补 `"app_health": "unknown"`，
旧进程族行保持原字段集合；`unhealthy`/`unmanaged` 的前端标签在 Task 11 补齐。

`webapp/log_collector.py` 的 `derive_unit_name` 调用追加
`deploy_mode=r["deploy_mode"]`；`webapp/routes/config_routes.py` 与
`deploy_strategies/process_service.py` 的同名调用分别追加
`deploy_mode=mode` / `deploy_mode=self.mode`。

- [ ] **Step 7: 运行通过**

Run:
`python -m pytest tests/test_war_restart.py tests/test_war_observability.py tests/test_restart_task.py tests/test_monitor_jar.py tests/test_log_collector.py tests/test_jar_service.py -v`
Expected: PASS；旧进程族重启/监控/日志断言保持不变。

- [ ] **Step 8: 检查范围，不提交**

Run: `git diff --stat`
Expected: 仅本任务列出的文件与测试；未提交。

---

### Task 11: WAR 管理路由与界面（预览、接入、受控更新、人工恢复）

**Files:**
- Create: `webapp/routes/tomcat_routes.py`
- Modify: `webapp/__init__.py`
- Modify: `webapp/routes/config_routes.py`
- Modify: `webapp/routes/deploy_routes.py`
- Modify: `webapp/static/js/pages/config/config-service-form.js`
- Modify: `webapp/static/js/jar_hosts.js`
- Modify: `webapp/static/js/pages/devices.js`
- Modify: `webapp/static/js/data.js`
- Test: `tests/test_tomcat_routes.py`
- Modify: `tests/js/jar_hosts_test.js`
- Test: `tests/js/war_config_form_test.js`
- Test: `tests/js/war_hosts_test.js`

**Interfaces:**
- Produces: `GET /api/projects/<pid>/services/<sid>/tomcat/preview?env=`（admin），
  逐主机返回 `host, instance_id, unit, digest, unit_content, files, manifest,
  deletes, templates, onboarded, config_status`；部分失败返回 207 + `failed`。
- Produces: `POST .../tomcat/onboard`，请求
  `{env, confirm:true, hosts:[{host, instance_id, digest}]}`（admin）；重算预览、
  校验摘要，按主机返回 207；只写实例文件与 unit，不启动。
- Produces: `POST .../tomcat/config`，请求
  `{env, action:"update", confirm:true, hosts:[...]}`（admin）；单 owner 内执行
  漂移校验 → 流量快照/摘流 → 维护标志 → 配置事务，完成后释放为
  `status=maintenance`，**不启动**；返回 `started:false`。
- Produces: `GET .../tomcat/recovery?env=`（admin），返回脱敏 owner（去掉
  `token`）、`owner_digest`、`last_result`、`config_status`、`maintenance`。
- Produces: `POST .../tomcat/recovery/revoke`，请求
  `{env, host, instance_id, confirm:true, confirm_digest}`（admin）；摘要不符或
  锁已不存在返回 409，成功记审计。
- `service_hosts` 行追加 `config_status/operation_state/instance_id/maintenance/
  app_health/process_status/recovery_status/lock_state`（存在才透传），
  `jar_hosts.js` 按字段自适应列。
- 重启请求新增布尔 `release_maintenance`，仅 `war_tomcat` 重启接受，并转成
  `extra=["--release-maintenance"]`。
- 前端 `api()` 对 207 会抛错（`err.data` 携带响应体），所有 WAR 操作必须从
  `e.data.failed` 读取逐主机失败，不能把 207 当整体成功。

- [ ] **Step 1: 写失败测试**

`tests/test_tomcat_routes.py`：

```python
"""WAR 管理路由：预览/接入/受控更新/恢复的权限、摘要与 207 语义"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.mkdtemp(prefix="war_routes_")

from webapp import create_app
from webapp import db as dbm

RUNTIME = {
    "app_home": "/opt/apps/orders", "tomcat_home": "/opt/tomcat",
    "java_home": "/usr/lib/jvm/java-17", "run_user": "appuser",
    "unit_name": "orders-war", "port": 18080, "context_path": "/orders",
    "readiness": {"path": "/health", "timeout": 30, "interval": 2,
                  "expected_status": 200, "body_contains": ""},
}
PLAN = {
    "digest": "a" * 64, "unit": "orders-war", "unit_content": "[Service]\n",
    "files": {"instance/conf/server.xml": "<Server/>"},
    "manifest": {"app_home": "/opt/apps/orders", "unit": "orders-war"},
    "deletes": ["instance/webapps/*"], "templates": ["server.xml"],
}


class TomcatRoutesTest(unittest.TestCase):
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
        r = cls.c.post("/api/projects", json={"name": "war-test", "code": "wartest"},
                       headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.pid = r.get_json()["id"]
        r = cls.c.post(f"/api/projects/{cls.pid}/services", json={
            "name": "orders", "build_type": "maven", "deploy_mode": "war_tomcat",
            "image_name": "", "service_name": "", "runtime_config": dict(RUNTIME)},
            headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.sid = r.get_json()["id"]
        r = cls.c.post(f"/api/projects/{cls.pid}/environments",
                       json={"name": "prod"}, headers=cls.h)
        assert r.status_code == 201, r.get_data(as_text=True)
        cls.env_id = r.get_json()["id"]
        for host in ("10.0.0.1", "10.0.0.2"):
            r = cls.c.post("/api/servers", json={
                "host": host, "ssh_port": 22, "ssh_user": "root"}, headers=cls.h)
            assert r.status_code == 201, r.get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        assert r.status_code == 200, r.get_data(as_text=True)
        return r.get_json()["token"]

    def _url(self, tail):
        return f"/api/projects/{self.pid}/services/{self.sid}/tomcat/{tail}"

    def _hosts(self, digest="a" * 64, iid="i1"):
        return [{"host": h, "instance_id": iid, "digest": digest}
                for h in ("10.0.0.1", "10.0.0.2")]

    def test_preview_reuses_manifest_id_and_reports_partial(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "existing"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="managed"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           side_effect=[dict(PLAN), RuntimeError("模板读取失败")]):
            r = self.c.get(self._url("preview?env=prod"), headers=self.h)
        self.assertEqual(r.status_code, 207, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data["hosts"][0]["instance_id"], "existing")
        self.assertEqual(len(data["failed"]), 1)
        self.assertNotIn("token", r.get_data(as_text=True))

    def test_onboard_requires_confirm_and_exact_hosts(self):
        r = self.c.post(self._url("onboard"),
                        json={"env": "prod", "hosts": self._hosts()},
                        headers=self.h)
        self.assertEqual(r.status_code, 400)
        r = self.c.post(self._url("onboard"),
                        json={"env": "prod", "confirm": True,
                              "hosts": self._hosts()[:1]}, headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_onboard_stale_digest_does_not_write(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN, digest="b" * 64)), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.onboard") as ob:
            r = self.c.post(self._url("onboard"), json={
                "env": "prod", "confirm": True, "hosts": self._hosts()},
                headers=self.h)
        self.assertEqual(r.status_code, 207, r.get_data(as_text=True))
        ob.assert_not_called()

    def test_onboard_writes_audit_and_never_starts(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN)), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.onboard") as ob:
            r = self.c.post(self._url("onboard"), json={
                "env": "prod", "confirm": True, "hosts": self._hosts()},
                headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(ob.call_count, 2)
        logs = dbm.list_audit_logs(self.app.config["DB_PATH"], limit=20)
        self.assertTrue(any(x["action"] == "war_tomcat_onboard" for x in logs))

    def test_config_update_is_one_owner_and_never_starts(self):
        class Ctx:
            def capture_traffic(self, host):
                return {"orders|G1": [{"port": 18080, "enabled": True,
                                       "weight": 5, "cluster": "D",
                                       "ephemeral": True}]}
            def nacos_offline(self, host):
                return None

        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "i1"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="managed"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.verify_instance",
                           return_value=[]), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.preview",
                           return_value=dict(PLAN)), \
                mock.patch("webapp.routes.tomcat_routes._traffic_context",
                           return_value=Ctx()), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.acquire") as acq, \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.release") as rel, \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.maintenance_stop",
                           return_value={"active": True}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.apply_config") as ap:
            r = self.c.post(self._url("config"), json={
                "env": "prod", "action": "update", "confirm": True,
                "hosts": self._hosts()}, headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(acq.call_count, 2)
        self.assertEqual(ap.call_count, 2)
        self.assertFalse(r.get_json()["hosts"][0]["started"])
        self.assertEqual(rel.call_args.args[3]["status"], "maintenance")
        self.assertEqual([h["status"] for h in r.get_json()["hosts"]],
                         ["maintenance", "maintenance"])
        joined = " ".join(str(x) for x in ap.call_args_list)
        self.assertNotIn("systemctl start", joined)

    def test_config_requires_update_and_confirm(self):
        r = self.c.post(self._url("config"), json={
            "env": "prod", "confirm": True, "hosts": self._hosts()},
            headers=self.h)
        self.assertEqual(r.status_code, 400)

    def test_recovery_sanitizes_owner_and_returns_digest(self):
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "i1"}), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.config_status",
                           return_value="maintenance"), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.inspect",
                           return_value={"owner": {"token": "secret",
                                                   "operation": "config_update"},
                                         "last_result": {"status": "failed"}}), \
                mock.patch("webapp.routes.tomcat_routes._owner_digest",
                           return_value="b" * 64):
            r = self.c.get(self._url("recovery?env=prod"), headers=self.h)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_data(as_text=True)
        self.assertNotIn("secret", body)
        self.assertEqual(r.get_json()["hosts"][0]["owner_digest"], "b" * 64)
        self.assertEqual(r.get_json()["hosts"][0]["config_status"], "maintenance")

    def test_revoke_rejects_bad_digest_and_owner_mismatch(self):
        r = self.c.post(self._url("recovery/revoke"), json={
            "env": "prod", "host": "10.0.0.1", "instance_id": "i1",
            "confirm": True, "confirm_digest": "bad"}, headers=self.h)
        self.assertEqual(r.status_code, 400)
        with mock.patch("webapp.routes.tomcat_routes.RemoteServer"), \
                mock.patch("webapp.routes.tomcat_routes.tomcat_instance.read_manifest",
                           return_value={"instance_id": "other"}), \
                mock.patch("webapp.routes.tomcat_routes.remote_operation.revoke") as rv:
            r = self.c.post(self._url("recovery/revoke"), json={
                "env": "prod", "host": "10.0.0.1", "instance_id": "i1",
                "confirm": True, "confirm_digest": "b" * 64}, headers=self.h)
        self.assertEqual(r.status_code, 409)
        rv.assert_not_called()
```

`tests/js/war_config_form_test.js`：复用
`config_service_form_visibility_test.js` 的 vm harness，只改三处——`api` 对两次
调用都返回 `[]`；`openWith('war_tomcat', {runtime_config: {...}})`；断言：

```javascript
check('WAR：显示 Tomcat/上下文/健康字段',
      html.includes('id="wTomcatHome"') && html.includes('id="wContext"')
      && html.includes('id="wHealthPath"'));
check('WAR：不显示 Docker 与 jar 专属字段',
      !html.includes('id="sDockerCtx"') && !html.includes('id="jArtifactPattern"'));
check('WAR：镜像短名不必填', requiredOf('镜像短名') === false);
check('WAR：构建类型锁定 Maven',
      /<option value="maven" selected/.test(html) && html.includes('id="sBuildType"'));
check('WAR：保留构建分区', html.includes('id="sec-build"'));
check('WAR：提供预览/接入/更新/恢复按钮',
      ['wPreviewBtn', 'wOnboardBtn', 'wConfigBtn', 'wRecoveryBtn']
        .every(id => html.includes('id="' + id + '"')));

// 从容器切到 WAR：字段集切换且构建类型回填 Maven
html = await openWith('container', {build_type: 'frontend'});
el('sDeployMode').onchange({target: {value: 'war_tomcat'}});
html = el('dwBody').innerHTML;
check('切换 WAR：重渲染 Tomcat 字段', html.includes('id="wTomcatHome"'));
check('切换 WAR：构建类型回填 Maven', /<option value="maven" selected/.test(html));
check('切换 WAR：Docker 字段消失', !html.includes('id="sDockerCtx"'));
```

`tests/js/war_hosts_test.js`：

```javascript
'use strict';
const { jarHostTableHTML } = require('../../webapp/static/js/jar_hosts.js');

const war = jarHostTableHTML([
  {host: '10.0.0.1', status: 'online', version: '1.0.0',
   config_status: 'maintenance', operation_state: 'in_progress',
   maintenance: true, instance_id: 'i1', live: true},
]);
if (!war.includes('<th>配置</th>') || !war.includes('维护中')
    || !war.includes('操作中')) {
  console.error('FAIL war host columns'); process.exit(1);
}
const bad = jarHostTableHTML([
  {host: '10.0.0.2', status: 'unhealthy', config_status: 'managed',
   operation_state: 'idle', maintenance: false, live: true},
]);
if (!bad.includes('不健康')) {
  console.error('FAIL unhealthy status label'); process.exit(1);
}
const unmanaged = jarHostTableHTML([
  {host: '10.0.0.3', status: 'unmanaged', config_status: 'unmanaged',
   operation_state: 'idle', maintenance: false, live: true},
]);
if (!unmanaged.includes('未托管')) {
  console.error('FAIL unmanaged status label'); process.exit(1);
}
const jar = jarHostTableHTML([{host: 'h', status: 'online', live: true}]);
if (jar.includes('<th>配置</th>')) {
  console.error('FAIL jar host columns must stay unchanged'); process.exit(1);
}
console.log('PASS war_hosts_test');
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_tomcat_routes.py -v`
Expected: FAIL，`404`（蓝图未注册）。
Run: `node tests/js/war_config_form_test.js`
Expected: FAIL（无 WAR 字段）。
Run: `node tests/js/war_hosts_test.js`
Expected: FAIL（无配置列）。

- [ ] **Step 3: 实现路由**

`webapp/routes/tomcat_routes.py`：

```python
"""WAR/Tomcat 实例托管路由：预览、接入、受控配置更新、人工恢复。"""
import re
import secrets
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request

from deploy import RemoteServer, merge_server_ssh

from deploy_strategies import remote_operation, tomcat_instance
from deploy_strategies.remote_operation import LockBusy, StopFailed
from deploy_strategies.tomcat_config import (TEMPLATE_VERSION,
                                             derive_war_unit,
                                             parse_war_config,
                                             validate_war_config)

from .. import db as dbm
from ..auth import audit, require_auth, require_role

tomcat_bp = Blueprint("tomcat", __name__)
_HEX64 = re.compile(r"[0-9a-f]{64}$")


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _service(pid, sid):
    rec = dbm.get_record(current_app.config["DB_PATH"], "services", sid)
    if not rec or rec["project_id"] != pid:
        return None, _err("服务不存在", 404)
    if (rec.get("deploy_mode") or "") != "war_tomcat":
        return None, _err("该服务不是 WAR/Tomcat 发布方式")
    return rec, None


def _env_servers(pid, env_name):
    db_path = current_app.config["DB_PATH"]
    env = (dbm.get_env_by_name(db_path, pid, env_name) if env_name
           else dbm.get_default_env(db_path, pid))
    if not env:
        return None, None, _err("目标环境不存在", 404)
    servers = dbm.list_env_servers(db_path, env["id"])
    if not servers:
        return None, None, _err("目标环境未绑定服务器")
    return env, servers, None


def _model(rec):
    errors = validate_war_config(rec.get("runtime_config"))
    if errors:
        raise ValueError("；".join(errors))
    cfg = parse_war_config(rec.get("runtime_config"))
    # manifest/证据的服务标识统一取 service_name，单元名沿既有 image_name 派生规则
    service = rec.get("service_name") or rec["name"]
    unit = derive_war_unit(cfg, rec.get("image_name") or rec["name"])
    return cfg, service, unit


def _traffic_context(env, rec):
    from deploy_strategies.tomcat_restart import RestartContext
    return RestartContext(env, rec)


def _owner_digest(server, instance_id):
    from deploy_strategies.remote_operation import lock_paths, shq
    path = lock_paths(instance_id)["owner"]
    out = server.exec(
        f"test -f {shq(path)} && sha256sum {shq(path)} | "
        f"awk '{{print $1}}' || true", timeout=15).strip()
    return out if _HEX64.fullmatch(out) else ""


def _host_requests(body, servers):
    rows = body.get("hosts")
    if not isinstance(rows, list):
        return None, "hosts 必须为列表"
    by_host = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("host"), str):
            return None, "hosts 每项必须包含 host"
        by_host[row["host"]] = row
    if set(by_host) != {s["host"] for s in servers}:
        return None, "hosts 必须与环境绑定服务器完全一致"
    return by_host, None


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/preview",
                 methods=["GET"])
@require_auth
@require_role("admin")
def preview_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    env, servers, err = _env_servers(pid, request.args.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    done, failed = [], []
    for srv in servers:
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            existing = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (existing or {}).get("instance_id") or uuid4().hex
            status = (tomcat_instance.config_status(
                existing, iid, pid, env["name"], service)
                if existing else "unmanaged")
            if status == "owner_mismatch":
                failed.append({"host": srv["host"],
                               "error": "主机已有其它归属的 Tomcat 实例，拒绝接管"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            done.append({
                "host": srv["host"], "instance_id": iid, "unit": unit,
                "digest": plan["digest"], "unit_content": plan["unit_content"],
                "files": plan["files"], "manifest": plan["manifest"],
                "deletes": plan["deletes"], "templates": plan["templates"],
                "onboarded": bool(existing), "config_status": status,
            })
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/onboard",
                 methods=["POST"])
@require_auth
@require_role("admin")
def onboard_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认接入")
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    by_host, msg = _host_requests(body, servers)
    if msg:
        return _err(msg)
    done, failed = [], []
    for srv in servers:
        req = by_host[srv["host"]]
        iid = str(req.get("instance_id") or "")
        digest = str(req.get("digest") or "")
        if not iid or not _HEX64.fullmatch(digest):
            failed.append({"host": srv["host"],
                           "error": "instance_id/digest 缺失或格式错误"})
            continue
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            if tomcat_instance.read_manifest(server, cfg["app_home"]):
                failed.append({"host": srv["host"],
                               "error": "实例已接入；配置变更请走配置更新"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            if plan["digest"] != digest:
                failed.append({"host": srv["host"],
                               "error": "预览摘要已变化，请重新预览"})
                continue
            tomcat_instance.onboard(server, plan, pid, env["name"], iid, digest)
            done.append({"host": srv["host"], "instance_id": iid,
                         "unit": unit, "digest": digest})
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    audit("war_tomcat_onboard", "service", sid,
          {"env": env["name"], "ok": [h["host"] for h in done],
           "failed": failed})
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/config",
                 methods=["POST"])
@require_auth
@require_role("admin")
def update_config(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认更新配置")
    if body.get("action") != "update":
        return _err('action 必须为 "update"')
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    by_host, msg = _host_requests(body, servers)
    if msg:
        return _err(msg)
    done, failed = [], []
    for srv in servers:
        host = srv["host"]
        req = by_host[host]
        server = RemoteServer(host, merge_server_ssh(env.get("ssh") or {}, srv))
        acquired = keep_owner = False
        token = ""
        try:
            manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (manifest or {}).get("instance_id") or ""
            if not iid:
                failed.append({"host": host, "error": "实例未接入"})
                continue
            if iid != str(req.get("instance_id") or ""):
                failed.append({"host": host, "error": "实例身份不一致"})
                continue
            status = tomcat_instance.config_status(
                manifest, iid, pid, env["name"], service)
            if status == "maintenance":
                failed.append({"host": host,
                               "error": "实例处于维护状态，请先结束维护并重启"})
                continue
            if status != "managed":
                failed.append({"host": host,
                               "error": f"实例配置状态为 {status}，拒绝更新"})
                continue
            plan = tomcat_instance.preview(
                server, cfg, service, pid, env["name"], unit, iid)
            if plan["digest"] != str(req.get("digest") or ""):
                failed.append({"host": host, "error": "预览摘要已变化，请重新预览"})
                continue
            token = secrets.token_hex(32)
            remote_operation.acquire(server, iid, token,
                                     "config_update", "preflight")
            acquired = True
            drift = tomcat_instance.verify_instance(
                server, cfg["app_home"], unit, manifest)
            if drift:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": "受管文件漂移"})
                acquired = False
                failed.append({"host": host,
                               "error": "受管文件或 unit 已漂移: " + ", ".join(drift)})
                continue
            ctx = _traffic_context(env, rec)
            snapshot = ctx.capture_traffic(host)
            try:
                ctx.nacos_offline(host)
            except Exception as e:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": f"摘流失败: {e}"})
                acquired = False
                failed.append({"host": host, "error": f"摘流失败，未停止实例: {e}"})
                continue
            try:
                tomcat_instance.maintenance_stop(
                    server, cfg["app_home"], unit, iid, token, snapshot)
            except StopFailed as e:
                keep_owner = True
                failed.append({"host": host,
                               "error": f"停止未确认，锁保留待人工核验: {e}"})
                continue
            except Exception as e:
                keep_owner = True
                failed.append({"host": host,
                               "error": f"维护状态写入/停止异常，锁保留: {e}"})
                continue
            try:
                tomcat_instance.apply_config(
                    server, cfg["app_home"], unit, iid, token,
                    plan["files"], plan["unit_content"], plan["digest"],
                    manifest_changes={
                        "unit": unit, "port": cfg["port"],
                        "context_path": cfg["context_path"],
                        "run_user": cfg["run_user"],
                        "template_version": TEMPLATE_VERSION,
                        "managed_files": sorted(plan["files"]),
                        "status": "maintenance",
                    })
            except Exception as e:
                remote_operation.release(server, iid, token, {
                    "status": "failed", "operation": "config_update",
                    "error": str(e)})
                acquired = False
                failed.append({"host": host,
                               "error": f"配置更新失败，实例保持维护/停止: {e}"})
                continue
            remote_operation.release(server, iid, token, {
                "status": "maintenance", "operation": "config_update",
                "config_digest": plan["digest"]})
            acquired = False
            done.append({"host": host, "instance_id": iid, "unit": unit,
                         "digest": plan["digest"], "started": False,
                         "status": "maintenance"})
        except LockBusy as e:
            failed.append({"host": host, "error": str(e)})
        except Exception as e:
            failed.append({"host": host, "error": str(e)})
        finally:
            if acquired and not keep_owner:
                try:
                    remote_operation.release(server, iid, token, {
                        "status": "failed", "operation": "config_update",
                        "error": "配置更新异常中断"})
                except Exception:
                    pass
            server.close()
    audit("war_tomcat_config_update", "service", sid,
          {"env": env["name"], "ok": [h["host"] for h in done],
           "failed": failed})
    body = {"env": env["name"], "hosts": done, "failed": failed,
            "note": "配置已更新，实例未启动；请核验后结束维护并重启"}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route("/projects/<int:pid>/services/<int:sid>/tomcat/recovery",
                 methods=["GET"])
@require_auth
@require_role("admin")
def recovery_instance(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    env, servers, err = _env_servers(pid, request.args.get("env") or "")
    if err:
        return err
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    done, failed = [], []
    for srv in servers:
        server = RemoteServer(srv["host"],
                              merge_server_ssh(env.get("ssh") or {}, srv))
        try:
            manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
            iid = (manifest or {}).get("instance_id") or ""
            state = remote_operation.inspect(server, iid) if iid else {}
            owner = dict(state.get("owner") or {})
            owner.pop("token", None)
            done.append({
                "host": srv["host"], "instance_id": iid, "unit": unit,
                "config_status": (tomcat_instance.config_status(
                    manifest, iid, pid, env["name"], service)
                    if manifest else "unmanaged"),
                "maintenance": (manifest or {}).get("maintenance") or {},
                "owner": owner,
                "owner_digest": _owner_digest(server, iid) if iid else "",
                "last_result": state.get("last_result") or {},
            })
        except Exception as e:
            failed.append({"host": srv["host"], "error": str(e)})
        finally:
            server.close()
    body = {"env": env["name"], "hosts": done, "failed": failed}
    return (jsonify(body), 207) if failed else jsonify(body)


@tomcat_bp.route(
    "/projects/<int:pid>/services/<int:sid>/tomcat/recovery/revoke",
    methods=["POST"])
@require_auth
@require_role("admin")
def revoke_lock(pid, sid):
    rec, err = _service(pid, sid)
    if err:
        return err
    body = _body()
    if body is None:
        return _err("请求体必须为 JSON 对象")
    if body.get("confirm") is not True:
        return _err("必须显式确认撤销锁")
    host = body.get("host")
    iid = str(body.get("instance_id") or "")
    digest = str(body.get("confirm_digest") or "")
    if not isinstance(host, str) or not iid or not _HEX64.fullmatch(digest):
        return _err("host/instance_id/confirm_digest 缺失或格式错误")
    env, servers, err = _env_servers(pid, body.get("env") or "")
    if err:
        return err
    if host not in {s["host"] for s in servers}:
        return _err("host 不属于该环境", 404)
    try:
        cfg, service, unit = _model(rec)
    except ValueError as e:
        return _err(str(e))
    srv = next(s for s in servers if s["host"] == host)
    server = RemoteServer(host, merge_server_ssh(env.get("ssh") or {}, srv))
    try:
        manifest = tomcat_instance.read_manifest(server, cfg["app_home"])
        if (manifest or {}).get("instance_id") != iid:
            return _err("实例身份不一致，拒绝撤销", 409)
        remote_operation.revoke(server, iid, digest)
    except RuntimeError as e:
        return _err(f"锁撤销失败（摘要不符或已不存在）: {e}", 409)
    finally:
        server.close()
    audit("war_tomcat_lock_revoke", "service", sid,
          {"env": env["name"], "host": host, "instance_id": iid})
    return jsonify({"host": host, "revoked": True})
```

`webapp/__init__.py` 注册：导入 `tomcat_bp` 后
`app.register_blueprint(tomcat_bp, url_prefix="/api")`。

`webapp/routes/config_routes.py` 的 `_merge_host_row` 末尾按字段透传（不伪造
默认值，jar 行保持原输出）：

```python
    row = {
        "host": host,
        "unit": live.get("unit") or unit,
        "status": live.get("status") or ("unknown" if db_row else "not_deployed"),
        "version": live.get("version") or db_row.get("current_version") or "",
        "previous_version": db_row.get("previous_version") or "",
        "pid": live.get("pid") or 0,
        "uptime_hours": live.get("uptime_hours"),
        "restarts": live.get("restarts") or 0,
        "cpu": live.get("cpu"),
        "rss_mb": live.get("rss_mb"),
        "updated_at": db_row.get("updated_at") or 0,
        "live": bool(live),
    }
    for key in ("config_status", "operation_state", "operation_phase",
                "instance_id", "app_health", "process_status",
                "recovery_status", "version_drift", "lock_state"):
        if key in live:
            row[key] = live[key]
    if "maintenance" in live:
        row["maintenance"] = bool(live["maintenance"])
    return row
```

`webapp/routes/deploy_routes.py`：在布尔字段区追加

```python
    release_maintenance = _bool_field(body, "release_maintenance", False)
```

与其它布尔一起做 `None` 校验；`release_maintenance` 为真时只允许
`type_ == "restart"` 且 `deploy_mode == "war_tomcat"`，否则 400。在
`type_ == "restart"` 分支的 `extra` 构建处追加：

```python
        if release_maintenance:
            extra.append("--release-maintenance")
```

- [ ] **Step 4: 实现服务表单的 WAR 组**

`config-service-form.js` 的常量追加：

```javascript
const PROCESS_BUILD = { jar_service: 'maven', go_binary: 'go', rust_binary: 'rust',
                        node_process: 'node', python_process: 'python',
                        war_tomcat: 'maven' };
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
  war_tomcat: {
    app_home: '', tomcat_home: '', java_home: '', run_user: '',
    unit_name: '', port: '', context_path: '', jvm_opts: [], env_files: [],
    stop_timeout: 60, start_timeout: 120,
    readiness: { type: 'http', path: '/', expected_status: 200,
                 body_contains: '', timeout: 120, interval: 5 },
  },
};
```

`st` 增加 `envs: []`，`open()` 的加载改为：

```javascript
const [svcs, envs] = await Promise.all([
  api(`/api/projects/${project.id}/services`),
  api(`/api/projects/${project.id}/environments`).catch(() => []),
]);
st.svcs = svcs.filter(x => !svc || x.id !== svc.id);
st.envs = envs;
```

> 模式切换时 `PROCESS_RC_DEFAULT[d.deploy_mode]` 会与旧 `prev` 合并，从
> `jar_service` 切到 `war_tomcat` 时 `jvm_opts` 可能是字符串；WAR 表单已按
> “数组或字符串”归一化读取，保存时又整对象重建，因此不会把旧模式字段写进
> WAR 配置。不要依赖表单里的旧值。

`render()` 追加 `const isWar = d.deploy_mode === 'war_tomcat';` 与
`const isProcLike = isProc && !isWar;`；发布方式下拉在 `jar_service` 之后加一行
`<option value="war_tomcat" ${isWar ? 'selected' : ''}>🪶 WAR / Tomcat 独立实例（systemd）</option>`，
说明文案注明「固定独立实例、共享 CATALINA_HOME、WAR 版本化、不启用 Manager、
不管理 Tomcat/JDK 安装」。构建类型锁定条件改为
`isStatic || isJar || isWar`；构建步骤的两处判断都追加 `&& !isWar`：

- 渲染分支：`${isFrontend ? \`...\` : (isProc && !isJar ? \`...\` : \`...\`)}`
  的首个条件改为 `isProc && !isJar && !isWar`；
- 保存读取：`d.build_order = (... : (isProc && !isJar ? [] : H.rowList(...).read()))`
  的第一个条件改为 `isProc && !isJar && !isWar`；
- 分组数组里 `isProc ? H.section({ id:'jar', ... }) : ...` 的条件改为
  `isProcLike`，并在 `isWar` 时插入 WAR 分组（不要把 jar 分组与 WAR 分组同时渲染）。

新增 `warCfg`（字段 id 必须与测试一致）：

```javascript
const warCfg = isWar ? `
  <div class="form-grid">
    ${H.field({ label:'应用根目录（app_home）', required:true,
      input:`<input id="jAppHome" value="${esc(rc.app_home || '')}" placeholder="/opt/apps/orders">`,
      hint:'实例目录为 {app_home}/instance；平台不改账号、sudo 或防火墙' })}
    ${H.field({ label:'Tomcat 安装目录（tomcat_home）', required:true,
      input:`<input id="wTomcatHome" value="${esc(rc.tomcat_home || '')}" placeholder="/opt/tomcat">`,
      hint:'共享 CATALINA_HOME；平台不安装 Tomcat，只生成 CATALINA_BASE' })}
    ${H.field({ label:'JDK 目录（java_home）', required:true,
      input:`<input id="wJavaHome" value="${esc(rc.java_home || '')}" placeholder="/usr/lib/jvm/java-17">`,
      hint:'目标机已安装 JDK；平台不安装、不切换默认 Java' })}
    ${H.field({ label:'上下文路径（context_path）', required:true,
      input:`<input id="wContext" value="${esc(rc.context_path || '/')}" placeholder="/orders">`,
      hint:'/ 表示根上下文；生成外部 Context XML，不写入 server.xml' })}
    ${H.field({ label:'端口（port）', required:true,
      input:`<input id="jPort" type="number" value="${esc(rc.port || '')}" placeholder="18080">`,
      hint:'唯一 HTTP Connector 端口；关闭 AJP/Manager/远程 shutdown' })}
    ${H.field({ label:'运行用户（run_user）', required:true,
      input:`<input id="jRunUser" value="${esc(rc.run_user || '')}" placeholder="appuser">`,
      hint:'目标机已存在且 UID 非 0 的账号；平台不创建账号、不改 sudo' })}
    ${H.field({ label:'systemd 单元名（unit_name）',
      input:`<input id="jUnit" value="${esc(rc.unit_name || '')}" placeholder="留空默认 smartops-{服务}-war">` })}
    ${H.field({ label:'JVM 参数（jvm_opts，每行一项）',
      input:`<textarea id="wJvmOpts" rows="2" placeholder="-Xms512m&#10;-Xmx1g">${esc((Array.isArray(rc.jvm_opts) ? rc.jvm_opts : String(rc.jvm_opts || '').split('\n')).filter(Boolean).join('\n'))}</textarea>`,
      hint:'逐项写入 systemd argv；禁止换行注入、catalina.base/home、classpath 等平台接管参数' })}
    ${H.field({ label:'环境变量文件（env_files）',
      input:`<textarea id="wEnvFiles" rows="2" placeholder="/opt/apps/orders/.env">${esc((rc.env_files || []).join('\n'))}</textarea>`,
      hint:'只写路径；敏感值留在目标机已有环境文件，预览与审计不显示密钥' })}
    ${H.field({ label:'健康路径（readiness.path）', required:true,
      input:`<input id="wHealthPath" value="${esc(rc.readiness?.path || '/')}" placeholder="/health">`,
      hint:'经 SSH 直连 127.0.0.1:port 探活，不跟随跳转、不使用目标机 curl' })}
    ${H.field({ label:'响应体包含（可选）',
      input:`<input id="wBodyContains" value="${esc(rc.readiness?.body_contains || '')}" placeholder="可选固定文本">`,
      hint:'首版只接受 HTTP 200；响应体最多读取 1 MiB；超时即任务失败，不会静默切版本' })}
    ${H.field({ label:'健康超时/间隔（秒）',
      input:`<input id="wHealthTimeout" type="number" value="${esc(rc.readiness?.timeout || 120)}" style="width:45%"> <input id="wHealthInterval" type="number" value="${esc(rc.readiness?.interval || 5)}" style="width:45%">` })}
    ${H.field({ label:'停止/启动超时（秒）',
      input:`<input id="jStopTimeout" type="number" value="${esc(rc.stop_timeout || 60)}" style="width:45%"> <input id="jStartTimeout" type="number" value="${esc(rc.start_timeout || 120)}" style="width:45%">` })}
    <div class="field full"><label>目标环境</label>
      <select id="wEnv">${(st.envs || []).map(e => `<option value="${esc(e.name)}" ${e.is_default ? 'selected' : ''}>${esc(e.name)}</option>`).join('')}</select>
      <div class="f-hint">预览/接入/更新/恢复均作用于该环境绑定的全部主机；操作仅管理员可用。</div>
    </div>
    <div class="field full" style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" id="wPreviewBtn" data-war="preview" type="button" ${d.id ? '' : 'disabled title="保存后可预览"'}>预览实例配置</button>
      <button class="btn" id="wOnboardBtn" data-war="onboard" type="button" ${d.id ? '' : 'disabled'}>接入实例（不启动）</button>
      <button class="btn" id="wConfigBtn" data-war="config" type="button" ${d.id ? 'disabled title="实例接入后可用"' : 'disabled'}>更新配置（摘流并停止）</button>
      <button class="btn" id="wRecoveryBtn" data-war="recovery" type="button" ${d.id ? '' : 'disabled'}>人工恢复检查</button>
    </div>
    <div class="hint">配置更新会先保留流量快照并摘流，再受控停止实例并落配置；完成后不自动启动，只能选择「结束维护并重启」恢复原权重。</div>
    <pre id="wPreviewContent" class="weak" style="display:none;white-space:pre-wrap;font-size:12px;margin:0"></pre>
  </div>` : '';
```

分组数组在 `isWar` 时插入
`H.section({ id:'war', icon:'🪶', title:'WAR / Tomcat 实例', desc:'固定独立实例 / 预览与接入 / 受控配置更新', open:true, body: warCfg })`。
渲染后绑定：

```javascript
document.querySelectorAll('[data-war]').forEach(b => {
  b.onclick = () => warAction(b.dataset.war);
});

function warEnv() {
  const el = document.getElementById('wEnv');
  return el ? el.value : '';
}

function renderWar(data) {
  const box = document.getElementById('wPreviewContent');
  if (!box) return;
  const lines = [];
  for (const h of (data.hosts || [])) {
    lines.push(`[${h.host}] unit=${h.unit || '—'} instance=${h.instance_id || '—'}`
      + ` config=${h.config_status || '—'}`);
    if (h.digest) lines.push('  digest: ' + h.digest);
    if (h.status) lines.push('  status: ' + h.status
      + (h.started === false ? '（未启动）' : ''));
  }
  for (const f of (data.failed || [])) {
    lines.push(`[${f.host}] 失败: ${f.error}`);
  }
  if (data.note) lines.push('', data.note);
  const first = (data.hosts || [])[0];
  if (first && first.unit_content) lines.push('', '--- unit ---', first.unit_content);
  box.textContent = lines.join('\n');
  box.style.display = 'block';
}

async function warAction(kind) {
  if (!d.id) { toastError('请先保存服务'); return; }
  const env = warEnv();
  if (!env) { toastError('请先选择目标环境'); return; }
  const base = `/api/projects/${project.id}/services/${d.id}/tomcat`;
  try {
    if (kind === 'preview') {
      renderWar(await api(`${base}/preview?env=${encodeURIComponent(env)}`));
      return;
    }
    if (kind === 'recovery') {
      renderWar(await api(`${base}/recovery?env=${encodeURIComponent(env)}`));
      return;
    }
    const preview = await api(`${base}/preview?env=${encodeURIComponent(env)}`);
    if ((preview.failed || []).length) { renderWar(preview); return; }
    const hosts = (preview.hosts || []).map(h => ({
      host: h.host, instance_id: h.instance_id, digest: h.digest}));
    if (!hosts.length) { toastError('没有可操作主机'); return; }
    const label = kind === 'onboard'
      ? '接入（只写实例与 unit，不启动）'
      : '更新配置（先摘流并停止，完成后不启动）';
    if (!await confirmDialog(
        `确认对 ${hosts.length} 台主机${label}？\n${hosts.map(h => h.host).join('、')}`)) {
      return;
    }
    const body = { env, confirm: true, hosts };
    if (kind === 'config') body.action = 'update';
    const data = await api(`${base}/${kind === 'onboard' ? 'onboard' : 'config'}`,
      { method: 'POST', body: JSON.stringify(body) });
    renderWar(data);
    toastSuccess(kind === 'onboard'
      ? '实例已接入（未启动）'
      : '配置已更新（实例未启动，请结束维护并重启）');
  } catch (e) {
    // api() 对 207 抛错，逐主机结果在 e.data
    if (e.data && Array.isArray(e.data.failed)) {
      renderWar(e.data);
      toastError(`部分主机失败（${e.data.failed.length} 台）`);
    } else {
      toastError(e.message);
    }
  }
}
```

保存时的收集分支：把现有 `if (isProc) {` 改成 `if (isWar) {`，WAR 分支整体在前，
原 `if (isProc)` 的整段代码原样下移为新分支的 `} else if (isProc) {`（**不要**把
WAR 放进通用进程分支内部：通用分支会用 `jAppArgs`/`jWorkDir`/`jEnvFiles`/
`jReadiness*` 这些 WAR 表单里不存在的 id 覆盖 `runtime_config`）：

```javascript
      if (isWar) {
        const num = (id, dflt) => {
          const n = Number(val(id));
          return Number.isFinite(n) && n > 0 ? n : dflt;
        };
        d.runtime_config = {
          app_home: val('jAppHome').trim(),
          tomcat_home: val('wTomcatHome').trim(),
          java_home: val('wJavaHome').trim(),
          run_user: val('jRunUser').trim(),
          unit_name: val('jUnit').trim(),
          port: Number(val('jPort') || 0),
          context_path: val('wContext').trim() || '/',
          jvm_opts: val('wJvmOpts').split('\n').map(x => x.trim()).filter(Boolean),
          env_files: val('wEnvFiles').split('\n').map(x => x.trim()).filter(Boolean),
          stop_timeout: num('jStopTimeout', 60),
          start_timeout: num('jStartTimeout', 120),
          readiness: {
            type: 'http',
            path: val('wHealthPath').trim() || '/',
            expected_status: 200,
            body_contains: val('wBodyContains').trim(),
            timeout: num('wHealthTimeout', 120),
            interval: num('wHealthInterval', 5),
          },
        };
        const abs = [['jAppHome', '应用根目录'], ['wTomcatHome', 'Tomcat 目录'],
                     ['wJavaHome', 'JDK 目录']];
        for (const [id, label] of abs) {
          if (!val(id).trim().startsWith('/')) {
            H.fieldErr(id, label + '必须为绝对路径'); ok = false;
          } else H.fieldErr(id, '');
        }
        if (!val('wContext').trim().startsWith('/')) {
          H.fieldErr('wContext', '上下文路径必须以 / 开头'); ok = false;
        } else H.fieldErr('wContext', '');
        if (!val('wHealthPath').trim().startsWith('/')) {
          H.fieldErr('wHealthPath', '健康路径必须以 / 开头'); ok = false;
        } else H.fieldErr('wHealthPath', '');
        const p = d.runtime_config.port;
        if (!(Number.isInteger(p) && p >= 1024 && p <= 65535)) {
          H.fieldErr('jPort', '端口必须为 1024-65535 的整数'); ok = false;
        } else H.fieldErr('jPort', '');
        if (!d.runtime_config.run_user) {
          H.fieldErr('jRunUser', '运行用户必须填写目标机已存在的非 root 账号'); ok = false;
        } else H.fieldErr('jRunUser', '');
      } else if (isProc) {
```

WAR 的构建类型固定为 Maven，保存时按下面的替换改写现有那一行：
`d.build_type = isJar ? 'maven'` → `d.build_type = (isJar || isWar) ? 'maven'`。

- [ ] **Step 5: 主机表与重启入口**

`jar_hosts.js` 在渲染前判断：

```javascript
const warMode = rows.some(r => r && Object.prototype.hasOwnProperty.call(r, 'config_status'));
const CONFIG_LABEL = { managed: '受管', maintenance: '维护中',
                       owner_mismatch: '归属不一致', unmanaged: '未接入' };
const OP_LABEL = { idle: '空闲', in_progress: '操作中' };
const PHASE_LABEL = { preflight: '预检', traffic: '摘流/恢复流量',
                      stopping: '停止', switching: '切换', starting: '启动',
                      health: '健康检查', recovery: '自动恢复',
                      rollback_preflight: '回滚预检', rollback_stop: '回滚停止',
                      restart_preflight: '重启预检', restart_stop: '重启停止' };
const APP_LABEL = { ok: '健康', failed: '不健康', unknown: '未知' };
const RECOVERY_LABEL = { not_needed: '无需', succeeded: '成功',
                         blocked: '受阻', failed: '失败',
                         unavailable: '不可用', unknown: '未知' };
```

同文件顶部 `STATUS_LABEL` 追加 `unhealthy: '不健康'`、`unmanaged: '未托管'`，
`DOT_CLASS` 追加 `unhealthy: 'bad'`、`unmanaged: 'warn'`。`pages/devices.js`
的 `dotClass` 把 `unhealthy` 记为 `bad`、`unmanaged` 记为 `warn`，`svcLabel`
的映射追加同名字段；`data.js` 的第 3 条异常告警列表追加 `unhealthy`
（未托管不产生告警）。

表头在「数据」前插入
`warMode ? '<th>进程</th><th>应用</th><th>恢复</th><th>配置</th><th>漂移</th><th>维护</th><th>操作</th>' : ''`；
每行同样插入：

```javascript
${warMode ? `<td>${esc(STATUS_LABEL[r.process_status] || r.process_status || '—')}</td>
  <td>${esc(APP_LABEL[r.app_health] || r.app_health || '—')}</td>
  <td>${esc(RECOVERY_LABEL[r.recovery_status] || r.recovery_status || '—')}</td>
  <td>${esc(CONFIG_LABEL[r.config_status] || r.config_status || '—')}</td>
  <td>${r.version_drift ? '是' : '否'}</td>
  <td>${r.maintenance ? '是' : '否'}</td>
  <td>${esc(r.operation_state === 'in_progress'
            ? (PHASE_LABEL[r.operation_phase] || '操作中')
            : (OP_LABEL[r.operation_state] || r.operation_state || '—'))}</td>` : ''}
```

「进程」列与「状态」列不同源：`status` 是展示聚合（可能因应用不健康或未托管
降级），`process_status` 固定表示进程本身；两者都显示，操作者才能区分
“进程在跑但应用失败”与“进程已停”。

非 WAR 行不输出新列，现有 `jar_hosts_test.js` 断言不变。`jar_hosts_test.js`
追加一个 WAR 行用例：`status: 'unhealthy'` 时输出「不健康」，`status:
'unmanaged'` 时输出「未托管」，`process_status: 'online'` 输出「在线」，
`app_health: 'failed'` 输出「不健康」且与进程「在线」并存，
`recovery_status: 'succeeded'` 输出「成功」，`recovery_status: 'unknown'`
不得输出「成功」，`version_drift: true` 输出「是」；`operation_state:
'in_progress'` 且 `operation_phase: 'health'` 时「操作」列输出「健康检查」，
而非笼统「操作中」；非 WAR 行仍不含「进程/应用/恢复/配置/漂移/维护/操作」列。

`devices.js` 的 `restartService`：在确认重启前，若
`svc.deploy_mode === 'war_tomcat'`，追加一次显式询问：

```javascript
    let releaseMaintenance = false;
    if (svc.deploy_mode === 'war_tomcat') {
      releaseMaintenance = await confirmDialog(
        '该实例若处于维护状态，普通重启会被拒绝。是否同时结束维护并按配置更新前的快照恢复原流量权重？'
        + '\n\n选择「取消」只会执行普通重启（维护中会失败）。');
    }
```

请求体追加 `release_maintenance: releaseMaintenance`。

- [ ] **Step 6: 运行通过**

Run: `python -m pytest tests/test_tomcat_routes.py tests/test_war_tomcat_strategy.py tests/test_deploy_strict_integration.py -v`
Expected: PASS。
Run: `node tests/js/war_config_form_test.js && node tests/js/war_hosts_test.js && node tests/js/config_service_form_visibility_test.js && node tests/js/jar_hosts_test.js`
Expected: 全部 PASS；旧表单/旧主机表断言不变。

- [ ] **Step 7: 检查范围，不提交**

Run: `git diff --stat`
Expected: 仅本任务列出的文件与测试；未提交。

---

### Task 12: 文档、便携包完整性与全量回归

**Files:**
- Modify: `scripts/package_portable.py:59-60`
- Create: `tests/test_portable_manifest.py`
- Modify: `README.md:288-302`
- Modify: `docs/usage.md`（在 10.10 之后新增 10.11）
- Test: `tests/test_portable_manifest.py`

**Interfaces:**
- Consumes: Task 1–11 的全部模块、路由、前端与测试。
- Produces: 无运行期接口；保证便携包白名单覆盖运行期导入的根模块，文档与实现口径一致。

- [ ] **Step 1: 写失败测试**

`tests/test_portable_manifest.py`：

```python
"""便携包白名单必须覆盖运行期导入的根模块（含懒导入）。"""
import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "package_portable", ROOT / "scripts" / "package_portable.py")
pp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pp)

ROOT_MODULES = {p.stem for p in ROOT.glob("*.py")}
SCAN_DIRS = ("webapp", "deploy_strategies", "agent")


class PortableManifestTest(unittest.TestCase):
    def test_whitelisted_files_exist(self):
        for name in pp.SOURCE_FILES:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_runtime_root_modules_are_packaged(self):
        missing = set()
        for dirname in SCAN_DIRS:
            for path in sorted((ROOT / dirname).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif (isinstance(node, ast.ImportFrom) and node.level == 0
                          and node.module):
                        names = [node.module]
                    for name in names:
                        top = name.split(".")[0]
                        if top in ROOT_MODULES and top + ".py" not in pp.SOURCE_FILES:
                            missing.add(
                                f"{path.relative_to(ROOT)} -> {top}.py")
        self.assertEqual(sorted(missing), [], "便携包缺少运行期根模块")

    def test_war_modules_import(self):
        import importlib
        for name in ("deploy_strategies.tomcat_config",
                     "deploy_strategies.remote_operation",
                     "deploy_strategies.tomcat_health",
                     "deploy_strategies.tomcat_instance",
                     "deploy_strategies.tomcat_lifecycle",
                     "deploy_strategies.war_tomcat",
                     "webapp.routes.tomcat_routes"):
            importlib.import_module(name)
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_portable_manifest.py -v`
Expected: FAIL，`service_unit.py` 被 `webapp/routes/config_routes.py` 懒导入但不在白名单。

- [ ] **Step 3: 补白名单**

`scripts/package_portable.py`：

```python
SOURCE_FILES = ["deploy.py", "deploy_pipeline.py", "menu.py", "service_unit.py",
                "requirements.txt", "README.md", "LICENSE"]
```

- [ ] **Step 4: 运行通过**

Run: `python -m pytest tests/test_portable_manifest.py -v`
Expected: PASS（3 tests）。不改变 `RELEASE_VERSION`，不重新打包、不发版。

- [ ] **Step 5: 更新 README**

将 `README.md:288` 的「七种方式」改为「八种方式」，在 `python_process` 列表项后新增：

```markdown
- `war_tomcat`：WAR + 固定独立 Tomcat 实例 + Linux systemd。Maven 构建 WAR → 上传到目标机不可变版本目录 → 校验后暂存提交 → 受控停止实例 → 原子切换 `CURRENT` → 清理实例展开缓存 → 启动 → 应用 HTTP 健康；共享目标机已安装的 `CATALINA_HOME`，平台不安装 JDK/Tomcat、不启 Manager。
```

在 Node/Python 段落（`README.md:302`）之后新增：

```markdown
WAR/Tomcat（`deploy_mode=war_tomcat`）：每应用固定一个独立 `CATALINA_BASE={app_home}/instance`，共享目标机已安装的 `CATALINA_HOME`；WAR 版本化到 `{app_home}/releases/{version}/` 并原子切换，Tomcat/JDK 与实例配置不随应用回滚变化。接入、配置更新与恢复均为管理员操作并记审计：配置更新先保存流量快照与维护标记，再摘流、受控停止、落配置，完成后不自动启动；只有显式「结束维护并重启」才恢复原权重。WAR 成功必须通过应用 HTTP 健康（SSH 直连 `127.0.0.1`），`systemctl active` 不能替代。锁冲突、停止失败、旧包缺失或健康失败均 fail-closed，需管理员检查/解除。字段与操作见 `docs/usage.md`「WAR / Tomcat 独立实例发布（war_tomcat）」。
```

- [ ] **Step 6: 更新使用说明**

在 `docs/usage.md` 的 10.10 之后新增：

```markdown
### 10.11 WAR / Tomcat 独立实例发布（war_tomcat）

`war_tomcat` 面向「WAR 包 + 目标机已安装 Tomcat」的 Java Web 应用：每应用固定一个独立实例，`CATALINA_BASE={app_home}/instance`，共享目标机 `CATALINA_HOME`；平台只管理实例目录、WAR 版本、systemd 单元和归属/锁，不安装 JDK/Tomcat，不建账号，不改 sudo/防火墙，不启用 Manager/AJP/远程 shutdown。目标机仅支持 Linux + systemd；Windows 目标机与 Kubernetes/Helm 不在本方式范围内。

目标机前提：

- 已安装 JDK（`java_home`）与 Tomcat（`tomcat_home`），三个 Tomcat 文件模板（`web.xml`、`catalina.properties`、`logging.properties`）可读；模板缺失或读取失败会阻止接入，不会用其它版本模板顶替；
- `run_user` 必须是已存在且 UID 非 0 的账号，并只对制品/配置只读、对 `webapps`/`work`/`temp`/`logs` 可写；部署身份负责版本、配置、状态、注册表与锁的写入，平台不创建账号、不改权限模型；
- `app_home` 为 Linux 绝对路径，且与 `tomcat_home` 不相同、不互相包含；其下 `webapps`/`work`/`temp` 被平台视为可再生目录，应用上传文件、报表与持久缓存必须外置；
- 端口需唯一且未被占用，首版业务 Connector 只监听 IPv4 `0.0.0.0`，健康探测通过 SSH 直连目标机回环地址。

配置字段（`runtime_config`）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `app_home` | 是 | 实例、制品、状态根目录；接入后不可原地换根 |
| `tomcat_home` | 是 | 已安装 Tomcat 根目录（`CATALINA_HOME`），平台不写该目录 |
| `java_home` | 是 | 已安装 JDK/JRE 根目录，使用其 `bin/java` |
| `run_user` | 是 | 目标机已存在、UID 非 0 的账号 |
| `unit_name` | 否 | 缺省 `smartops-{服务}-war`；保存与比较时规范为同一 `.service` 名 |
| `port` | 是 | 1024–65535 的整数 |
| `context_path` | 是 | 根应用填 `/`（规范化为空）；其它如 `/orders`、`/team/orders`，不允许末尾 `/` |
| `artifact_pattern` | 否 | 默认 `target/*.war`，相对构建搜索根；禁止绝对路径与 `..`，全候选只能有一个 |
| `jvm_opts` | 否 | 字符串数组，逐项写入 systemd argv；禁止换行注入、classpath、`catalina.home/base`、临时目录等平台接管参数 |
| `env_files` | 否 | 目标机已有环境文件的绝对路径数组；平台只传路径，不读取值、不写入预览或审计 |
| `stop_timeout` / `start_timeout` | 否 | 正整数，默认 60 / 120 秒 |
| `readiness.path` | 是 | 相对应用根的绝对路径，如 `/health`；最终请求路径为 `context_path + path` |
| `readiness.expected_status` | 否 | 首版只支持 200 |
| `readiness.body_contains` | 否 | 可选固定文本，按字面量包含检查；不猜测 `UP`/`ok` |
| `readiness.timeout` / `interval` | 否 | 默认 120 / 5 秒，正数；总等待与单请求超时共同受截止时间约束 |

操作流程：

1. **预览**：管理员在服务表单选择环境后预览，逐主机返回实例身份、配置摘要、unit 内容、受管文件与接入状态；任一台读取失败按 207 返回逐主机结果，不整体回滚。
2. **接入**：确认后只写实例目录、归属清单与 systemd 单元并 `daemon-reload`/`enable`，不启动应用；接入前重算摘要，摘要变化或实例已有其它归属时拒绝，不覆盖。
3. **发布**：Maven 构建出唯一 WAR → 上传到 `.incoming/{operation_id}` → 校验 SHA-256 → 暂存提交到 `releases/{version}/app.war` → 受控停止旧实例 → 原子切换 `CURRENT` → 清理该实例 appBase 展开副本/work/temp → 启动 → 应用 HTTP 健康。版本目录不可变，回滚只切换链接、不重传。
4. **配置更新**：管理员确认后，在单 owner 锁内校验受管文件未漂移，保存流量快照并写维护标记，再摘流、受控停止、落新配置；完成后**不自动启动**，状态为「维护中」。普通重启会被拒绝，只有「结束维护并重启」会恢复原流量权重并清除维护标记。
5. **恢复**：断线或失败后管理员可查看脱敏 owner、owner 文件摘要、最近结果与配置状态；确认摘要后解除锁。锁已消失或摘要不符时拒绝解除并返回冲突。
6. **观测**：周期探测同时展示进程状态、应用 HTTP 健康、每主机版本（以 `CURRENT` 为准并标出版本漂移）、配置状态（受管/维护中/归属不一致/未接入）、恢复状态（无需/成功/受阻/失败/不可用/未知）与操作状态（空闲/操作中及当前阶段：预检、摘流、停止、切换、启动、健康检查、自动恢复）；`active` 不等于发布成功，`unknown` 不等于成功。

限制与已知取舍：

- 单实例发布在「停止 → 切换 → 启动」期间不可用，平台不承诺零停机；
- 应用回滚只回滚 WAR，不恢复实例配置、JDK/Tomcat、数据库或外置数据；
- 环境文件内容不纳入配置指纹，外部修改由运维负责；健康接口首版不支持需要认证密钥的端点；
- 锁是平台之间的协调协议，不能防御拥有目标机 root 权限的人；绕过协议手工操作此类实例属于运维禁令，平台只保证自身操作 fail-closed；
- 首次接入、应用发布、配置更新、重启、回滚和恢复的实际行为仍需在 Tomcat 9 + Java 8、Tomcat 10.1 + Java 11、Tomcat 11 + Java 17 的 Linux + systemd 环境验收；文档与自动化测试不能替代真机验收。
```

- [ ] **Step 7: 全量回归与质量门禁**

Run: `python -m pytest -q`
Expected: 全绿，0 failures；旧七种模式的测试断言不变。

Run: `python scripts/quality_gate.py`
Expected: 无 FAIL；ruff/node 缺失时对应步骤为 SKIP，不算通过新功能验证。

Run: `node tests/js/war_config_form_test.js && node tests/js/war_hosts_test.js && node tests/js/config_service_form_visibility_test.js && node tests/js/jar_hosts_test.js`
Expected: 全部 PASS。

- [ ] **Step 8: 真机验收清单（本计划不执行）**

以下检查在隔离的 Linux + systemd 测试机执行，至少覆盖三组运行时：Tomcat 9 + Java 8、Tomcat 10.1 + Java 11、Tomcat 11 + Java 17。执行前准备独立 `app_home`、非 root 运行账号与一个测试 WAR；不得在真实生产机或未授权环境操作。

```text
[ ] 首次接入只写文件/单元并 enable，实例未启动，审计有 war_tomcat_onboard
[ ] 根 context（/）与非根 context（/orders）均能启动并通过 HTTP 健康
[ ] 连续发布两个版本，实际响应内容随版本切换，CURRENT 指向新版本
[ ] 停机后旧 appBase 展开副本/work/temp 被清理，未残留旧应用
[ ] 健康检查持续失败时任务失败，按严格语义停止/恢复，不报告成功
[ ] 首次启动失败时不再继续后续步骤，结果明确为失败
[ ] 进程实际以 run_user 运行，UID 非 0；未创建账号/改 sudo/开防火墙
[ ] 普通重启通过；维护中的普通重启被拒绝
[ ] 配置更新后实例保持停止、状态为维护中；结束维护并重启后恢复原权重
[ ] 手工修改受管文件或 unit 后，发布/配置更新被漂移检查拦下
[ ] 两台平台实例同时操作同一实例时，仅一方获得 owner，另一方 fail-closed
[ ] SSH 中断后 owner 保留，管理员按摘要检查并解除后可恢复
[ ] server.xml 无 AJP/Manager/远程 shutdown，只有业务 HTTP Connector
[ ] 目标机原有 Tomcat/JDK 安装未被平台改写
```

上述清单本轮**未执行**，在完成前不得把 WAR/Tomcat 描述为已在真实环境验证。

- [ ] **Step 9: 检查范围，不提交**

Run: `git status --short && git diff --stat`
Expected: 文档、便携包白名单、新增测试与前 11 个任务文件；无 `git commit`、无 tag、无发布。

