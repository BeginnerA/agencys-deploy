"""DB → 旧格式 YAML 导出：全量导出（Dashboard 手动/CI 一致性）+ 运行时导出（deploy.py db 模式）"""
import re
from pathlib import Path

from . import db as dbm

# 文件名安全字符：仅允许字母数字下划线连字符（防路径穿越）
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+$")


def _check_safe_name(value: str, what: str):
    if not _SAFE_NAME.match(value):
        raise ValueError(f"非法{what}: {value!r}（仅允许字母数字 _ -）")


def _yaml_dump(data: dict) -> str:
    import yaml
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


def export_projects_yaml(db_path: str, project_id: int) -> str:
    """导出 projects.yaml（单个项目 + 其全部服务）"""
    proj = dbm.get_record(db_path, "projects", project_id)
    projects = {}
    for s in dbm.list_services(db_path, project_id):
        if not s["enabled"]:
            continue
        build_order = []
        for b in s["build_order"] or []:
            if not isinstance(b, dict):
                continue
            entry = {"name": b.get("name", ""), "path": b.get("path", ""),
                     "goal": b.get("goal", "package")}
            if b.get("extra_profile"):
                entry["extra_profile"] = b["extra_profile"]
            build_order.append(entry)
        cfg = {
            "build_order": build_order,
            "docker_context": s["docker_context"],
            "dockerfile": s["dockerfile"],
            "image_name": s["image_name"],
            "service_name": s["service_name"],
            "git_repo": s["git_repo"],
            "nacos_groups": s["nacos_groups"] or [],
            "dubbo_services": s["dubbo_services"] or [],
            "snailjob_group": s["snailjob_group"],
        }
        if s["health_url"]:
            cfg["health_url"] = s["health_url"]
        if s["health_port"]:
            cfg["health_port"] = s["health_port"]
        if s["clear_log_dir"]:
            cfg["clear_log_dir"] = s["clear_log_dir"]
        cfg["log_error_keywords"] = s["log_error_keywords"] or []
        projects[s["name"]] = cfg
    return _yaml_dump({"projects": projects})


def export_pipelines_yaml(db_path: str, project_id: int) -> str:
    pipelines = {}
    for p in dbm.list_pipelines(db_path, project_id):
        stages = []
        missing = []
        for st in p["stages"] or []:
            if not isinstance(st, dict):
                continue
            services = []
            for svc in st.get("services", []) or []:
                if not isinstance(svc, dict):
                    continue
                s = dbm.get_record(db_path, "services", svc.get("service_id", 0))
                if not s:
                    missing.append(svc.get("name") or f"service_id={svc.get('service_id')}")
                    continue
                entry = {"name": s["name"]}
                if svc.get("version"):
                    entry["version"] = svc["version"]
                if svc.get("previous_version"):
                    entry["previous_version"] = svc["previous_version"]
                services.append(entry)
            stages.append({"name": st.get("name", ""),
                           "parallel": bool(st.get("parallel", False)),
                           "services": services})
        if missing:
            raise ValueError(f"流水线 {p['name']} 引用不存在的服务: "
                             + ", ".join(dict.fromkeys(missing)))
        pipelines[p["name"]] = {"build_first": bool(p["build_first"]),
                                "stages": stages}
    return _yaml_dump({"pipelines": pipelines})


def export_env_yaml(db_path: str, env: dict) -> str:
    """导出 deploy.{env}.yaml 单文件（registry/nacos/mysql_snailjob/ssh/build/deploy）"""
    deploy = dict(env.get("deploy") or {})
    build = dict(env.get("build") or {})
    # 环境表有独立列存储 docker_compose_path / maven_profile，
    # 即使 deploy/build JSON 中未包含也要合并进去，保证导出 YAML 字段完整
    dcp = env.get("docker_compose_path")
    if dcp and "docker_compose_path" not in deploy:
        deploy["docker_compose_path"] = dcp
    mp = env.get("maven_profile")
    if mp and "maven_profile" not in build:
        build["maven_profile"] = mp
    cfg = {
        "registry": env.get("registry") or {},
        "nacos": env.get("nacos") or {},
        "mysql_snailjob": env.get("mysql_snailjob") or {},
        "ssh": env.get("ssh") or {},
        "build": build,
        "deploy": deploy,
    }
    return _yaml_dump(cfg)


def export_servers_yaml(db_path: str, env: dict) -> str:
    servers = []
    env_ssh = dict(env.get("ssh") or {})
    for s in dbm.list_env_servers(db_path, env["id"]):
        # 服务器级 SSH 覆盖优先于环境级：DB 为 NULL（未配置）才继承环境级，
        # 显式值（含空串 = 禁用密码认证）一律覆盖
        ssh = dict(env_ssh)
        if s["ssh_port"] is not None:
            ssh["port"] = s["ssh_port"]
        if s["ssh_user"] is not None:
            ssh["user"] = s["ssh_user"]
        if s["ssh_password"] is not None:
            ssh["password"] = s["ssh_password"]
        if s["ssh_key_file"] is not None:
            ssh["key_file"] = s["ssh_key_file"]
        entry = {"ip": s["host"]}
        if s["nacos_ip"]:
            entry["nacos_ip"] = s["nacos_ip"]
        if s["remark"]:
            entry["remark"] = s["remark"]
        entry["ssh"] = ssh
        servers.append(entry)
    return _yaml_dump({"servers": servers})


def export_all(db_path: str, target_dir: Path) -> list:
    """全量导出到 target_dir，返回生成文件列表。
    单项目时输出 config/ 兼容名（projects.yaml/pipelines.yaml，可直接复制为 config/）；
    多项目时按 code 加后缀（projects.{code}.yaml）避免冲突（deploy/servers 的 prod 无后缀命名
    在多项目下仍会冲突，属旧格式结构局限，多项目迁移需手动调整）。"""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    written = []
    projs = dbm.list_records(db_path, "projects")
    single = len(projs) == 1
    for proj in projs:
        _check_safe_name(proj["code"], "项目 code")
        suffix = "" if single else f".{proj['code']}"
        (target_dir / f"projects{suffix}.yaml").write_text(
            export_projects_yaml(db_path, proj["id"]), encoding="utf-8")
        (target_dir / f"pipelines{suffix}.yaml").write_text(
            export_pipelines_yaml(db_path, proj["id"]), encoding="utf-8")
        written.append(f"projects{suffix}.yaml")
        written.append(f"pipelines{suffix}.yaml")
        for env in dbm.list_environments(db_path, proj["id"]):
            _check_safe_name(env["name"], "环境名")
            fname = f"deploy.{env['name']}.yaml" if env["name"] != "prod" else "deploy.yaml"
            (target_dir / fname).write_text(export_env_yaml(db_path, env),
                                            encoding="utf-8")
            sname = f"servers.{env['name']}.yaml" if env["name"] != "prod" else "servers.yaml"
            (target_dir / sname).write_text(export_servers_yaml(db_path, env),
                                            encoding="utf-8")
            written.append(fname)
            written.append(sname)
    return written


def export_runtime(db_path: str, project_id: int, env: str,
                   target_dir: Path) -> Path:
    """deploy.py db 模式：生成与 config/ 同构的临时 YAML 目录，返回该目录"""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    env_obj = None
    if env:
        env_obj = dbm.get_env_by_name(db_path, project_id, env)
        if env_obj is None:
            raise ValueError(f"项目不存在环境 '{env}'，请检查参数")
    else:
        env_obj = dbm.get_default_env(db_path, project_id)
    if env_obj is None:
        raise ValueError("该项目未配置任何环境，请先在 Dashboard 配置")
    name = env_obj["name"]
    _check_safe_name(name, "环境名")
    (target_dir / "projects.yaml").write_text(
        export_projects_yaml(db_path, project_id), encoding="utf-8")
    (target_dir / "pipelines.yaml").write_text(
        export_pipelines_yaml(db_path, project_id), encoding="utf-8")
    (target_dir / ("deploy.yaml" if name == "prod" else f"deploy.{name}.yaml")).write_text(
        export_env_yaml(db_path, env_obj), encoding="utf-8")
    (target_dir / ("servers.yaml" if name == "prod" else f"servers.{name}.yaml")).write_text(
        export_servers_yaml(db_path, env_obj), encoding="utf-8")
    return target_dir
