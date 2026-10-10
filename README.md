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
pip install -r requirements.txt
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
- 实时监控：默认 30s 周期 SSH 探测容器 + Nacos 查询实例（`--monitor-interval 0` 关闭）。
- 配置存 `data/platform.db`（权威源）；`config/` 目录为导出产物 + CLI 兼容（`deploy.py --config-source yaml`）。
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

Web 操作台之上叠加的 AI 层，四阶段演进：**只读智能运维助手**（LLM + 34 个运维工具 + Web/CLI/MCP 对话）→ **决策点 AI 化**（灰度继续/停止/回滚、发布失败归因+回滚建议、告警归因、发布前预检，规则引擎优先 + LLM 润色）→ **发布执行 Agent**（自然语言生成计划 → 人工审批 → 复用平台任务通道执行 → 审计闭环）→ **MCP Server + 长期记忆案例库**。支持自然语言管理项目/服务配置、定时发布计划、发布窗口、灰度策略、灰度运行中决策与告警闭环（项目/服务/发布窗口管理仅 admin；定时计划立即执行同样需人工审批）；对话中给 git 地址即可走通「建项目 → 注册服务（git_repo 填 http(s) 远程 URL 首次构建自动 clone）→ 发布审批」端到端接入链；失败归因建议可一键采纳为回滚审批单（采纳后自动标记，支持忽略/重开与状态过滤的建议生命周期管理），已完结发布可一键沉淀为案例（案例库支持标签过滤、相似案例推荐与来源溯源）；多会话支持自动标题与重命名，待处理建议以导航角标全站提醒；另带**周期巡检日报**：定时聚合服务健康/告警/发布/待审审批推送到企业微信/飞书/钉钉/邮件等六渠道（智能助手页配置，带发送历史追溯）。

```powershell
# 配置 OpenAI 兼容 LLM 网关（也可在 Web「智能助手 → 网关配置」填写）
$env:AGENT_LLM_BASE_URL = "https://your-gateway/v1"
$env:AGENT_LLM_API_KEY  = "sk-xxx"

python webapp/app.py          # 默认同时启动 AI 建议调度（30s 周期）
# 浏览器打开 http://127.0.0.1:5678/#/agent 开始对话；
# 或命令行：python agent/cli.py --once "auth 服务最近有异常吗"
```

零新增 Python 依赖（requests 直连）；未配置网关时自动降级纯规则，核心发布/监控功能不受影响；审批永远人工，viewer 全链路只读。完整说明见 [docs/agent-evolution.md](docs/agent-evolution.md)。

## 注意事项

- 第一次使用前，务必在测试环境或首台服务器反复验证。
- `sj_job` 备份文件会保存在 `./backups/` 目录，请定期清理。
- 如果 Nacos 是 2.x 且 Open API 路径不兼容，请根据实际情况调整 `scripts/nacos_manager.py`。
- 配置文件中的密码仅作占位，生产环境请使用环境变量或密钥管理工具。
