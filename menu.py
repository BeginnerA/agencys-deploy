#!/usr/bin/env python3
"""交互式发布菜单：选择环境 → 发布方式 → 项目/流水线 → 自动版本 → 执行"""
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from common import load_projects, load_pipelines, load_config, log
from deploy import get_current_remote_version, bump_patch_version


def choose(prompt: str, options: list, allow_empty: bool = False) -> str:
    """列出选项并返回用户选择的 key；options: [(key, desc), ...]"""
    print(f"\n{prompt}")
    for i, (key, desc) in enumerate(options, 1):
        print(f"  [{i}] {key} - {desc}")
    while True:
        try:
            raw = input(f">>> 请输入编号 (1-{len(options)})" + ("，回车=默认1" if allow_empty else "") + ": ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(1)
        if allow_empty and raw == "":
            return options[0][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("无效输入，请重新选择")


def main():
    deploy_root = Path(__file__).resolve().parent

    # 1. 环境
    envs = [("", "生产（prod）")]
    for p in sorted(deploy_root.glob("config/servers.*.yaml")):
        env_name = p.stem.split(".")[1]
        envs.append((env_name, f"环境 {env_name}"))
    env = choose("请选择目标环境:", envs, allow_empty=True)
    if env:
        os.environ["PLATFORM_DEPLOY_ENV"] = env

    # 2. 发布方式
    mode = choose("请选择发布方式:", [("service", "单服务发布"), ("pipeline", "流水线发布")])

    # 3. 项目或流水线
    if mode == "service":
        projects = load_projects()["projects"]
        options = [(name, proj.get("service_name", "")) for name, proj in projects.items()]
        project = choose("请选择要发布的项目:", options)
        deploy_cmd = ["python", str(deploy_root / "deploy.py"), "--project", project]
        project_name = project
    else:
        pipelines = load_pipelines()["pipelines"]
        options = [(name, f"{len(p.get('stages', []))} 个 stage") for name, p in pipelines.items()]
        pipeline = choose("请选择要发布的流水线:", options)
        deploy_cmd = ["python", str(deploy_root / "deploy_pipeline.py"), "--pipeline", pipeline]
        project_name = None

    # 4. 自动查询服务器当前版本（单服务；流水线首次发版场景较多，跳过查询由用户手动输入）
    version = previous_version = None
    if mode == "service":
        try:
            config = load_config(env=env)
            current = get_current_remote_version(project_name, config, env=env)
            if current is None:
                log("服务器上未发现运行中的容器（首次发版），请手动输入版本号")
            else:
                suggested = bump_patch_version(current)
                print(f"\n当前线上版本: {current}，建议新版本: {suggested}")
                raw = input(">>> 新版本号（回车使用建议值）: ").strip()
                version = raw or suggested
                previous_version = current
        except Exception as e:
            log(f"自动查询版本失败（可手动输入）: {e}")
    if version is None:
        version = input(">>> 新版本号 (x.y.z): ").strip()
        previous_version = input(">>> 回滚版本号（首次发版直接回车）: ").strip() or None

    # 5. 确认执行
    deploy_cmd += ["--version", version]
    if env:
        deploy_cmd += ["--env", env]
    if previous_version:
        deploy_cmd += ["--previous-version", previous_version]
    print(f"\n{'=' * 60}")
    print(f"即将执行: {' '.join(deploy_cmd)}")
    print(f"{'=' * 60}")
    confirm = input(">>> 确认执行? (y/N): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消")
        return 0
    print()
    return subprocess.call(deploy_cmd)


if __name__ == "__main__":
    sys.exit(main())
