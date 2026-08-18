"""Maven 多模块构建 + Docker 镜像构建推送"""
import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path
from common import load_config, get_project_config, get_registry_auth, get_registry_mode, log, fail


def find_mvn(config: dict = None) -> str:
    """跨平台定位 mvn 可执行文件（绝对路径）

    查找顺序（三级，均未命中才报错）：
      1. 配置文件 build.maven_home（显式指定，最高优先级）
      2. 环境变量 MAVEN_HOME/M2_HOME，或 PATH 中的 mvn
      3. Windows 常见安装位置（IDEA 自带 Maven、Chocolatey 等）兜底
    Windows 上 subprocess 无法直接执行裸命令 "mvn"（CreateProcess 只认 .exe/.cmd），
    必须返回绝对路径。
    """
    # 1. 配置文件显式指定
    build_cfg = (config or {}).get("build", {})
    maven_home = build_cfg.get("maven_home") or ""
    if maven_home:
        exe = Path(maven_home).expanduser() / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
        if exe.exists():
            return str(exe)
        fail(f"配置的 maven_home 无效（不存在 mvn）：{maven_home}")
    # 2. 环境变量 / PATH
    for var in ("MAVEN_HOME", "M2_HOME"):
        home = os.environ.get(var)
        if home:
            exe = Path(home).expanduser() / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
            if exe.exists():
                return str(exe)
    found = shutil.which("mvn")
    if found:
        return found
    # 3. Windows 常见安装位置（IDEA 自带 Maven、Chocolatey 等）
    if os.name == "nt":
        candidates = [
            *Path(r"C:\Program Files\JetBrains").glob("IntelliJ IDEA*/plugins/maven-plugin/lib/maven3/bin/mvn.cmd"),
            Path(r"C:\ProgramData\chocolatey\bin\mvn.cmd"),
            Path(r"C:\Program Files\apache-maven*/bin/mvn.cmd"),
            Path(r"D:\apache-maven*/bin/mvn.cmd"),
        ]
        for cand in candidates:
            if cand.exists():
                return str(cand)
    fail("未找到 Maven：请在 config/deploy*.yaml 的 build.maven_home 配置，或设置 MAVEN_HOME 环境变量，或安装 Maven 并加入 PATH")


def find_java_home(config: dict = None) -> str:
    """定位 JDK 根目录（三级查找：配置文件 → 环境变量/PATH → 常见安装位置）"""
    import re
    # 1. 配置文件显式指定
    build_cfg = (config or {}).get("build", {})
    java_home = build_cfg.get("java_home") or ""
    if java_home:
        jh = Path(java_home).expanduser()
        if (jh / "bin" / "java.exe").exists():
            return str(jh)
        fail(f"配置的 java_home 无效（不存在 java.exe）：{java_home}")
    # 2. 环境变量 / PATH
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / "java.exe").exists():
        return java_home
    found = shutil.which("java")
    if found:
        return str(Path(found).resolve().parent.parent)
    # 3. Windows 常见安装位置（取版本号最高）
    if os.name == "nt":
        def ver(p: Path):
            m = re.search(r"(\d+)(?:\.(\d+))?", p.name)
            return tuple(int(x) for x in m.groups() if x is not None) if m else (0,)
        for base in (Path(r"C:\Program Files\Java")):
            for jdk in sorted(base.glob("jdk-*"), key=ver, reverse=True):
                if (jdk / "bin" / "java.exe").exists():
                    return str(jdk)
    fail("未找到 JDK：请在 config/deploy*.yaml 的 build.java_home 配置，或设置 JAVA_HOME 环境变量，或安装 JDK 并加入 PATH")


def run(cmd: list, cwd: Path, env: dict = None, timeout: int = None):
    log(f"执行: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        # 实时输出已经通过 PIPE 捕获，统一打印
        if result.stdout:
            for line in result.stdout.rstrip().splitlines():
                print(f"  | {line}", flush=True)
        if result.returncode != 0:
            fail(f"命令执行失败 (rc={result.returncode}): {' '.join(cmd)}")
    except subprocess.TimeoutExpired:
        fail(f"命令超时 ({timeout}s): {' '.join(cmd)}")
    except FileNotFoundError:
        fail(f"命令不存在: {cmd[0]}")


def docker_login(registry: dict):
    if not registry["username"]:
        log("未配置镜像仓库账号，跳过 docker login")
        return
    cmd = [
        "docker", "login", registry["host"],
        "-u", registry["username"],
        "--password-stdin",
    ]
    log(f"docker login {registry['host']}")
    result = subprocess.run(cmd, input=registry["password"].encode())
    if result.returncode != 0:
        fail("docker login 失败")


KNOWN_MAVEN_PROFILES = ("local", "dev", "test", "prod")


def get_maven_profile(config: dict, env: str = None) -> str:
    """确定 Maven 打包 profile（-P 参数），决定 jar 内固化的环境配置

    优先级：
    1. build.maven_profile 显式配置（deploy.{env}.yaml），支持自定义环境（如 qa）
    2. env 与已知 Maven profile 同名（local/dev/test/prod）时直接使用
    3. 未传 --env 默认 prod（deploy.yaml 即生产配置，保持历史行为）
    未知环境且未显式配置时直接报错，防止打出错误环境的镜像。
    """
    explicit = (config.get("build") or {}).get("maven_profile")
    if explicit:
        return explicit
    if env in KNOWN_MAVEN_PROFILES:
        return env
    if not env:
        return "prod"
    fail(
        f"环境 {env} 没有对应的 Maven profile（已知: {'/'.join(KNOWN_MAVEN_PROFILES)}），"
        f"请在 config/deploy.{env}.yaml 的 build.maven_profile 显式配置"
    )


def build_project(project_name: str, version: str, env: str = None):
    config = load_config(env=env)
    proj = get_project_config(project_name)
    registry = get_registry_auth(config)
    mode = get_registry_mode(config)
    # Maven profile 决定 jar 内固化的环境配置（spring.profiles.active / Nacos 命名空间）
    # 注意必须在下方 env = os.environ.copy() 覆盖参数前计算
    profile = get_maven_profile(config, env)
    project_root = Path(config["deploy"]["project_root"]).expanduser()
    if not project_root.exists():
        fail(f"项目根目录不存在: {project_root}")

    if mode == "push":
        docker_login(registry)
    else:
        log(f"直传模式（{mode}）: 跳过 docker login / docker push")

    # 定位 Maven 与 JDK（优先级：配置文件 build.* → 环境变量/PATH → 常见位置）
    mvn = find_mvn(config)
    java_home = find_java_home(config)
    env = os.environ.copy()
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", "")
    log(f"使用 Maven: {mvn}" + (f"，JAVA_HOME: {java_home}" if java_home else ""))
    log(f"Maven profile: {profile}（--env: {env or '默认/生产'}），jar 将固化该环境配置")

    # Maven 顺序构建（单步超时 10 分钟）
    for step in proj["build_order"]:
        module_path = project_root / step["path"]
        pom = module_path / "pom.xml"
        if not pom.exists():
            fail(f"未找到 pom.xml: {pom}")
        # 支持 extra_profile：与环境 profile 组合为 -P{env},{extra}
        # 用于同一仓库按 Maven profile 产出不同 jar 的项目（如 basketball 的 web/job/cli）
        extra = step.get("extra_profile")
        maven_profiles = f"{profile},{extra}" if extra else profile
        cmd = [
            mvn, "-f", str(pom), "clean", step["goal"],
            f"-P{maven_profiles}", f"-Drevision={version}", "-DskipTests",
        ]
        run(cmd, project_root, env=env, timeout=600)

    # Docker 构建（超时 10 分钟，--progress=plain 便于排查）
    # 直传模式使用 {namespace}/{image_name} 短名（与 IDEA 直推一致），便于服务器 docker load 后直接引用
    if mode == "push":
        image = f"{registry['host']}/{registry['namespace']}/{proj['image_name']}:{version}"
    else:
        ns = registry["namespace"]
        image = f"{ns}/{proj['image_name']}:{version}" if ns else f"{proj['image_name']}:{version}"
    dockerfile = project_root / proj["dockerfile"]
    context = project_root / proj["docker_context"]
    cmd = [
        "docker", "build",
        "--progress=plain",
        "-t", image,
        "-f", str(dockerfile),
        str(context),
    ]
    run(cmd, project_root, timeout=600)

    if mode == "push":
        # 推送镜像（超时 10 分钟，取决于上行带宽）
        cmd = ["docker", "push", image]
        run(cmd, project_root, timeout=600)
        log(f"镜像已推送: {image}")
    else:
        log(f"镜像已构建（未推送，部署时直传）: {image}")


def main():
    parser = argparse.ArgumentParser(description="构建并推送镜像")
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--env", help="目标环境，例如 prod/test")
    args = parser.parse_args()
    build_project(args.project, args.version, env=args.env)


if __name__ == "__main__":
    main()
