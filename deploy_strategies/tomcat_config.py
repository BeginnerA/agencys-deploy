"""WAR/Tomcat 配置：纯解析、校验、命名与渲染，不做 SSH/IO。"""
import hashlib
import json
import re
import shlex
from xml.sax.saxutils import escape

_SAFE_PATH = re.compile(r"^/(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+$")
_UNIT = re.compile(r"^[A-Za-z0-9_.@-]{1,120}$")
_RUN_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_CONTEXT_SEG = re.compile(r"^[A-Za-z0-9_-]+$")
_FORBIDDEN_OPTS = ("-cp", "-classpath", "@", "-Dcatalina.base=", "-Dcatalina.home=",
                   "-Djava.io.tmpdir=", "-Djava.util.logging.config.file=",
                   # 裸形式（无 "="）同样由平台接管，用户也不得覆盖
                   "-Dcatalina.base", "-Dcatalina.home", "-Djava.io.tmpdir",
                   "-Djava.util.logging.config.file",
                   "-Djava.util.logging.manager=", "-Djava.util.logging.manager",
                   "-jar")

# 模板版本参与指纹：模板内容调整需同步提升，避免旧预览被误认为同配置
TEMPLATE_VERSION = "tomcat-template-v1"
_REQUIRED_TEMPLATES = ("web.xml", "catalina.properties", "logging.properties")
# 构建输入不属于实例配置，不进指纹（spec §178）；只剔除这些键，其余字段全部参与
_DIGEST_EXCLUDED_KEYS = ("artifact_pattern",)
# Description 值折叠：换行/控制字符/反斜杠 → 空格（反斜杠是 systemd 续行符）
_DESCRIPTION_FOLD = {code: " " for code in (*range(32), 127, ord("\\"))}


def normalize_context_path(value: str) -> str:
    value = _text(value).strip()
    if value in ("", "/"):
        return ""
    return "/" + value.strip("/")


def _text(value) -> str:
    """标量按原样转字符串（由校验拒绝错类型）；None/缺失按空串，保证解析不抛异常。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


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
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def parse_war_config(runtime_config: dict) -> dict:
    rc = _as_dict(runtime_config)
    readiness = _as_dict(rc.get("readiness"))

    def as_int(value, default):
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    unit = _text(rc.get("unit_name")).strip()
    if unit.endswith(".service"):
        unit = unit[:-8]
    return {
        "app_home": _text(rc.get("app_home")).strip().rstrip("/"),
        "tomcat_home": _text(rc.get("tomcat_home")).strip().rstrip("/"),
        "java_home": _text(rc.get("java_home")).strip().rstrip("/"),
        "run_user": _text(rc.get("run_user")).strip(),
        "unit_name": unit,
        "port": as_int(rc.get("port"), 0),
        "context_path": normalize_context_path(rc.get("context_path")),
        "artifact_pattern": _text(rc.get("artifact_pattern")).strip() or "target/*.war",
        "jvm_opts": _as_list(rc.get("jvm_opts")),
        "env_files": _as_list(rc.get("env_files")),
        "stop_timeout": as_int(rc.get("stop_timeout"), 60),
        "start_timeout": as_int(rc.get("start_timeout"), 120),
        "readiness": {
            "type": "http",
            "path": _text(readiness.get("path")).strip(),
            "expected_status": as_int(readiness.get("expected_status"), 200),
            "body_contains": _text(readiness.get("body_contains")),
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


def _has_control(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _safe_path(value: str) -> bool:
    if not isinstance(value, str) or _has_control(value):
        return False
    # fullmatch：避免末尾换行借 "$" 的尾随匹配绕过检查
    if not _SAFE_PATH.fullmatch(value):
        return False
    return not any(seg in (".", "..") for seg in value.split("/"))


def validate_war_config(runtime_config: dict) -> list[str]:
    raw = _as_dict(runtime_config)
    cfg = parse_war_config(raw)
    errors = []
    for key in ("jvm_opts", "env_files"):
        if key in raw and not isinstance(raw[key], list):
            errors.append(f"{key} 必须是数组")
    for field in ("app_home", "tomcat_home", "java_home", "run_user",
                  "unit_name", "artifact_pattern", "context_path"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            errors.append(f"{field} 必须是字符串")
    for field in ("stop_timeout", "start_timeout"):
        if isinstance(raw.get(field), bool):
            errors.append(f"{field} 必须是正整数")
    readiness_raw = raw.get("readiness")
    if isinstance(readiness_raw, dict):
        for key in ("path", "body_contains"):
            if (readiness_raw.get(key) is not None
                    and not isinstance(readiness_raw[key], str)):
                errors.append(f"readiness.{key} 必须是字符串")
    for field in ("app_home", "tomcat_home", "java_home"):
        if not _safe_path(cfg[field]):
            errors.append(f"{field} 必须是安全的绝对路径")
    homes = ("app_home", "tomcat_home", "java_home")
    for i, left in enumerate(homes):
        for right in homes[i + 1:]:
            if cfg[left] and cfg[right] and (
                    cfg[left] == cfg[right]
                    or cfg[left].startswith(cfg[right] + "/")
                    or cfg[right].startswith(cfg[left] + "/")):
                errors.append(f"{left} 与 {right} 不得相同或互相包含")
    if not _RUN_USER.match(cfg["run_user"]) or cfg["run_user"].lower() == "root":
        errors.append("run_user 必须是已存在的非 root 账号名")
    if cfg["unit_name"] and not _UNIT.match(cfg["unit_name"]):
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
            or _has_control(path)
            or any(ch in path for ch in ("?", "#", "\\", "\n", "\r", "%"))):
        errors.append("readiness.path 必须是相对应用根的绝对路径，不能含查询、片段或穿越")
    if r["expected_status"] != 200:
        errors.append("首版 readiness.expected_status 仅支持 200")
    if "\n" in str(r["body_contains"]):
        errors.append("readiness.body_contains 不能含换行")
    if r["timeout"] <= 0 or r["interval"] <= 0 or r["interval"] > r["timeout"]:
        errors.append("readiness.timeout 必须大于 interval 且均为正数")
    return errors


def _xml_attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def _pct(value: str) -> str:
    """systemd 对单元内的 % 做说明符展开（单引号也不能阻止），必须写成 %%。"""
    return value.replace("%", "%%")


def _unit_text(value: str) -> str:
    """单行指令值：折叠换行、控制字符与反斜杠，避免借 Description 注入或续行。"""
    return _pct(str(value).translate(_DESCRIPTION_FOLD))


def _sd(value: str) -> str:
    """ExecStart 单个参数：shlex 引用防分词；%→%% 防说明符；$→$$ 即禁用环境变量展开。"""
    return _pct(shlex.quote(value)).replace("$", "$$")


def render_instance_files(cfg: dict, service: str, templates: dict) -> dict:
    """渲染 instance/ 下的受管配置；键为相对 instance/ 的路径，值为完整文件内容。"""
    templates = templates if isinstance(templates, dict) else {}
    missing = [name for name in _REQUIRED_TEMPLATES
               if not isinstance(templates.get(name), str)
               or not templates[name].strip()]
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
            unpackWARs="false" autoDeploy="false" deployOnStartup="true"
            copyXML="false"/>
    </Engine>
  </Service>
</Server>
"""
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
    """完整 systemd 单元：直接起 JVM，平台控制 catalina home/base/tmpdir/日志。"""
    app = cfg["app_home"]
    base = f"{app}/instance"
    tomcat = cfg["tomcat_home"]
    cp = f"{tomcat}/bin/bootstrap.jar:{tomcat}/bin/tomcat-juli.jar"
    argv = [
        f"{cfg['java_home']}/bin/java",
        # 用户参数紧随 java：平台 -D 在其后，Java 末次重复生效，平台始终胜出
        *cfg["jvm_opts"],
        f"-Dcatalina.home={tomcat}",
        f"-Dcatalina.base={base}",
        f"-Djava.io.tmpdir={base}/temp",
        f"-Djava.util.logging.config.file={base}/conf/logging.properties",
        "-Djava.util.logging.manager=org.apache.juli.ClassLoaderLogManager",
        "-classpath", cp,
        "org.apache.catalina.startup.Bootstrap", "start",
    ]
    exec_start = " ".join(_sd(x) for x in argv)
    lines = [
        "[Unit]", f"Description={_unit_text(service)}", "After=network.target", "",
        "[Service]", "Type=simple", f"User={_pct(cfg['run_user'])}",
        f"WorkingDirectory={_pct(base)}", f"ExecStart={exec_start}",
        "SuccessExitStatus=143", "KillMode=control-group", "Restart=no",
        f"TimeoutStopSec={cfg['stop_timeout']}",
        f"TimeoutStartSec={cfg['start_timeout']}",
        *[f"EnvironmentFile={_pct(p)}" for p in cfg["env_files"]],
        "", "[Install]", "WantedBy=multi-user.target", "",
    ]
    return "\n".join(line for line in lines if line)


def config_digest(cfg: dict, files: dict,
                  template_version: str = TEMPLATE_VERSION,
                  unit_content: str = "") -> str:
    """配置指纹：规范化配置 + 模板版本 + 受管文件内容 + 单元文本，稳定排序、无时间戳。

    `unit_content` 是 `render_war_unit` 渲染出的完整单元文本，Task 6/7 调用方必须传入；
    缺省空串时 `unit_sha256` 仍存在，保证不传的调用方指纹稳定。
    构建输入（`artifact_pattern`）不属于实例配置，剔除后参与哈希；
    其余字段（含 readiness、各类超时）全部参与。过滤生成新字典，不改调用方入参。
    """
    config = {key: value for key, value in cfg.items()
              if key not in _DIGEST_EXCLUDED_KEYS}
    payload = {
        "config": config,
        "files": {key: files[key] for key in sorted(files)},
        "template_version": template_version,
        "unit_sha256": hashlib.sha256(unit_content.encode("utf-8")).hexdigest(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
