#!/usr/bin/env python3
"""智能运维平台多服务流水线发布

示例：
    python deploy_pipeline.py --pipeline xxx --version 1.0.29 --previous-version 1.0.28

说明：
    按照 config/pipelines.yaml 中定义的 stage 顺序发布微服务。
    每个 stage 可以串行（parallel=false）或并行（parallel=true）执行。
    发布失败会自动回滚对应服务，并停止后续 stage。
"""
import os
import sys
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from common import (
    load_config,
    get_pipeline_config,
    get_project_config,
    get_registry_mode,
    load_yaml,
    log,
    fail,
    default_servers_file,
)
from build import build_project
from snailjob_manager import clear_summary
from deploy import (
    Deployer, platform_db_path, runtime_config_dir,
)


def _resolve_source_db(args) -> bool:
    """返回 True 表示走 db 模式：显式 db 恒为 True（由 _switch 明确报错）；auto 查 DB 存在该流水线"""
    if args.config_source == "yaml":
        return False
    if args.config_source == "db":
        return True
    import sqlite3
    db_path = platform_db_path()
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT project_id FROM pipelines WHERE name=? LIMIT 1",
                (args.pipeline,)).fetchone()
        finally:
            conn.close()
        return bool(row)
    except Exception:
        return False


def _switch_to_db_config(args) -> None:
    """db 模式：导出运行时 YAML 到实例 data/runtime/ 并覆盖 common 配置目录（按流水线查项目）"""
    from webapp.exporter import export_runtime
    db_path = platform_db_path()
    if not db_path.exists():
        fail(f"{db_path} 不存在，请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
    import sqlite3
    if args.project_id is None:
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT project_id FROM pipelines WHERE name=? LIMIT 1",
                    (args.pipeline,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            fail(f"{db_path} 不可用（{e}），请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
        if not row:
            fail(f"db 模式未找到流水线 {args.pipeline}，请先检查 Dashboard 配置")
        args.project_id = row[0]
    else:
        # 显式 --project-id：先校验项目存在，避免导出空配置后误导性报错
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM projects WHERE id=? LIMIT 1", (args.project_id,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            fail(f"{db_path} 不可用（{e}），请先通过 Web Dashboard 初始化配置（或使用 yaml 模式）")
        if not row:
            fail(f"db 模式未找到项目 ID {args.project_id}，请检查 Dashboard 配置")
    tmp = runtime_config_dir(args.project_id, args.env)
    try:
        export_runtime(str(db_path), args.project_id, args.env or "", tmp)
    except ValueError as e:
        fail(f"{e}（可使用 yaml 模式或先配置环境）")
    except sqlite3.Error as e:
        fail(f"{db_path} 读取失败（{e}），库文件可能损坏或未初始化（可使用 yaml 模式）")
    # 模块级变量覆盖：必须用 `import common`（与顶部 from common import ... 是同一模块实例）；
    # scripts/common 目录无 __init__.py，`import scripts.common` 会是第二个模块实例，覆盖无效
    import common as common_mod
    common_mod.CONFIG_DIR_OVERRIDE = tmp


def all_pipeline_services(pipeline_cfg: dict) -> list:
    """按出现顺序收集流水线中所有不重复的服务配置（含可能指定的版本）"""
    seen = set()
    result = []
    for stage in pipeline_cfg.get("stages", []):
        for svc in stage.get("services", []):
            name = svc["name"]
            if name not in seen:
                seen.add(name)
                result.append(svc)
    return result


def auto_fill_versions(pipeline_cfg: dict, config: dict, env: str = None):
    """--auto-version：为流水线中每个服务查询服务器当前版本，写入内存配置

    svc.version = 当前版本+1，svc.previous_version = 当前版本；
    服务级显式版本优先；容器不存在（首次发版）时报错要求手动配置。
    """
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


def build_services(service_specs: list, env: str = None):
    """顺序构建并推送指定服务的镜像

    service_specs: [(name, version), ...]，同一服务不同版本以第一次出现为准
    """
    seen = set()
    for name, version in service_specs:
        if name in seen:
            continue
        seen.add(name)
        log(f">>> 构建镜像: {name}:{version}")
        build_project(name, version, env=env)


def deploy_one_service(
    service_name: str,
    version: str,
    previous_version: str,
    servers_file: str,
    config: dict,
    skip_snailjob: bool,
    env: str = None,
    canary: bool = False,
) -> bool:
    """发布单个服务到指定服务器清单"""
    servers_path = Path(servers_file)
    if not servers_path.is_absolute():
        servers_path = Path(__file__).resolve().parent / servers_path
    servers = load_yaml(servers_path).get("servers", [])
    if not servers:
        log(f"[{service_name}] 服务器清单为空: {servers_file}")
        return False

    try:
        deployer = Deployer(service_name, version, previous_version, config, env=env, canary=canary)
        return deployer.run(
            servers,
            skip_build=True,
            skip_snailjob=skip_snailjob,
            clear_summary_flag=False,
        )
    except Exception as e:
        log(f"[{service_name}] 发布异常: {e}")
        return False


def rollback_one_service(
    service_name: str,
    version: str,
    previous_version: str,
    servers_file: str,
    config: dict,
    skip_snailjob: bool,
    env: str = None,
) -> bool:
    """全量回滚单个服务的所有服务器到 previous_version"""
    servers_path = Path(servers_file)
    if not servers_path.is_absolute():
        servers_path = Path(__file__).resolve().parent / servers_path
    servers = load_yaml(servers_path).get("servers", [])
    if not servers:
        log(f"[{service_name}] 服务器清单为空: {servers_file}")
        return False

    try:
        deployer = Deployer(service_name, version, previous_version, config, env=env)
        return deployer.rollback_all(servers, skip_snailjob=skip_snailjob)
    except Exception as e:
        log(f"[{service_name}] 回滚异常: {e}")
        return False


def rollback_stage(
    stage: dict,
    version: str,
    previous_version: str,
    default_servers_file: str,
    config: dict,
    skip_snailjob: bool,
    env: str = None,
) -> bool:
    """回滚一个 stage 中所有服务的所有服务器到 previous_version"""
    stage_name = stage.get("name", "unknown")
    services = stage.get("services", [])

    if not services:
        log(f"stage [{stage_name}] 没有服务，跳过")
        return True

    log(f"\n========== 回滚 stage [{stage_name}] ==========")

    results = []
    for svc in services:
        name = svc["name"]
        svc_version = svc.get("version", version)
        svc_previous = svc.get("previous_version", previous_version)
        servers_file = svc.get("servers_file") or default_servers_file
        ok = rollback_one_service(name, svc_version, svc_previous, servers_file, config, skip_snailjob, env)
        results.append((name, ok))

    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        status = "成功" if ok else "失败"
        log(f"[{name}] stage [{stage_name}] 回滚{status}")

    if all_ok:
        log(f"========== stage [{stage_name}] 全部回滚成功 ==========\n")
    else:
        log(f"========== stage [{stage_name}] 存在回滚失败，停止后续 stage ==========\n")

    return all_ok


def push_one_service(
    service_name: str,
    version: str,
    previous_version: str,
    servers_file: str,
    config: dict,
    env: str = None,
) -> bool:
    """推送单个服务的镜像到服务器（仅直传模式）"""
    servers_path = Path(servers_file)
    if not servers_path.is_absolute():
        servers_path = Path(__file__).resolve().parent / servers_path
    servers = load_yaml(servers_path).get("servers", [])
    if not servers:
        log(f"[{service_name}] 服务器清单为空: {servers_file}")
        return False

    try:
        deployer = Deployer(service_name, version, previous_version, config, env=env)
        return deployer.push_image_only(servers)
    except Exception as e:
        log(f"[{service_name}] 推送异常: {e}")
        return False


def push_stage(
    stage: dict,
    version: str,
    previous_version: str,
    default_servers_file: str,
    config: dict,
    env: str = None,
) -> bool:
    """推送一个 stage 中所有服务的镜像到服务器"""
    stage_name = stage.get("name", "unknown")
    services = stage.get("services", [])

    if not services:
        log(f"stage [{stage_name}] 没有服务，跳过")
        return True

    log(f"\n========== 推送镜像 stage [{stage_name}] ==========")

    results = []
    for svc in services:
        name = svc["name"]
        svc_version = svc.get("version", version)
        svc_previous = svc.get("previous_version", previous_version)
        servers_file = svc.get("servers_file") or default_servers_file
        ok = push_one_service(name, svc_version, svc_previous, servers_file, config, env)
        results.append((name, ok))

    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        status = "成功" if ok else "失败"
        log(f"[{name}] stage [{stage_name}] 推送{status}")
    return all_ok


def run_stage(
    stage: dict,
    version: str,
    previous_version: str,
    default_servers_file: str,
    config: dict,
    skip_build: bool,
    skip_snailjob: bool,
    env: str = None,
    canary: bool = False,
) -> bool:
    """执行一个 stage，返回是否全部成功"""
    stage_name = stage.get("name", "unknown")
    services = stage.get("services", [])
    parallel = stage.get("parallel", False)

    if not services:
        log(f"stage [{stage_name}] 没有服务，跳过")
        return True

    log(f"\n========== 进入 stage [{stage_name}] parallel={parallel} ==========")

    # 解析每个服务的版本号与服务器清单（支持服务级覆盖）。
    # 必须以元组携带 servers_file：循环变量在循环结束后仍指向最后一个服务，
    # 若在后续循环里再从 svc 取值，所有服务都会误用最后一台服务的清单。
    service_versions = []
    for svc in services:
        name = svc["name"]
        svc_version = svc.get("version", version)
        svc_previous = svc.get("previous_version", previous_version)
        servers_file = svc.get("servers_file") or default_servers_file
        service_versions.append((name, svc_version, svc_previous, servers_file))

    # 如果流水线不要求 build_first，且未跳过构建，则在本 stage 部署前顺序构建
    if not skip_build:
        build_services([(name, svc_version)
                        for name, svc_version, _, _ in service_versions], env=env)

    results = []

    if parallel:
        with ThreadPoolExecutor(max_workers=len(services)) as executor:
            futures = {}
            for name, svc_version, svc_previous, servers_file in service_versions:
                futures[
                    executor.submit(
                        deploy_one_service,
                        name,
                        svc_version,
                        svc_previous,
                        servers_file,
                        config,
                        skip_snailjob,
                        env,
                        canary,
                    )
                ] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    ok = future.result()
                except Exception as e:
                    log(f"[{name}] stage [{stage_name}] 执行异常: {e}")
                    ok = False
                results.append((name, ok))
    else:
        for name, svc_version, svc_previous, servers_file in service_versions:
            ok = deploy_one_service(
                name, svc_version, svc_previous, servers_file, config, skip_snailjob, env, canary
            )
            results.append((name, ok))

    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        status = "成功" if ok else "失败"
        log(f"[{name}] stage [{stage_name}] {status}")

    if all_ok:
        log(f"========== stage [{stage_name}] 全部成功 ==========\n")
    else:
        log(f"========== stage [{stage_name}] 存在失败，停止后续 stage ==========\n")

    return all_ok


def main():
    parser = argparse.ArgumentParser(description="智能运维平台多服务流水线发布")
    parser.add_argument("--pipeline", required=True, help="流水线名称，例如 xxx")
    parser.add_argument("--version", help="全局默认新版本号（可被 pipelines.yaml 中服务的 version 覆盖）")
    parser.add_argument("--previous-version", help="全局默认回滚版本号（可被 pipelines.yaml 中服务的 previous_version 覆盖）；首次发版可不传，失败时跳过容器回滚")
    parser.add_argument("--env", help="目标环境，例如 prod/test，会读取 deploy.{env}.yaml 和 servers.{env}.yaml")
    parser.add_argument(
        "--servers-file",
        default=None,
        help="默认服务器清单文件（覆盖 --env 的默认选择）",
    )
    parser.add_argument("--config-source", choices=["auto", "yaml", "db"], default="auto",
                        help="配置来源：auto=DB 存在该流水线则用 db 否则 yaml；yaml=旧 config/；db=SQLite（需 --project-id）")
    parser.add_argument("--project-id", type=int, default=None,
                        help="--config-source db 时的项目 ID；--pipeline 传该项目内流水线名")
    parser.add_argument("--skip-build", action="store_true", help="跳过 Maven 构建和镜像推送")
    parser.add_argument("--skip-snailjob", action="store_true", help="跳过 SnailJob 暂停/恢复")
    parser.add_argument("--clear-summary", action="store_true", help="全部完成后清零 sj_job_summary")
    parser.add_argument("--auto-version", action="store_true", help="自动查询各服务服务器当前版本作为 previous-version，新版本=当前版本 patch+1")
    parser.add_argument("--canary", action="store_true", help="金丝雀模式：每个服务首台发布后暂停等待人工确认")
    parser.add_argument("--rollback", action="store_true", help="全量回滚模式：将流水线中所有服务的所有服务器回滚到 --previous-version")
    parser.add_argument("--build-only", action="store_true", help="仅构建所有服务镜像，不推送、不发布")
    parser.add_argument("--push-only", action="store_true", help="构建并推送所有服务镜像到服务器，不发布")
    args = parser.parse_args()

    # db 模式切换：必须在互斥校验与 load_config 之前（保证 common 全部走覆盖目录）
    if _resolve_source_db(args):
        _switch_to_db_config(args)

    if args.build_only and args.push_only:
        parser.error("--build-only 和 --push-only 不能同时使用")
    if args.rollback and (args.build_only or args.push_only):
        parser.error("--rollback 不能与 --build-only/--push-only 同时使用")
    if args.rollback and not args.previous_version:
        parser.error("--rollback 必须指定 --previous-version（目标回滚版本）")
    if args.auto_version and (args.rollback or args.build_only or args.push_only):
        parser.error("--auto-version 只能用于普通发布模式，不能与 --rollback/--build-only/--push-only 同时使用")

    if args.env:
        os.environ["PLATFORM_DEPLOY_ENV"] = args.env

    config = load_config(env=args.env)
    pipeline_cfg = get_pipeline_config(args.pipeline)
    default_servers = default_servers_file(env=args.env)

    if args.auto_version:
        auto_fill_versions(pipeline_cfg, config, env=args.env)

    # 启动前确认环境信息
    log(f"目标环境: {args.env or '默认/生产'}")
    nacos_conf = config.get("nacos") or {}
    log(f"Nacos 地址: {nacos_conf.get('url') or '未配置（跳过 Nacos 操作）'}，"
        f"命名空间: {nacos_conf.get('namespace') or 'public'}")

    # 校验每个服务都有版本号：服务级 > 全局；previous_version 可选（首次发版无回滚版本）
    missing_version = []
    no_prev = []
    for svc in all_pipeline_services(pipeline_cfg):
        if not svc.get("version") and not args.version:
            missing_version.append(svc["name"])
        if not svc.get("previous_version") and not args.previous_version:
            no_prev.append(svc["name"])
    if missing_version:
        fail(f"以下服务未指定 version，且未提供 --version：{', '.join(missing_version)}")
    if no_prev:
        log(f"以下服务未指定 previous_version（首次发版场景）：{', '.join(no_prev)}，失败时将跳过容器回滚")

    # --build-only: 仅构建所有服务镜像
    if args.build_only:
        all_services = all_pipeline_services(pipeline_cfg)
        log(f">>> 仅构建模式：构建 {len(all_services)} 个服务的镜像")
        build_services(
            [(svc["name"], svc.get("version", args.version)) for svc in all_services],
            env=args.env,
        )
        log("构建完成")
        sys.exit(0)

    # --push-only: 构建 + 推送所有服务镜像
    if args.push_only:
        all_services = all_pipeline_services(pipeline_cfg)
        log(f">>> 构建并推送模式：构建 {len(all_services)} 个服务的镜像")
        build_services(
            [(svc["name"], svc.get("version", args.version)) for svc in all_services],
            env=args.env,
        )
        mode = get_registry_mode(config)
        if mode != "direct":
            log(f"当前为仓库模式（{mode}），构建阶段已将镜像推送到 registry，无需额外推送")
        else:
            log(">>> 直传模式：推送镜像到各服务器")
            pipeline_success = True
            for stage in pipeline_cfg.get("stages", []):
                ok = push_stage(
                    stage, args.version, args.previous_version,
                    args.servers_file or default_servers, config, env=args.env,
                )
                if not ok:
                    pipeline_success = False
                    break
            if pipeline_success:
                log("镜像推送完成")
            else:
                fail("部分服务镜像推送失败")
        sys.exit(0)

    # 若配置 build_first，先统一构建所有涉及的镜像（支持每个服务不同版本）
    if pipeline_cfg.get("build_first", True) and not args.skip_build:
        all_services = all_pipeline_services(pipeline_cfg)
        log(f">>> 流水线要求 build_first，先统一构建 {len(all_services)} 个服务")
        build_services(
            [(svc["name"], svc.get("version", args.version)) for svc in all_services],
            env=args.env,
        )

    # 按顺序执行 stage（回滚模式逆序）
    stages = pipeline_cfg.get("stages", [])
    if args.rollback:
        stages = list(reversed(stages))
        log(">>> 全量回滚模式：按 stage 逆序回滚")

    pipeline_success = True
    from notify import send_notify
    send_notify(config, "start", project=args.pipeline, version=args.version,
                previous_version=args.previous_version, env=args.env)
    start_ts = time.time()
    for stage in stages:
        if args.rollback:
            ok = rollback_stage(
                stage,
                args.version,
                args.previous_version,
                args.servers_file or default_servers,
                config,
                args.skip_snailjob,
                env=args.env,
            )
        else:
            ok = run_stage(
                stage,
                args.version,
                args.previous_version,
                args.servers_file or default_servers,
                config,
                args.skip_build,
                args.skip_snailjob,
                env=args.env,
                canary=args.canary,
            )
        if not ok:
            pipeline_success = False
            break

    if args.clear_summary:
        log(">>> 流水线结束，清零 sj_job_summary 计数器")
        clear_summary(env=args.env)

    if pipeline_success:
        log("流水线全部完成")
        send_notify(config, "success", project=args.pipeline, version=args.version,
                    previous_version=args.previous_version, env=args.env,
                    elapsed=f"{time.time() - start_ts:.0f}s")
    else:
        send_notify(config, "failed", project=args.pipeline, version=args.version,
                    previous_version=args.previous_version, env=args.env,
                    elapsed=f"{time.time() - start_ts:.0f}s", detail="流水线发布中断")
        fail("流水线发布中断")


if __name__ == "__main__":
    main()
