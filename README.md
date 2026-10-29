# 智能运维平台一键发布自动化脚本

这套脚本把原先 IDEA 点击打包 → 推送镜像 → Nacos 下线 → 1Panel 停删容器 → 改 docker-compose → 启动 → 调权重 → 手动 scp 镜像 的流程，改成一条命令完成的多服务器滚动发布。

## 适合替代的人工操作

- IDEA 里逐个点 Maven 生命周期
- 手动改 Dockerfile 运行配置的镜像版本号
- Nacos 页面逐个点服务下线/上线、改权重
- 1Panel 里停容器、删容器、看日志
- SSH 里手动改 docker-compose.yml 版本号
- 跨服务器导出/导入镜像 tar 包
- 足球/篮球项目发布前手动改 sj_job.job_status
- 巡检时手动把 sj_job_summary 计数器清零

## 整体流程

```
本地触发 deploy.sh
    │
    ├── 1. Maven 顺序构建（xxx → xxx → 目标项目）
    ├── 2. Docker 镜像构建并推送（push 模式推到仓库；direct 模式留本地待直传）
    ├── 3. （定时任务项目）备份并暂停 SnailJob
    ├── 4. 逐台服务器滚动发布
    │       a. 确保新镜像可用（push 模式 docker pull；direct 模式 save/scp/load）
    │       b. Nacos 下线该 IP 对应的所有服务实例
    │       c. 等待实例消失
    │       d. SSH 更新 docker-compose.yml 镜像版本
    │       e. docker compose up -d 启动
    │       f. HTTP（或容器状态）+ 日志关键字健康检查
    │       g. 失败则自动回滚到 --previous-version
    │       h. Nacos 上线并恢复权重
    ├── 5. （定时任务项目）恢复 SnailJob
    └── 6. 可选清零 sj_job_summary 计数器
```

## 前置条件

1. 一台控制机（可以是 Jenkins agent、WSL、macOS、Linux 或 Windows 本机），已安装：
   - Python 3.9+
   - Maven
   - Docker（能登录并推送到镜像仓库）
   - 能免密 SSH 到所有目标服务器
2. 所有目标服务器：
   - push 模式：已配置镜像仓库认证（`docker login`）
   - direct 模式：无需镜像仓库，仅需能 SSH、能执行 docker 命令
   - 有统一的 docker-compose.yml 路径（默认 `/data/docker/docker-compose.yml`）
   - 能访问 Nacos、MySQL（direct 模式无需访问镜像仓库）
3. 获取以下信息并填入配置文件：
   - 镜像仓库地址、命名空间、账号密码
   - Nacos 地址、命名空间、分组、账号密码
   - MySQL xxx 地址、账号密码
   - 目标服务器 IP 列表
   - SSH 登录用户和私钥路径
   - 各项目的 Dockerfile/端口/健康检查地址

## 快速开始

### 1. 安装 Python 依赖

```bash
cd agencys-deploy
pip install -r requirements.txt        # 运行依赖
pip install -r requirements-dev.txt    # 开发/CI 依赖（pytest、ruff），可选
```

### 2. 填写配置

编辑以下文件，把示例值换成真实值：

- `config/deploy.yaml`：镜像仓库、Nacos、MySQL、SSH 等全局信息
- `config/servers.yaml`：目标服务器 IP 列表
- `config/projects.yaml`：各项目的构建顺序、Dockerfile 路径、端口、SnailJob group 等

敏感信息建议通过环境变量传入，避免写在配置文件里：

```bash
export REGISTRY_USERNAME=your_name
export REGISTRY_PASSWORD=your_pass
export NACOS_USERNAME=your_name
export NACOS_PASSWORD=your_pass
export MYSQL_USERNAME=your_name
export MYSQL_PASSWORD=your_pass
```

### 3. 试运行（不实际构建，先检查 Nacos/SSH 连通性）

```bash
# 查看 Nacos 中服务名称的实例
python scripts/nacos_manager.py list --service xxx

# 单独测试目标服务器的 SSH 连接（在 deploy.py 里会自动做）
python scripts/health_check.py --host 192.168.1.1 --project xxx
```

### 4. 执行发布

Linux/macOS/WSL/Git Bash：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28
```

Windows PowerShell / cmd（参数与 deploy.sh 完全一致）：

```powershell
# PowerShell 推荐入口（依赖预检 + 参数透传）
.\deploy.ps1 --project xxx --version 1.0.29 --previous-version 1.0.28 --env test

# cmd / 双击入口（不受 PowerShell 执行策略限制）
deploy.bat --project xxx --version 1.0.29 --previous-version 1.0.28 --env test
```

> 注意：不要在 PowerShell 里直接执行 `./deploy.sh`，PowerShell 无法执行 `.sh`
> 文件，会按 Windows 文件关联交给 Git Bash 新窗口运行，当前终端看不到任何输出（"没反应"）。

如果镜像已经构建好，只想跑滚动发布：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --skip-build
```

足球/篮球项目会自动按 projects.yaml 里的 `snailjob_group` 暂停和恢复定时任务：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --clear-summary
```

SnailJob 暂停前会先备份 `sj_job` 记录（SQL 文件 + 任务状态快照，存放在 `backups/` 目录）；
恢复时按备份快照**精确恢复**：只把备份时运行（`job_status=1`）的任务恢复为运行，
备份前就暂停（`job_status=0`）的任务保持暂停不变。

### 5. 浏览器操作台（Web Dashboard · 智能运维平台）

启动 Web 页面，在浏览器里完成**配置管理 + 发布 + 监控**（大屏一体式，支持多项目）：

```powershell
pip install -r requirements.txt
python webapp/app.py          # 默认 http://127.0.0.1:5678
```

- 首次启动自动导入 `config/` 旧配置（原 11 个服务 + 4 条流水线 + prod/test 环境），创建默认管理员 `admin/admin`（首次登录强制改密）。
- 大屏主页：服务状态卡片墙（在线/异常/发布中/未部署 + 镜像版本 + Nacos 实例数）、发布趋势/成功率图表、实时任务日志；点击卡片直接发布/回滚。
- 「管理」抽屉（admin）：项目管理、服务/环境/服务器/流水线 CRUD、用户与角色、YAML 导入导出。
- 实时监控：周期默认读取 `DASHBOARD_MONITOR_INTERVAL`（30s），可用 `--monitor-interval N` 覆盖；`0` 仅关闭实时探测与告警引擎，发布调度、日志采集、SSL 到期探测、AI 建议调度仍独立运行。`jar_service` 额外探测 systemd 单元（进程存活、版本、PID、运行时长、重启数、CPU/RSS）并采集 `journalctl` 日志；设备页可查看每主机版本视图。
- 后台服务周期独立：发布调度 `--deploy-interval`（默认 30s，或 `DASHBOARD_DEPLOY_INTERVAL`）、AI 建议 `--agent-interval`（默认 30s，或 `DASHBOARD_AGENT_INTERVAL`，`0` 单独关闭）；监控关闭时日志采集回退为 120s 独立轮询。
- 页面数据实时推送（SSE 通道 `/api/stream`）：监控轮次完成、告警产生/状态变更、发布任务新建与状态流转、审批单/决策建议更新、服务日志采集轮次有新行时，后端逐帧通知前端，页面收到后去抖重取数据（所有列表页已改为事件驱动，无事件时不发请求）；通道异常自动退回原有定时轮询，顶栏指示灯显示「实时 / 连接中 / 降级轮询」，后台标签页自动释放连接并降频。例外：发布任务日志正文由子进程写文件（跨进程无法事件化），抽屉/日志中心仍保留 2s/3s 增量追尾。
- 交互无障碍：可点击的非原生元素（筛选 chip、侧栏导航项、任务名链接、搜索下拉项等）自动补 `tabindex/role/aria-label` 并支持 Enter/Space 触发，键盘焦点统一显形（`:focus-visible` 主色焦点环）；抽屉/弹窗/下拉面板/搜索面板共用 Esc 层栈（只关最顶一层），关闭后焦点归还触发元素，确认框初始焦点在「取消」；通知同屏上限 4 条、同文案合并计数、读屏可播报；系统开启「减弱动态效果」时全局停用动画与过渡。
- 色彩对比与表单可读性：弱化文本与亮色底上的文字统一按 WCAG AA 校正（亮色填充一律用深字 token `--on-bright`，渐变端点提亮），小字对比度达标；表单控件自动关联字段名（`label for`/`aria-label`/`aria-labelledby`，换肤下拉同步镜像到焦点元素）、必填项标 `aria-required`、提示与错误说明经 `aria-describedby` 关联、校验失败置 `aria-invalid` 且错误文本以 `role="alert"` 即时播报。
- 导航地标与表格语义：页面首部提供“跳到主内容” skip link（键盘 Tab 首站可见、点击直达，默认隐藏）；侧栏导航包为 `<nav aria-label=主导航>`、`<aside>`/`<header>`/`<main>`/`<footer>` 构成页地标；数据表列头自动补 `scope=col`（读屏可将单元格对上列头）、表名就近取面板标题/页签（无标题回退到页签名）。
- 配置存 `data/platform.db`（权威源）；`config/` 目录为导出产物 + CLI 兼容（`deploy.py --config-source yaml`）。
- 数据目录可整体外置：`python webapp/app.py --data-dir /srv/deploy-a --config-dir /srv/cfg-a [--log-dir /var/log/deploy-a]`，同一份代码即可跑多套互不干扰的实例（库、密钥、导出物、任务日志全部跟随 `--data-dir`）；不传参数时保持项目内 `data/`、`config/` 的默认落点。
- 角色：admin（全部）/ operator（发布回滚）/ viewer（只读）。生产部署建议 `--host 0.0.0.0` + 前置 Nginx 反代 + 环境变量注入 `DASHBOARD_SECRET_KEY`。

**首次发版**（服务器上没有旧版本）时，`--previous-version` 可以省略：

```bash
./deploy.sh --project xxx --version 1.0.0
```

首次发版时脚本不会尝试回滚容器（没有旧版本可回滚）；若发布失败，会恢复 Nacos 上线并保留新版本容器，提示人工排查。之后再次发版需补上 `--previous-version` 指向当前线上版本。

**金丝雀模式**（`--canary`）：首台发布成功后暂停，等待人工验证再决定是否继续：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --canary
```

首台发布完成后终端会提示三个选项：

- `y`：继续发布剩余服务器
- `n`：停止发布，首台保持新版本（默认，直接回车等同）
- `r`：回滚首台到 `--previous-version`（仅指定了回滚版本时可用）

流水线同样支持：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --canary
```

**全量回滚**（`--rollback`）：全部服务器发布成功后发现问题，一键将所有服务器回滚到旧版本：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --rollback
```

回滚流程与发布类似，但跳过构建，逐台执行：Nacos 下线 → 回滚容器到 `--previous-version` → 健康检查 → Nacos 上线。某台回滚失败不会中断后续服务器，最终汇总结果。`--previous-version` 为必填项。

流水线同样支持，且会按 stage 逆序回滚：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --rollback
```

**仅构建**（`--build-only`）：只执行 Maven 打包和 Docker 构建，不推送镜像到服务器、不发布：

```bash
./deploy.sh --project xxx --version 1.0.29 --build-only
```

**构建并推送**（`--push-only`）：构建 + 推送镜像到服务器（直传模式 docker save → scp → docker load），但不执行发布：

```bash
./deploy.sh --project xxx --version 1.0.29 --push-only
```

流水线同样支持：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --build-only
./pipeline.sh --pipeline xxx --version 1.0.29 --push-only
```

### 多环境与打包（--env ↔ Maven profile）

`--env` 同时控制「部署目标」和「打包环境配置」：

| `--env` | 读取的部署配置 | Maven 打包 profile（-P） |
|---------|---------------|------------------------|
| 不传 | `deploy.yaml` / `servers.yaml`（生产） | `-Pprod` |
| `prod` | `deploy.prod.yaml` / `servers.prod.yaml` | `-Pprod` |
| `test` | `deploy.test.yaml` / `servers.test.yaml` | `-Ptest` |
| `dev` | `deploy.dev.yaml` / `servers.dev.yaml` | `-Pdev` |

Maven profile 决定 jar 内固化的环境配置（`spring.profiles.active` 与 Nacos 命名空间，定义在父 POM `xxx/xxx/pom.xml` 的 profiles 中）。映射规则：

1. `deploy.{env}.yaml` 的 `build.maven_profile` 显式配置优先（支持自定义环境，如 qa）
2. 未配置时按 `--env` 自动推导：`test → -Ptest`、`dev → -Pdev`；不传 `--env` 默认 `-Pprod`
3. 未知环境且未显式配置时**直接报错**，防止打出错误环境的镜像

> 注意：`--skip-build` 时不会打包，profile 不生效；`--env test` 产出的镜像只包含测试环境配置（连测试 Nacos），不要推到生产服务器。

## 配置文件说明

### config/deploy.yaml

| 配置项 | 含义 |
| --- | --- |
| `registry.mode` | 镜像分发模式：`push`（默认，推送远程仓库）/ `direct`（scp 直传，无需仓库） |
| `registry.namespace` | 两种模式共用的镜像命名空间：push 模式为仓库项目空间（完整镜像名 `{host}/{namespace}/{image}:{version}`）；direct 模式为镜像名前缀（产出 `{namespace}/{image}:{version}`，与 IDEA 直推格式一致，如 `xxx/服务名称`）；留空则 direct 不带前缀 |
| `registry.host` | 镜像仓库域名（仅 push 模式使用） |
| `nacos.url` | prod 环境 Nacos 地址 |
| `nacos.namespace` | 命名空间 ID |
| `nacos.group` | 默认分组，一般 DEFAULT_GROUP |
| `mysql_snailjob.*` | xxx 数据库连接 |
| `ssh.user` / `ssh.key_file` | 连接服务器的 SSH 用户和私钥 |
| `deploy.project_root` | 本地业务项目源码根目录 |
| `build.maven_profile` | Maven 打包 profile（-P 参数），决定 jar 内固化的环境配置；缺省按 `--env` 推导（`test→-Ptest`、`dev→-Pdev`），不传 `--env` 默认 `-Pprod`，未知环境需显式配置 |
| `deploy.docker_compose_path` | 服务器上 docker-compose.yml 路径 |
| `deploy.dist_dir` | direct 模式本地镜像 tar 导出目录（默认 `dist`） |
| `deploy.remote_tmp_dir` | direct 模式服务器临时目录（默认 `/tmp/platform-deploy`） |

### config/servers.yaml

按实际服务器填写，第一台默认作为首台灰度：

```yaml
servers:
  - ip: "192.168.1.1"
    role: first
  - ip: "192.168.1.2"
    role: follower
```

### config/projects.yaml

每个项目包括：

- `build_order`：Maven 构建顺序和 goal
- `docker_context` / `dockerfile`：Docker 构建上下文
- `service_name`：docker-compose 里的服务名
- `snailjob_group`：定时任务分组，非定时任务留空
- `health_url` / `health_port`：健康检查地址（`health_url` 留空/省略时退化为容器运行状态检查）

## 直传模式（无镜像仓库）

当没有公共镜像仓库时，把 `config/deploy.yaml` 的 `registry.mode` 设为 `direct`：

- 构建阶段：`docker build` 后**不推送**，镜像以短名 `{namespace}/{image_name}:{version}` 留在本地（如 `xxx/服务名称:1.0.29`，与 IDEA 直推格式一致）
- 部署阶段：脚本自动执行 本地 `docker save` 导出 tar → SFTP 上传 → 服务器 `docker load` → 更新 compose 镜像名
- 以镜像 ID（digest）为准判断是否需要传输：本地与远端一致才跳过（多台滚动不会重复传大包）；**同版本重复发布新代码时远端 digest 不同，会自动重新传输**，本地 `dist/` 的旧 tar 包也会校验内容后重新导出
- 无需配置 `registry.host/username/password`（仅 push 模式使用），`docker login` 全流程跳过；`registry.namespace` 仍作为镜像名前缀使用

直传模式回滚说明：回滚到 `--previous-version` 时，脚本优先使用服务器本地与本地镜像一致的旧镜像；若服务器上没有，会尝试从本地导出旧版本镜像传输，此时本地也必须保留有旧版本镜像（或上次构建的 tar 包在 `dist/` 目录）。

## 运行方式（deploy_mode）

每个服务在「管理 → 服务」里选择一种发布方式。七种方式共用项目/环境/流水线/回滚编排，差异只在构建产物与切换动作：

- `container`：Docker 容器（默认）。Maven 构建 → 镜像推送/直传 → 滚动替换容器。
- `static_archive`：静态站点压缩包（1Panel/nginx）。前端构建 → zip → 站点目录解压换入，无容器、无 Nacos。
- `jar_service`：纯 Java 可执行 jar + Linux systemd。Maven 构建 → jar 上传到目标机版本目录 → 切换 `CURRENT` 符号链接 → 重启单元。
- `go_binary`：Go 单二进制 + Linux systemd。平台机交叉编译（`CGO_ENABLED=0 GOOS=linux`）出 Linux 二进制 → 上传到目标机版本目录 → 切换 `CURRENT` 符号链接 → 重启单元，目标机无 JDK/解释器依赖（`CGO_ENABLED=0` 产物静态链接）。
- `rust_binary`：Rust 单二进制 + Linux systemd。平台机 `cargo build --release` 构建出 Linux 二进制，后续切换动作与 `go_binary` 相同。
- `node_process`：Node 后端进程 + Linux systemd。平台机 `npm ci` → 可选 `npm run build`（`package.json` 有 `build` 脚本才执行）→ `npm prune --omit=dev` 打成含 `node_modules` 的 tar.gz → 上传解压到目标机版本目录 → 切换 `CURRENT` 符号链接 → 重启单元，目标机只需 node。
- `python_process`：Python 进程 + Linux systemd。平台机收集 manylinux wheel 打成 wheelhouse 包（源码 + `requirements.txt` + `wheels/`）→ 上传解压到目标机版本目录 → 停止单元 → 切换 `CURRENT` 符号链接 →（依赖变化时）离线装进 `app_home/venv` → 启动单元，目标机只需 python3 + python3-venv。

`jar_service` 要求目标机已安装 JDK/JRE，并由平台生成 systemd 单元后接入（管理员操作，记审计）；平台不安装 JDK，也不接管人工维护的单元。版本目录 `{app_home}/releases/{version}/app.jar` 不可变，`CURRENT` 符号链接原子切换，回滚只换链接不重传。单实例重启存在停机窗口，平台不承诺零停机。字段表与操作步骤见 `docs/usage.md`「Java 进程发布（jar_service）」。平台周期探测单元状态并从 `.deploy_version` 读取每主机版本；版本变化时写穿 `service_host_state`，监控关闭时页面回退到该快照。

Go/Rust 单二进制（`deploy_mode=go_binary`/`rust_binary`）：平台机交叉编译（Go，`CGO_ENABLED=0 GOOS=linux`）或 cargo 构建出 Linux 二进制，与 jar 共用 releases/CURRENT 切换与 systemd 托管；目标机无 JDK/解释器依赖——Go 产物静态链接，Rust gnu target 产物仍依赖目标机 glibc 版本（glibc 过旧会报 `GLIBC_2.xx not found`，可改 musl target 或静态链接规避，见 `docs/usage.md` 10.9）。构建前提在平台机：`go`/`cargo` 在 PATH，或配置 `build.go_home`/`build.cargo_home` 指向工具链（Windows 下同时解析 `go.exe`/`cargo.exe`、`.cmd`/`.bat` 等可执行形态）。Windows/macOS 平台机需配置 `build.rust_target`（如 `x86_64-unknown-linux-gnu`，并安装对应 target 与 linker）：Windows 未配置直接报错，macOS 未配置会静默产出 darwin 二进制，均建议改用 WSL/Linux 构建或 `build_type=none` 外部投入。外部投入（`build_type=none`）时须把产物放到平台机 `dist/`（`deploy.dist_dir`）下，即 `dist/{service}-{version}`，并附同名 `.sha256` 旁文件，发布前会校验。目标机前提与 jar 相同：仅 Linux + systemd，单元由平台生成并接入，平台只接管 `ExecStart` 指向 `{app_home}/CURRENT/app` 特征串的单元（jar 为 `…/CURRENT/app.jar`）。单实例发布重启存在停机窗口，平台不承诺零停机；jar/容器/静态三种既有方式行为零变化。字段说明见 `docs/usage.md`「Go/Rust 单二进制发布」。

Node 进程（`deploy_mode=node_process`）：平台机 `npm ci` →（可选 build）→ `npm prune --omit=dev` 打 tar.gz（含 node_modules），远端解压到 `releases/{version}/` 后切换 `CURRENT`；目标机只需 node。Python 进程（`deploy_mode=python_process`）：平台机收集 manylinux wheel，目标机在 `app_home/venv` 离线安装（不联网、不写系统目录），requirements 变化时自动重装，回滚到依赖不同的旧版本时先重装再重启。完整字段、构建前提与限制见 `docs/usage.md`「Node/Python 进程发布」。目标机只需预装 node（`runtime_config.node_bin` 指向其绝对路径）或 python3 + python3-venv（含 ensurepip），平台不代装任一运行时；单实例发布在「停止 → 换链接 → 启动」期间服务不可用，Python 触发依赖重装还会拉长这段窗口，平台不承诺零停机。

## 回滚策略

- **发布时自动回滚**：单台健康检查失败时，脚本自动把容器回滚到 `--previous-version` 并恢复 Nacos 上线；首台失败会立即停止继续滚动。
- **全量回滚**（`--rollback`）：全部发布成功后发现问题，一键将所有服务器回滚到 `--previous-version`，跳过构建，逐台执行 Nacos 下线 → 回滚容器 → 健康检查 → Nacos 上线。
- 数据库 schema 变更等破坏性升级无法通过镜像回滚恢复，请单独规划。

## 快捷方式与增强功能

### 交互式菜单（无需记参数）

```bash
python menu.py            # 或 menu.ps1 / menu.sh / menu.bat
```

依次选择：环境 → 单服务/流水线 → 项目 → 自动查询线上版本（推荐新版本号）→ 确认执行。

### 自动版本号（--auto-version）

SSH 查询服务器上容器当前镜像 tag，自动推导：`previous-version = 当前版本`、`version = 当前版本 patch+1`：

```bash
./deploy.sh --project xxx --auto-version
./pipeline.sh --pipeline xxx --auto-version
```

`--auto-version` 与显式 `--version/--previous-version`、`--rollback/--build-only/--push-only` 互斥；首次发版（服务器无容器）会报错提示手动指定版本。

### Git Tag 联动

`config/projects.yaml` 每个项目新增 `git_repo` 字段（服务 → 源码仓库映射），发布成功后自动在仓库打 `v{version}` 标签并推送：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28
# 发布成功后自动: cd 仓库 && git tag v1.0.29 && git push origin v1.0.29
```

- `--no-git-tag`：跳过打标签
- `--skip-git-push`：打标签但不推送远端
- 未配置 `git_repo` 或 git 操作失败仅告警，不阻断发布

### 消息通知（飞书 / 钉钉）

`config/deploy.yaml` 增加 `notify` 节（默认 `enabled: false`）：

```yaml
notify:
  enabled: true
  channels: ["feishu", "dingtalk"]
  feishu_webhook: ""       # 或用环境变量 NOTIFY_FEISHU_WEBHOOK
  dingtalk_webhook: ""     # 或用环境变量 NOTIFY_DINGTALK_WEBHOOK
  dingtalk_secret: ""      # 钉钉加签密钥，可选
  on_events: ["start", "success", "failed", "rollback"]
```

发布开始/成功/失败/回滚时推送群机器人消息；通知失败仅告警不阻断。Jenkins 也可单独调用 `python scripts/notify.py --event success --project xxx --version 1.0.30`。

### Web Dashboard（浏览器操作台）

新版大屏一体化操作台（配置管理 + 发布 + 监控 + 多用户角色）见上文「5. 浏览器操作台」，启动方式、功能与角色说明以该节为准。

### Jenkins 接入

`jenkins/Jenkinsfile` 已重写为参数化 Pipeline（项目/流水线/版本/环境/自动版本/跳过构建等），接入步骤见 `docs/jenkins-setup.md`。

### 其他

- 权重灰度：新版本实例上线后先给低权重，逐步放大（`nacos_manager.py` 支持在线改权重）。
- 1Panel 兼容：目前直接操作 docker-compose.yml，1Panel 仅用于查看日志。

## AI 智能运维助手（Agent）

Web 操作台之上叠加的 AI 层，四阶段演进：**只读智能运维助手**（LLM + 49 个运维工具 + Web/CLI/MCP 对话）→ **决策点 AI 化**（灰度继续/停止/回滚、发布失败归因+回滚建议、告警归因、发布前预检，规则引擎优先 + LLM 润色）→ **发布执行 Agent**（自然语言生成计划 → 人工审批（Web 审批单页或对话中明确指示 decide_run，决策者身份落审计）→ 复用平台任务通道执行 → 审计闭环）→ **MCP Server + 长期记忆案例库**。支持自然语言管理项目/服务/环境/服务器/发布流水线、定时发布计划、发布窗口、灰度策略、灰度运行中决策与告警闭环（含告警规则与静默窗口：调阈值/改作用域/发版期间静默告警，operator/admin；平台配置均仅 admin；定时计划立即执行同样需人工审批）；观测与治理对话化：日志检索可继续看某条前后文（logs_context）、响应时间历史与服务器快照扩展指标可查（monitor_metrics）、日志存储概况/保留期/手动清理可对话完成（manage_log_storage，仅 admin）；发布决策支持：服务依赖全图/单服务上下游影响/建议发布顺序/部署快照与两次快照间版本差异可查（service_topology），依赖关系可对话维护（manage_dependencies，含环检测；每次部署成功自动写环境快照供对比）；到期监控可对话管理（manage_monitors：证书/域名/云续费/license 到期项，domain 自动 WHOIS，ssl 可手动探测，全局默认阈值仅 admin）；服务器 SSH 连通测试、监控采集即时刷新、配置导入导出（DB⇄YAML 备份迁移，仅 admin）均可对话完成；对话中给 git 地址与目标服务器即可走通「建项目 → 注册服务（git_repo 填 http(s) 远程 URL 首次构建自动 clone）→ 建环境（可从既有项目 clone 含服务器绑定）→ 绑定服务器 → 发布审批」完全对话化接入链，多服务分阶段发布可对话编排发布流水线（stages 用服务名，发布 target 填流水线名整链发布）；失败归因建议可一键采纳为回滚审批单（采纳后自动标记，支持忽略/重开与状态过滤的建议生命周期管理），已完结发布可一键沉淀为案例（案例库支持标签过滤、相似案例推荐与来源溯源）；多会话支持自动标题、重命名、置顶与关键字搜索，待处理建议以导航角标全站提醒；另带**周期巡检日报**：定时聚合服务健康/告警/发布/待审审批推送到企业微信/飞书/钉钉/邮件等六渠道（智能助手页配置，带发送历史追溯）；页面实时通道健康度可查询（live_status：SSE 订阅数/事件量/监控与告警引擎是否在跑）——「数据为什么不即时」有明确排查入口。

```powershell
# 配置 OpenAI 兼容 LLM 网关（也可在 Web「智能助手 → 网关配置」填写）
$env:AGENT_LLM_BASE_URL = "https://your-gateway/v1"
$env:AGENT_LLM_API_KEY  = "sk-xxx"

python webapp/app.py          # 默认同时启动 AI 建议调度（30s 周期）
# 浏览器打开 http://127.0.0.1:5678/#/agent 开始对话；
# 或命令行：python agent/cli.py --once "auth 服务最近有异常吗"
```

零新增 Python 依赖（requests 直连）；未配置网关时自动降级纯规则，核心发布/监控功能不受影响；审批永远人工，viewer 全链路只读。完整说明见 [docs/agent-evolution.md](docs/agent-evolution.md)。

## 开发与质量门禁

一条命令跑完全部自检：

```bash
python scripts/quality_gate.py            # 语法 → 静态检查 → Python 单测 → 前端
python scripts/quality_gate.py --quick    # 跳过 Python 单测（只跑语法/静态/前端，改动非逻辑时最快）
python scripts/quality_gate.py --only js  # 只跑前端（compile|lint|pytest|js）
python scripts/quality_gate.py --strict   # 工具缺失（ruff/node）也判 FAIL
```

四步的含义与口径：

- **compile**：`ast.parse` 逐个解析全部 Python 源码与 `tests/`，语法错误当场失败；纯只读（不在源码树落 `__pycache__`、不执行任何模块代码），成本 <1s。
- **lint**：装了 `ruff` 才跑（配置见 `ruff.toml`，只选 F/E4/E7/E9/W6 这类真错误规则；风格类规则刻意没开，历史长行未清理前开了就是噪音）。没装记 SKIP，`--strict` 下记 FAIL。
- **pytest**：`tests/` 全量用例，见下方测试隔离约定。
- **js**：`node --check` 逐个语法校验 `webapp/static/js/**`，再跑 `tests/js/*_test.js` 无头用例（不依赖浏览器）。没装 node 记 SKIP。

测试隔离约定（写新用例时请遵守，否则等于给真实环境埋雷）：

- 用例只用临时目录与临时库：`create_app(data_dir=..., config_dir=..., log_dir=...)`，任务日志用 `webapp.task_manager.set_log_dir(tmp)`；不要改 `webapp.DATA_DIR` 全局，也不要让用例碰仓库 `data/`（里面是真实库）。
- `tests/conftest.py` 有会话级护栏：它把 `webapp.DATA_DIR` / `webapp.CONFIG_DIR` 指到 pytest 临时目录，所以漏传目录的 `create_app()` 最坏也只污染临时目录（护栏前，这类裸调用会在真实 `data/platform.db` 上跑迁移、并把 `data/export/config/` 覆盖成用例造的假配置）。护栏不覆盖 `task_manager.LOG_DIR`——它由每次 `create_app` 改写，所以读日志目录的用例必须自己 `mock.patch.object(task_manager, "LOG_DIR", tmp)`，别依赖它的当前值。
- 禁止真实副作用：不发起 SSH/HTTP/构建，子进程用 stub（参考 `tests/test_task_log_isolation.py` 里 `mock.patch.object(task_manager, "build_cli", ...)`），导出用 `mock.patch("webapp.exporter.export_all", ...)`。RED 阶段的失败必须"失败在校验上"，不能失败成"真的写了生产目录"。
- `webapp/app.py` 在 import 时不建 app（惰性 `get_app()`），所以只 import 工具函数的脚本不会再触发首启引导。

`jenkins/Jenkinsfile` 在 Deploy 前加了 **Quality gate** stage：门禁任一 FAIL 直接中止，不进入发布；CI 镜像未装 node/ruff 时该步记 SKIP，不会误伤发布。

## 注意事项

- 第一次使用前，务必在测试环境或首台服务器反复验证。
- `sj_job` 备份文件会保存在 `./backups/` 目录，请定期清理。
- 如果 Nacos 是 2.x 且 Open API 路径不兼容，请根据实际情况调整 `scripts/nacos_manager.py`。
- 配置文件中的密码仅作占位，生产环境请使用环境变量或密钥管理工具。

## 便携版打包与发布（Windows 免安装）

面向无 Python 环境的机器：一个 zip 解压即用，包内置 Python 3.12.10 embeddable 运行时（3.12 系列最后一个带 embeddable 的版本）与全部第三方依赖（`runtime\python\Lib\site-packages`），免安装、免联网、免 pip。产物目录 `dist-portable/` 已被 `.gitignore` 忽略，不入库。

### 出包

版本号由 `scripts/package_portable.py` 顶部常量 `RELEASE_VERSION` 控制（改版本号只改这一处）：

```powershell
pytest tests -q                     # 发布门禁：先跑绿用例（仓库根 pytest.ini 已约束 testpaths）
python scripts/package_portable.py  # 五阶段一键出包，走缓存约 24~30s
```

五阶段（任一环失败即中止，不可绕过）：

| 阶段 | 做什么 |
| --- | --- |
| fetch | 下载 Python embeddable 与 wheel 到 `dist-portable/.cache/`，`pip --target --platform win_amd64` 交叉安装；`--refresh` 强制重下 |
| assemble | 白名单组装：仅 `agent/ webapp/ scripts/ deploy_strategies/ config/` + `deploy.py deploy_pipeline.py menu.py requirements.txt README.md LICENSE` + `scripts/portable/` 启动器；`data/`、`.venv/`、`.git/`、`dist*/` 永不进包 |
| leak_check | 三重防线：白名单 + 禁入断言（`*.db`/`secret_key`/`__pycache__`/日志出现即失败）+ 凭据探针（从源 `data/` 全表与 YAML 收集真实值扫包内文本，并做密钥形态识别；命中经公开面判定，泄漏即失败） |
| smoke | 用包内 Python 真实拉起平台（自动建库、admin 首启强制改密、JWT 登录、页面与静态资源 200），随后清理回出厂态并复检禁入项 |
| zip | 产物 `dist-portable/智能运维平台-便携版-{版本}.zip`，日志输出文件数与 SHA256 |

调试参数：`--no-zip` 只出文件夹、`--skip-smoke` 跳过冒烟（**禁止用于发布**）、`--clean` 清空 `dist-portable/`。启动器 `启动平台.bat` / `menu.bat` 以 PATH 前置 `runtime\python`，`launcher.py` 从 5678 起自动避让 20 个端口并打开浏览器。

### 发布收口（双远端 + Release）

```powershell
git tag -f v<版本> <commit>                     # tag 必须指向实际打包基线，保证 tag 与包内容同源
git push origin v<版本> --force                 # Gitee
git push github master; git push github v<版本> --force

Copy-Item "dist-portable\智能运维平台-便携版-v<版本>.zip" `
          "dist-portable\SmartOps-portable-v<版本>-win-x64.zip"
# 新版本：创建 Release 并上传资产
gh release create v<版本> "dist-portable\SmartOps-portable-v<版本>-win-x64.zip" `
  --repo <owner>/<repo> --title "智能运维平台 便携版 v<版本>" `
  --notes-file dist-portable\_release-notes-v<版本>.md
# 同版本原地重发：替换资产并刷新说明
gh release delete-asset v<版本> <旧asset> --repo <owner>/<repo> --yes
gh release upload v<版本> "dist-portable\SmartOps-portable-v<版本>-win-x64.zip" --repo <owner>/<repo> --clobber
gh release edit v<版本> --repo <owner>/<repo> --notes-file dist-portable\_release-notes-v<版本>.md
```

- **发布资产必须用 ASCII 文件名**（如 `SmartOps-portable-v<版本>-win-x64.zip`）：中文文件名会被 GitHub 吞成 `-.-`，下载直链失效。
- 发布前刷新 `dist-portable/_release-notes-v<版本>.md` 的 SHA256 与打包 commit；上传后核对 GitHub 自算 `digest` 与本地 SHA256 一致，并用免登录直链验证 `302 → 200` 且 `Content-Length` 与本地一致。
- 发布完整性核验：包内源码逐文件与 `git show <打包基线commit>:<path>` 的 blob 比对（不可拿 master HEAD 当基准；`core.autocrlf=true` 下仅 CRLF 行尾差异属预期）。
- 发布后若 master 上只追加测试配置/文档（不改包内白名单源码），无需重打包或挪 tag；在 release notes 写明产物基线 commit，避免包内文档比 master 滞后被误认为产物过期。
- 渠道定位：Gitee 为使用主渠道（仅同步 `master` + tag，未建发行版），下载入口统一在 GitHub Release。
- 发布前删除陈旧包副本（如 `dist-portable/_preview`）：旧包易被误当发布物。
- Releases 列表页的时间取的是 release 的 `published_at`：`gh release edit` 与 delete-asset + upload 只推进 `updated_at`，同版本号原地重发后页面仍显示首次发布时间（看起来像旧版，但资产 digest 已是新的）。需要把时间刷成当天只能 `gh release delete v<版本> --yes`（不会删 tag）后 `gh release create v<版本> --title … --notes-file …` + `gh release upload` 重建；代价是下载计数与 release 编辑历史归零，`/releases/tag/<v>` 与 `/releases/download/...` 直链不变。
