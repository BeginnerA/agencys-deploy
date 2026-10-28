"""Maven 多模块构建 / 前端构建 + Docker 镜像构建推送 / 静态站点 zip 打包

支持多种构建类型（由 projects.yaml 的 build_type 字段决定）：
  maven    - Java 微服务：Maven 多模块构建 → Docker 镜像（默认）
  frontend - Web 前端项目：自定义命令（npm install / npm run build 等）→ Docker 镜像
  go/rust  - 单二进制交叉编译（CGO_ENABLED=0 GOOS=linux / cargo --target），
             产物直接写 dist/{service}-{version}（含 .sha256 旁文件），零 Docker

发布方式 deploy_mode: static_archive 时构建尾部改为把产出目录打包为
 dist/{zip_prefix}-{version}.zip 供静态策略上传解压，跳过 Docker 构建。
"""
import os
import sys
import time
import shutil
import hashlib
import zipfile
import subprocess
import argparse
from pathlib import Path
from common import (load_config, get_project_config, get_registry_auth,
                    get_registry_mode, log, fail,
                    is_remote_git_url, mask_git_url, repo_local_path)


def git_prepare(proj: dict, project_root: Path) -> None:
    """构建前 Git 准备：远程 URL 自动 clone + 切换分支 + 拉取最新代码

    行为由 projects.yaml（或 DB services 表）的字段控制：
      git_repo              - 本地仓库相对路径，或 http(s) 远程 URL；URL 形态
                               首次构建自动 clone 到 project_root 下同名目录
                               （末段去 .git），此后按本地仓库复用
      git_branch            - 构建前 checkout 的分支/tag，留空则不切换
      git_pull_before_build - 为 true 时在 checkout 后执行 git pull

    checkout / pull 失败仅告警不阻断（本地可能已手动同步，与 git_tag 一致）；
    远程 URL clone 失败则 fail 阻断——clone 是该形态下源码的唯一来源，
    继续构建必然失败，快速失败报错更清晰。URL 内嵌凭证在日志中脱敏。
    """
    repo = (proj.get("git_repo") or "").strip()
    if not repo:
        return
    repo_path = repo_local_path(repo, project_root)
    if repo_path is None:
        log(f"警告: 远程 URL 末段目录名不合法（须字母数字 _ -），跳过: {mask_git_url(repo)}")
        return
    if is_remote_git_url(repo):
        # 远程 URL：本地无仓库时自动 clone，已 clone 过则复用本地
        if not (repo_path / ".git").exists():
            log(f"远程仓库首次构建，自动 clone: {mask_git_url(repo)} → {repo_path}")
            try:
                result = subprocess.run(
                    ["git", "clone", repo, str(repo_path)],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode != 0:
                    fail(f"git clone 失败: {mask_git_url(repo)}: {result.stderr.strip()}")
                log(f"git clone 完成: {repo_path.name}")
            except subprocess.TimeoutExpired:
                fail(f"git clone 超时（600 秒）: {mask_git_url(repo)}")
            except FileNotFoundError:
                fail("git 命令不存在，无法 clone 远程仓库")
    elif not (repo_path / ".git").exists():
        log(f"警告: {repo_path} 不是 Git 仓库，跳过构建前 Git 操作")
        return

    branch = (proj.get("git_branch") or "").strip()
    pull = bool(proj.get("git_pull_before_build"))

    if not branch and not pull:
        return  # 两个都没配置，直接跳过（远程 URL 已 clone/复用完成）

    log(f"构建前 Git 准备: {mask_git_url(repo) if is_remote_git_url(repo) else repo}"
        f"（branch={branch or '当前'}, pull={pull}）")

    # 1. 切换分支/tag
    if branch:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "checkout", branch],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                log(f"警告: git checkout {branch} 失败（不阻断构建）: {result.stderr.strip()}")
                return  # checkout 失败就不继续 pull 了
            log(f"git checkout {branch} 完成")
        except subprocess.TimeoutExpired:
            log(f"警告: git checkout {branch} 超时，跳过")
            return
        except FileNotFoundError:
            log("警告: git 命令不存在，跳过构建前 Git 操作")
            return

    # 2. 拉取最新代码
    if pull:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log(f"警告: git pull 失败（不阻断构建）: {result.stderr.strip()}")
            else:
                summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "Already up to date."
                log(f"git pull 完成: {summary}")
        except subprocess.TimeoutExpired:
            log("警告: git pull 超时，跳过")
        except FileNotFoundError:
            log("警告: git 命令不存在，跳过 git pull")


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
            # 路径带 * 时必须用 glob（Path 字面量不会展开通配符，直接 exists 恒为 False）
            *Path(r"C:\Program Files").glob("apache-maven*/bin/mvn.cmd"),
            *Path("D:\\").glob("apache-maven*/bin/mvn.cmd"),
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


def find_node():
    """定位 node/npm/npx 可执行文件（仅 Windows 绝对路径兜底）

    前端构建需要 node 与包管理器在 PATH 中可用；
    Windows 上 subprocess 无法直接执行裸命令，须返回绝对路径。
    """
    node = shutil.which("node")
    if node:
        return node
    if os.name == "nt":
        for cand in [
            Path(r"C:\Program Files\nodejs\node.exe"),
            Path(r"C:\Program Files (x86)\nodejs\node.exe"),
            *Path(r"C:\Program Files\nodejs").glob("node.exe"),
        ]:
            if cand.exists():
                return str(cand)
    fail("未找到 node：请安装 Node.js 并加入 PATH")


def find_pm(project_root: Path, step_path: Path) -> str:
    """探测前端包管理器：项目目录下有 pnpm-lock → pnpm，有 yarn.lock → yarn，否则 npm

    返回绝对路径（Windows 兼容）。
    """
    for pm_name, lock_file in [("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock")]:
        if (step_path / lock_file).exists():
            pm = shutil.which(pm_name)
            if pm:
                return pm
            fail(f"项目使用 {pm_name}（检测到 {lock_file}），但未找到 {pm_name} 命令，请安装后加入 PATH")
    # 默认 npm
    npm = shutil.which("npm")
    if npm:
        return npm
    if os.name == "nt":
        npm_cmd = Path(r"C:\Program Files\nodejs\npm.cmd")
        if npm_cmd.exists():
            return str(npm_cmd)
    fail("未找到 npm：请安装 Node.js 并加入 PATH")


def is_clean_failure(output: str) -> bool:
    """判断 Maven 失败是否为 clean 阶段无法删除构建目录

    Windows 下 target 目录被其他进程（IDE 编译、杀毒扫描等）瞬时占用时，
    maven-clean-plugin 会报 "Failed to clean project: Failed to delete ..."，
    这种失败通常重试即可自愈，需要与编译错误等确定性失败区分。
    """
    return bool(output) and "Failed to clean project" in output and "maven-clean-plugin" in output


def is_transient_io_failure(output: str) -> bool:
    """判断 Maven 失败是否为可自愈的瞬时 I/O 错误（重试即可，区别于确定性失败）

    三类场景（Windows 下常见）：
    1. clean 阶段无法删除构建目录：target 被 IDE 编译/杀毒扫描等瞬时占用
    2. 编译阶段读取生成文件失败：IDE 语言服务器（如 Java 扩展增量编译）与 Maven
       并发写同一个 target/generated-sources 时，javac 读到的生成文件正被并发
       删除/重写/锁定，报 "error reading file"（中文环境 "读取...时出错"）
    3. spring-boot repackage 阶段读取 jar 失败：大 jar 在打包瞬间被杀毒扫描/
       其他进程锁定，插件读到的 jar 结构损坏，报 "Index N out of bounds for
       length 0" 类索引越界（失败后 target 常残留 22 字节空 jar）
    """
    if not output:
        return False
    if is_clean_failure(output):
        return True
    if "error reading file" in output or ("读取" in output and "时出错" in output):
        return True
    return "repackage failed" in output and "out of bounds for length" in output


def run(cmd: list, cwd: Path, env: dict = None, timeout: int = None,
        retry_on_clean_failure: bool = False,
        max_retries: int = 2, retry_delay: int = 3):
    """执行命令并实时打印输出

    retry_on_clean_failure: 当失败为瞬时 I/O 错误（Maven clean 删除目录失败、
    编译读取生成文件失败，Windows 下文件被 IDE/杀毒等进程占用）时自动重试，
    间隔 retry_delay 秒，最多 max_retries 次；仍失败或失败原因不是瞬时
    I/O 错误时按原逻辑报错。
    """
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
        if result.returncode != 0 and retry_on_clean_failure and is_transient_io_failure(result.stdout or ""):
            for attempt in range(1, max_retries + 1):
                log(f"构建目录/生成文件可能被其他进程占用（IDE 编译、杀毒扫描等），{retry_delay} 秒后自动重试（第 {attempt}/{max_retries} 次）")
                time.sleep(retry_delay)
                result = subprocess.run(
                    cmd, cwd=cwd, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=timeout,
                )
                if result.stdout:
                    for line in result.stdout.rstrip().splitlines():
                        print(f"  | {line}", flush=True)
                if result.returncode == 0:
                    return
                if not is_transient_io_failure(result.stdout or ""):
                    break
        if result.returncode != 0:
            if is_transient_io_failure(result.stdout or ""):
                log("提示: 若本机 IDE 打开了该项目且正在编译，请关闭项目窗口后重试发布")
            fail(f"命令执行失败 (rc={result.returncode}): {' '.join(cmd)}")
    except subprocess.TimeoutExpired:
        fail(f"命令超时 ({timeout}s): {' '.join(cmd)}")
    except FileNotFoundError:
        fail(f"命令不存在: {cmd[0]}")


def run_shell(cmd: str, cwd: Path, env: dict = None, timeout: int = None):
    """通过 shell 执行自定义命令（前端构建步骤的 commands 列表）

    支持 shell 语法（管道、重定向等）；Windows 使用 cmd /c，Unix 使用 /bin/sh -c。
    """
    log(f"执行: {cmd}")
    shell_cmd = cmd if os.name != "nt" else f'cmd /c "{cmd}"'
    try:
        result = subprocess.run(
            shell_cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, shell=True, timeout=timeout,
        )
        if result.stdout:
            for line in result.stdout.rstrip().splitlines():
                print(f"  | {line}", flush=True)
        if result.returncode != 0:
            fail(f"命令执行失败 (rc={result.returncode}): {cmd}")
    except subprocess.TimeoutExpired:
        fail(f"命令超时 ({timeout}s): {cmd}")


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


def _step_env(env_vars: dict, step: dict) -> dict:
    """步骤级环境变量：build_order 步骤可声明 "env": {KEY: VAL}（JSON 自由字段，
    无需改表），仅对本步骤命令生效（副本叠加，不污染全局 env_vars）。
    典型场景：Vue CLI4/webpack4 在 Node17+ 需 NODE_OPTIONS=--openssl-legacy-provider，
    写在服务配置里才能从任意触发入口（CLI/Web UI/定时计划/Agent）一致生效。"""
    step_env = dict(env_vars)
    for k, v in (step.get("env") or {}).items():
        step_env[str(k)] = str(v)
    return step_env


def _run_maven_steps(proj: dict, project_root: Path, version: str,
                     profile: str, env_vars: dict, config: dict = None):
    """Maven 多模块构建（原有逻辑）"""
    mvn = find_mvn(config)
    java_home = find_java_home(config)
    if java_home:
        env_vars["JAVA_HOME"] = java_home
        env_vars["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env_vars.get("PATH", "")
    log(f"使用 Maven: {mvn}" + (f"，JAVA_HOME: {java_home}" if java_home else ""))
    log(f"Maven profile: {profile}（jar 将固化该环境配置）")

    for step in proj["build_order"]:
        module_path = project_root / step["path"]
        pom = module_path / "pom.xml"
        if not pom.exists():
            fail(f"未找到 pom.xml: {pom}")
        extra = step.get("extra_profile")
        maven_profiles = f"{profile},{extra}" if extra else profile
        cmd = [
            mvn, "-f", str(pom), "clean", step["goal"],
            f"-P{maven_profiles}", f"-Drevision={version}", "-DskipTests",
        ]
        # Windows 下 target 可能被 IDE 增量编译/杀毒等瞬时占用导致 clean 失败或
        # 读取生成文件失败，自动重试自愈（间隔 5 秒 × 3 次，给足 IDE 编译窗口）
        run(cmd, project_root, env=_step_env(env_vars, step), timeout=600,
            retry_on_clean_failure=True, max_retries=3, retry_delay=5)


def _run_frontend_steps(proj: dict, project_root: Path, version: str,
                        env_vars: dict):
    """前端构建：按 build_order 中每个步骤的 commands 列表顺序执行

    commands 中的命令支持 shell 语法（管道、重定向等）。
    特殊变量替换：${VERSION} → 当前版本号。
    """
    node = find_node()
    log(f"使用 Node: {node}")
    log(f"构建类型: frontend（Web 前端项目）")

    for step in proj.get("build_order", []):
        step_name = step.get("name", "未命名步骤")
        step_path = project_root / step.get("path", ".")
        commands = step.get("commands", [])
        if not commands:
            log(f"警告: 步骤 [{step_name}] 无 commands，跳过")
            continue
        log(f">>> 前端步骤: {step_name}（{step_path}）")
        step_env = _step_env(env_vars, step)
        for cmd_template in commands:
            # 支持 ${VERSION} 变量替换
            cmd = cmd_template.replace("${VERSION}", version)
            # 如果命令以 npm/pnpm/yarn 开头，尝试定位绝对路径
            parts = cmd.split(None, 1)
            if parts and parts[0] in ("npm", "pnpm", "yarn"):
                pm = shutil.which(parts[0])
                if pm:
                    # run_shell 用 cmd /c "{cmd}" 包裹：路径含空格时（如 nvm/IDEA 自带
                    # Node 装在带空格目录）必须给可执行文件再加一层引号，凑齐 cmd 的多引号
                    # 保留规则，否则 cmd 剥掉外层引号后按空格截断报 "不是内部或外部命令"
                    if " " in pm:
                        pm = f'"{pm}"'
                    cmd = pm + (" " + parts[1] if len(parts) > 1 else "")
            run_shell(cmd, step_path, env=step_env, timeout=600)


def _make_static_zip(proj: dict, project_root: Path, version: str, config: dict):
    """静态发布打包：构建产出目录 → dist/{zip_prefix}-{version}.zip（文件直接置于包根）

    zip 名与 deploy_strategies.static_archive 的约定一致；dist 目录解析规则与
    Deployer.__init__ 相同（deploy.dist_dir 相对路径时基于仓库根）。
    """
    src = project_root / (proj.get("static_dist_dir") or "dist")
    if not src.exists():
        fail(f"静态构建产出目录不存在: {src}（检查 static_dist_dir 配置与构建命令产出）")
    prefix = proj.get("zip_prefix") or proj.get("service_name") or proj.get("image_name") or "site"
    dist_dir = Path(config["deploy"].get("dist_dir", "dist"))
    if not dist_dir.is_absolute():
        dist_dir = Path(__file__).resolve().parent.parent / dist_dir
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / f"{prefix}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    log(f"静态包已生成: {zip_path}（{zip_path.stat().st_size / 1024 / 1024:.1f} MB）")


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


def build_project(project_name: str, version: str, env: str = None):
    config = load_config(env=env)
    proj = get_project_config(project_name)
    registry = get_registry_auth(config)
    mode = get_registry_mode(config)
    build_type = proj.get("build_type", "maven")
    deploy_mode = proj.get("deploy_mode") or "container"
    artifact = {"static_archive": "archive", "jar_service": "jar",
                "go_binary": "binary", "rust_binary": "binary",
                "node_process": "package", "python_process": "package"
                }.get(deploy_mode, "image")
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
    if artifact in ("binary", "package"):
        return  # 产物已在对应 _run_*_steps 内生成（含 .sha256），无后续打包

    # Docker 构建（超时 10 分钟，--progress=plain 便于排查）
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
