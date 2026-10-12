"""YAML → DB 导入（首启引导/手动导入共用）：解析 config/ 旧格式，逐文件容错"""
import time
from pathlib import Path

from . import db as dbm

IMPORT_FILES = ["projects.yaml", "pipelines.yaml", "deploy.yaml",
                "deploy.test.yaml", "servers.yaml", "servers.test.yaml"]
# 注意顺序依赖：servers*.yaml 必须排在 deploy*.yaml 之后
# （_import_servers 要求目标环境已存在，环境由 deploy*.yaml 创建）

ENV_NAME = {"deploy.yaml": "prod", "servers.yaml": "prod",
            "deploy.test.yaml": "test", "servers.test.yaml": "test"}


def load_yaml(path: Path) -> dict:
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _import_projects(db_path: str, path: Path) -> tuple:
    """projects.yaml → 1 项目 + N 服务；返回 (ok, message)"""
    data = load_yaml(path)  # 模块级 load_yaml（与 common.load_yaml 同为 yaml.safe_load）
    projects = data.get("projects", {})
    if not projects:
        return False, "projects.yaml 无 projects 段"
    # 项目：优先复用已有项目（与流水线/服务器/环境导入一致）；空库时创建通用存量项目
    # （多项目平台下新项目应走前端手工创建）
    pid = _first_project_id(db_path)
    if pid is None:
        code = "legacy"
        pid = dbm.create_record(db_path, "projects", dict(
            name="存量项目", code=code, description="从旧配置导入", git_org="",
            created_at=int(time.time())))
    else:
        code = dbm.get_record(db_path, "projects", pid)["code"]
    n = 0
    for name, cfg in projects.items():
        if dbm.get_service_by_name(db_path, pid, name):
            continue
        build_order = cfg.get("build_order", []) or []
        # 前端项目的 build_order 元素含 commands 数组（无 goal 字段），原样保留
        normalised_build_order = []
        for b in build_order:
            # 步骤级构建环境变量（如 NODE_OPTIONS）原样保留，与 exporter 往返对称
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
            deploy_mode=cfg.get("deploy_mode") or "container",
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
    return True, f"项目 code={code}，导入服务 {n} 个"


def _import_pipelines(db_path: str, path: Path) -> tuple:
    data = load_yaml(path)
    pipelines = data.get("pipelines", {})
    if not pipelines:
        return False, "pipelines.yaml 无 pipelines 段"
    pid = _first_project_id(db_path)
    if pid is None:
        return False, "无项目，请先导入 projects.yaml"
    n = 0
    skipped = []
    for name, cfg in pipelines.items():
        if dbm.get_pipeline_by_name(db_path, pid, name):
            continue
        stages = []
        for st in cfg.get("stages", []) or []:
            svc_ids = []
            for svc in st.get("services", []) or []:
                s = dbm.get_service_by_name(db_path, pid, svc.get("name", ""))
                if s:
                    svc_ids.append({"service_id": s["id"],
                                    "version": svc.get("version", ""),
                                    "previous_version": svc.get("previous_version", "")})
                else:
                    skipped.append(svc.get("name", ""))
            stages.append({"name": st.get("name", ""),
                           "parallel": bool(st.get("parallel", False)),
                           "services": svc_ids})
        dbm.create_record(db_path, "pipelines", dict(
            project_id=pid, name=name,
            build_first=1 if cfg.get("build_first", True) else 0,
            stages=stages))
        n += 1
    msg = f"导入流水线 {n} 条"
    if skipped:
        msg += f"，跳过未找到服务: {', '.join(sorted(set(skipped)))}"
    return True, msg


def _first_project_id(db_path: str) -> int:
    rows = dbm.list_records(db_path, "projects")
    return rows[0]["id"] if rows else None


def _import_servers(db_path: str, path: Path, env_name: str) -> tuple:
    data = load_yaml(path)
    servers = data.get("servers", [])
    if not servers:
        return False, f"{path.name} 无 servers 段"
    pid = _first_project_id(db_path)
    if pid is None:
        return False, "无项目，请先导入 projects.yaml"
    env = dbm.get_env_by_name(db_path, pid, env_name)
    if env is None:
        return False, f"环境 {env_name} 不存在（请先导入 deploy.{env_name}.yaml）"
    n = 0
    ssh_override = 0
    for idx, s in enumerate(servers):
        host = s.get("ip", "")
        if not host:
            continue
        ssh_cfg = s.get("ssh") or {}
        # 覆盖统计：仅计入实际写入非 NULL 的覆盖字段（含 port；显式 null = 继承，不计入）
        override_vals = [ssh_cfg.get(k) for k in ("port", "user", "password", "key_file")]
        is_override = any(v is not None for v in override_vals)
        row = None
        for x in dbm.list_records(db_path, "servers", "host=?", (host,)):
            row = x
            break
        if row is None:
            # ssh 三态：无 key → NULL（导出时继承环境级）；显式值（含空串）→ 覆盖
            sid = dbm.create_record(db_path, "servers", dict(
                host=host, ssh_port=ssh_cfg.get("port"),
                ssh_user=ssh_cfg.get("user"),
                ssh_password=ssh_cfg.get("password"),
                ssh_key_file=ssh_cfg.get("key_file"),
                nacos_ip=s.get("nacos_ip", ""),
                remark=s.get("remark", "") or "", enabled=1))
        else:
            sid = row["id"]
        dbm.bind_server(db_path, env["id"], sid, idx)
        n += 1
        if is_override:
            ssh_override += 1
    msg = f"环境 {env_name} 绑定服务器 {n} 台"
    if ssh_override:
        msg += f"（{ssh_override} 台含服务器级 SSH 覆盖配置）"
    return True, msg


def _import_deploy(db_path: str, path: Path, env_name: str) -> tuple:
    data = load_yaml(path)
    pid = _first_project_id(db_path)
    if pid is None:
        return False, "无项目，请先导入 projects.yaml"
    env = dbm.get_env_by_name(db_path, pid, env_name)
    cfg = {k: data.get(k, {}) for k in
           ("registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy")}
    deploy_cfg = data.get("deploy", {}) or {}
    build_cfg = data.get("build", {}) or {}
    fields = dict(
        registry=cfg["registry"] or {},
        nacos=cfg["nacos"] or {},
        mysql_snailjob=cfg["mysql_snailjob"] or {},
        ssh=cfg["ssh"] or {},
        build=build_cfg,
        deploy=deploy_cfg,
        docker_compose_path=deploy_cfg.get("docker_compose_path", ""),
        maven_profile=build_cfg.get("maven_profile", ""),
        monitor_enabled=1)
    if env:
        dbm.update_record(db_path, "environments", env["id"], fields)
        return True, f"环境 {env_name} 已存在，配置已刷新"
    fields["project_id"] = pid
    fields["name"] = env_name
    fields["is_default"] = 1 if env_name == "prod" else 0
    dbm.create_record(db_path, "environments", fields)
    return True, f"创建环境 {env_name}"


def import_from_config(db_path: str, config_dir: Path,
                       on_error: callable = None) -> dict:
    """导入 config/ 下全部文件；返回 {file: (ok, message)}
    on_error(fname, exc) 可选回调：返回 False 跳过该文件继续，返回 True 中止导入"""
    dbm.init_db(db_path)  # 自包含：确保表结构存在（幂等）
    config_dir = Path(config_dir)
    results = {}
    for fname in IMPORT_FILES:
        path = config_dir / fname
        if not path.exists():
            results[fname] = (False, "文件不存在")
            continue
        try:
            if fname == "projects.yaml":
                results[fname] = _import_projects(db_path, path)
            elif fname == "pipelines.yaml":
                results[fname] = _import_pipelines(db_path, path)
            elif fname.startswith("servers") or fname.startswith("deploy"):
                env_name = ENV_NAME.get(fname)
                if env_name is None:
                    results[fname] = (False, "未注册的文件模式")
                    continue
                if fname.startswith("servers"):
                    results[fname] = _import_servers(db_path, path, env_name)
                else:
                    results[fname] = _import_deploy(db_path, path, env_name)
        except Exception as e:
            results[fname] = (False, f"解析失败: {e}")
            if on_error and on_error(fname, e):
                break
    return results


def needs_onboarding(db_path: str) -> bool:
    """空库（无项目）→ 需要首启引导"""
    return dbm.project_count(db_path) == 0
