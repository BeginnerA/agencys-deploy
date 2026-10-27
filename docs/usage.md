# 智能运维平台一键发布脚本使用说明书

## 一、简介

`agencys-deploy` 是一套面向微服务项目的发布自动化脚本（智能运维平台），把原先在 IDEA、Nacos、1Panel、SSH 之间反复切换的手动流程，收敛成一条命令。它支持三种使用方式：

- **单服务发布**：一次只发布一个微服务，例如 `xxx`。
- **首台灰度发布**：先更新一台服务器，观察通过后再继续更新其余服务器。
- **多服务流水线发布**：按照 `config/pipelines.yaml` 中定义的顺序，分阶段发布多个微服务，支持阶段内串行或并行。

发布过程中会自动完成 Maven 构建、Docker 镜像推送、Nacos 实例下线上线、服务器容器滚动更新、健康检查、失败自动回滚、SnailJob 定时任务暂停与恢复等操作。

## 二、前置条件

### 2.1 控制机环境

控制机可以是本地开发机、WSL、macOS、Linux 服务器或 Jenkins agent，需要安装：

- Python 3.9 及以上版本
- Maven 3.8 及以上版本
- Docker（已登录到目标镜像仓库并能推送）
- 到所有目标服务器的免密 SSH 访问

### 2.2 目标服务器环境

- 已安装 Docker 和 docker compose 插件
- 已执行 `docker login` 登录到镜像仓库
- 服务器上有统一的 `docker-compose.yml` 文件，例如 `/data/docker/docker-compose.yml`
- 服务器能访问 Nacos、MySQL、镜像仓库和控制机健康检查端口

### 2.3 安装依赖

在控制机上进入项目目录（`agencys-deploy`），执行：

```bash
cd agencys-deploy
pip install -r requirements.txt
```

`requirements.txt` 包含 `PyYAML`、`requests`、`PyMySQL`、`paramiko`。

改代码或跑 CI 时再装开发依赖（`pytest`、`ruff`）：

```bash
pip install -r requirements-dev.txt
python scripts/quality_gate.py     # 语法 + 静态检查 + 单测 + 前端无头用例
```

发布流水线（`jenkins/Jenkinsfile`）已在 Deploy 前加同名门禁 stage，任一 FAIL 即中止发布。工具缺失（未装 ruff/node）记 SKIP，加 `--strict` 可强制 FAIL。

测试隔离：`tests/conftest.py` 在会话开始把 `webapp.DATA_DIR` / `webapp.CONFIG_DIR` 重定向到 pytest 临时目录，所以即使某个用例漏传目录，`create_app()` 也不会去真实 `data/` 上跑迁移或写导出物。写用例时仍应显式传 `create_app(data_dir=...)`；涉及任务/采集日志目录的用例要自己 `mock.patch.object(task_manager, "LOG_DIR", tmp)`——那个全局每次 `create_app` 都会被改写，不能依赖它的当前值。

## 三、目录结构

```
agencys-deploy/
├── config/
│   ├── deploy.yaml          # 全局配置：镜像仓库、Nacos、MySQL、SSH、发布参数
│   ├── deploy.prod.yaml     # 生产环境配置（--env prod 时读取）
│   ├── deploy.test.yaml     # 测试环境配置（--env test 时读取）
│   ├── projects.yaml        # 项目元数据：构建顺序、Dockerfile、服务名、SnailJob 分组等
│   ├── servers.yaml         # 默认目标服务器清单
│   ├── servers.prod.yaml    # 生产环境服务器清单
│   ├── servers.test.yaml    # 测试环境服务器清单
│   └── pipelines.yaml       # 多服务流水线定义
├── scripts/
│   ├── common.py            # 配置加载、日志、环境变量读取
│   ├── build.py             # Maven 构建 + Docker 构建推送
│   ├── nacos_manager.py     # Nacos 实例查询、下线、上线、改权重
│   ├── health_check.py      # HTTP 健康检查 + 容器日志错误关键字扫描
│   ├── snailjob_manager.py  # SnailJob 备份、暂停、恢复、清零计数器
│   └── package_portable.py  # Windows 免安装便携包打包（见 10.7）
├── deploy.py                # 单服务发布主控脚本
├── deploy.sh                # 单服务发布入口脚本
├── deploy_pipeline.py       # 多服务流水线发布主控脚本
├── pipeline.sh              # 多服务流水线入口脚本
├── ansible/                 # 可选 Ansible 封装
├── jenkins/                 # 可选 Jenkins Pipeline 示例
├── docs/
│   └── usage.md             # 本说明书
└── README.md
```

## 四、配置说明

### 4.1 config/deploy.yaml

这是全局配置文件，发布前必须把所有占位值替换为生产环境真实值。敏感信息建议通过环境变量传入。

```yaml
registry:
  host: "harbor.example.com"        # 镜像仓库域名
  namespace: "xxx"                  # 镜像仓库命名空间
  username: ""                      # 建议通过环境变量 REGISTRY_USERNAME 传入
  password: ""                      # 建议通过环境变量 REGISTRY_PASSWORD 传入

nacos:
  url: "http://nacos-prod.example.com:8848"
  namespace: "prod"                 # 命名空间 ID，public 可留空
  group: "DEFAULT_GROUP"
  username: ""                      # 建议通过环境变量 NACOS_USERNAME 传入
  password: ""                      # 建议通过环境变量 NACOS_PASSWORD 传入

mysql_snailjob:
  host: "mysql.example.com"
  port: 3306
  database: "xxx"
  username: ""                      # 建议通过环境变量 MYSQL_USERNAME 传入
  password: ""                      # 建议通过环境变量 MYSQL_PASSWORD 传入

ssh:
  user: "deploy"
  key_file: "~/.ssh/id_rsa_deploy"  # SSH 私钥路径
  port: 22

deploy:
  project_root: "/path/to/项目根目录"     # 本地 Maven 项目源码根目录
  docker_compose_path: "/data/docker/docker-compose.yml"
  health_timeout: 120               # 健康检查总超时（秒）
  health_interval: 5                # 健康检查轮询间隔（秒）
  canary_wait: 0                    # 首台灰度观察等待时间（秒），0 表示不等待
```

推荐通过环境变量注入敏感信息：

```bash
export REGISTRY_USERNAME=your_name
export REGISTRY_PASSWORD=your_pass
export NACOS_USERNAME=your_name
export NACOS_PASSWORD=your_pass
export MYSQL_USERNAME=your_name
export MYSQL_PASSWORD=your_pass
```

### 4.2 config/servers.yaml

目标服务器清单。`role: first` 表示首台灰度服务器，其余为 follower。发布时会按数组顺序依次处理，第一台默认作为灰度机。

```yaml
servers:
  - ip: "192.168.1.1"
    role: first
  - ip: "192.168.1.2"
    role: follower
```

如果某些服务需要发布到不同的服务器组，可以在 `config/pipelines.yaml` 的服务配置里单独指定 `servers_file`，也可以单独创建 `servers-canary.yaml` 等文件，发布时通过 `--servers-file` 指定。

### 4.3 config/projects.yaml

每个项目的元数据，新增项目时按相同结构追加即可。以 `xxx` 为例：

```yaml
projects:
  xxx:
    build_order:
      - name: xxx
        path: xxx
        goal: install
      - name: xxx
        path: xxx
        goal: install
      - name: xxx
        path: xxx
        goal: package
    docker_context: xxx
    dockerfile: xxx/Dockerfile
    image_name: xxx
    service_name: xxx
    dubbo_services: []
    snailjob_group: ""                  # 非定时任务项目留空
    health_url: "http://{ip}:9207/actuator/health"
    health_port: 9207
    log_error_keywords:
      - "ERROR"
      - "Exception"
      - "Failed to start"
```

关键字段说明：

- `build_order`：Maven 构建顺序，`goal` 一般为 `install` 或 `package`。
- `docker_context` / `dockerfile`：Docker 构建上下文和 Dockerfile 相对路径。
- `image_name`：镜像短名，完整镜像名为 `{registry}/{namespace}/{image_name}:{version}`。
- `service_name`：服务器 `docker-compose.yml` 中对应的服务名。
- `dubbo_services`：如果该服务在 Nacos 中还注册了额外的 Dubbo 服务名，请列出。
- `snailjob_group`：定时任务分组名，非定时任务留空。
- `health_url`：健康检查地址，`{ip}` 会被替换为当前服务器 IP。留空/省略则跳过 HTTP 检查，退化为容器运行状态检查（`docker ps` 确认 Up）+ 日志关键字检查。
- `health_port`：服务对外端口，用于判断容器是否启动。
- `log_error_keywords`：容器日志中判定启动失败的错误关键字列表。
- `clear_log_dir`：宿主机应用日志目录（如 1Panel 挂载的 logs 目录）。配置了该路径时，发布会在启动新容器前清空该目录下所有文件（保留目录结构）；省略/留空则不清空（默认）。适用于希望新版本日志从零开始、避免与旧版本日志混杂的项目。回滚（`--previous-version`）不会清空日志。

### 4.4 config/pipelines.yaml

定义多服务发布流水线。一个流水线由多个 stage 组成，每个 stage 可以串行或并行发布其中的服务。

```yaml
pipelines:
  xxx:
    build_first: true          # true：先统一构建所有镜像，再进入滚动发布
    stages:
      - name: infra
        parallel: false        # false：stage 内服务串行发布
        services:
          - name: xxx
          - name: xxx
      - name: business
        parallel: true         # true：stage 内服务并行发布
        services:
          - name: xxx
            servers_file: ""   # 可单独指定服务器清单，留空使用默认
          - name: xxx
      - name: jobs
        parallel: false
        services:
          - name: xxx
          - name: xxx
```

- `build_first: true` 适合全量发布，避免阶段并行时多个 Maven 进程冲突。
- `build_first: false` 时，每个 stage 开始前会顺序构建本 stage 涉及的服务，再进入部署。
- 如果某个 stage 中任一服务发布失败，流水线会停止后续 stage，已经成功的服务保持新版本。

流水线默认使用命令行传入的 `--version` / `--previous-version` 作为所有服务的版本。如果不同微服务需要发布不同镜像版本，可以在 `services` 中为单个服务指定 `version` 和 `previous_version`，它们会覆盖全局版本号：

```yaml
pipelines:
  xxx:
    build_first: true
    stages:
      - name: business
        parallel: false
        services:
          - name: xxx
            version: 1.0.30
            previous_version: 1.0.29
          - name: xxx
            version: 1.0.28
            previous_version: 1.0.27
```

对应调用时可以省略 `--version` / `--previous-version`：

```bash
./pipeline.sh --pipeline xxx
```

也可以混合使用：命令行提供全局默认值，只有特殊服务在 `pipelines.yaml` 中单独指定版本。如果某个服务既没指定服务级版本，也没提供全局版本，脚本会拒绝执行并提示缺失版本的服务列表。

### 4.5 多环境配置（生产 / 测试）

脚本支持通过 `--env` 参数切换环境，自动读取对应的环境专属配置文件：

- `./deploy.sh --env prod ...` 会读取 `config/deploy.prod.yaml` 和 `config/servers.prod.yaml`
- `./deploy.sh --env test ...` 会读取 `config/deploy.test.yaml` 和 `config/servers.test.yaml`
- 不带 `--env` 时，默认读取 `config/deploy.yaml` 和 `config/servers.yaml`

因此，维护两套环境只需要在 `config/` 目录下放置对应的文件：

```
config/
├── deploy.yaml              # 默认配置（可当作生产环境模板）
├── deploy.prod.yaml         # 生产环境真实配置
├── deploy.test.yaml         # 测试环境配置
├── servers.yaml             # 默认服务器清单
├── servers.prod.yaml        # 生产环境服务器清单
├── servers.test.yaml        # 测试环境服务器清单
├── projects.yaml
└── pipelines.yaml
```

生产环境发布示例：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --env prod
```

测试环境发布示例：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --env test
```

`--env` 会同时切换**部署目标配置**（`deploy.{env}.yaml`、`servers.{env}.yaml`：服务器清单、SSH、Nacos 管理地址、MySQL）和 **Maven 打包 profile**：

- `--env test` → `mvn -Ptest`（jar 固化 `spring.profiles.active=test`，Nacos 命名空间 test）
- `--env prod` → `mvn -Pprod`
- 不传 `--env` → 默认生产配置，`mvn -Pprod`
- 自定义环境（如 qa）需在 `deploy.qa.yaml` 的 `build.maven_profile` 显式指定，否则脚本报错拒绝打包

Maven profile 决定 jar 内固化的环境配置：`application.yml` 中的 `@profiles.active@`、`@nacos.server@` 等占位符由 Maven 资源过滤替换，profile 定义在 `xxx/xxx/pom.xml`。

如果某个环境的服务器清单路径不按上述约定命名，可以通过 `--servers-file` 显式覆盖：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --env test --servers-file config/servers-test-canary.yaml
```

流水线同样支持 `--env`：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --env prod
```

## 五、单服务发布

### 5.1 标准发布

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28
```

这条命令会执行：

1. 按 `projects.yaml` 中的 `build_order` 执行 Maven 构建（profile 由 `--env` 推导：`test→-Ptest`、`prod→-Pprod`、不传默认 `-Pprod`，见上文「多环境」说明）。
2. 构建 Docker 镜像并推送到镜像仓库。
3. 读取 `config/servers.yaml`，逐台服务器滚动发布。
4. 每台服务器：Nacos 下线 → 更新 docker-compose.yml → 启动容器 → 健康检查 → Nacos 上线。

**首次发版**（服务器上没有旧版本）可省略 `--previous-version`，失败时跳过容器回滚、只恢复 Nacos 上线：

```bash
./deploy.sh --project xxx --version 1.0.0
```

### 5.2 跳过构建

如果镜像已经构建并推送过，只想执行滚动发布：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --skip-build
```

### 5.3 跳过 SnailJob 控制

如果只想验证部署流程，不想暂停或恢复定时任务：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --skip-snailjob
```

### 5.4 清零巡检计数器

足球、篮球等项目发布结束后，可能需要把 `sj_job_summary` 中的失败计数清零：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --clear-summary
```

### 5.5 金丝雀模式（--canary）

首台发布成功后自动暂停，等待人工确认后再决定是否继续发布剩余服务器。适合生产环境重要服务升级时先发布一台验证、再全量发布。

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --canary
```

首台发布完成后终端会显示提示：

```
============================================================
金丝雀模式：首台 192.168.1.1 已发布成功
请验证服务是否正常，然后选择操作：
  [y] 继续发布剩余服务器
  [n] 停止发布（首台保持新版本）
  [r] 回滚首台到 1.0.28
============================================================
>>> 请选择 (y/n/r):
```

三个选项：

- **y**（continue）：继续发布剩余服务器，与不加 `--canary` 时一样逐台滚动。
- **n**（stop，默认）：停止发布，首台保持新版本，其余服务器不受影响。直接回车等同 `n`。
- **r**（rollback）：把首台回滚到 `--previous-version`（下线 Nacos → 回滚容器 → 重新上线）。仅在指定了 `--previous-version` 时可用。

> 注意：如果只有一台服务器，不会触发交互提示（没有剩余服务器可继续）。如果首台发布失败，也不会触发提示，直接停止滚动。

流水线同样支持 `--canary`，每个服务的首台都会暂停确认：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --canary
```

### 5.6 全量回滚（--rollback）

全部服务器发布成功后发现业务异常，可一键将所有服务器回滚到旧版本。

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --rollback
```

回滚流程与发布类似，但跳过构建，逐台执行：

1. 暂停 SnailJob（如配置了 snailjob_group）
2. Nacos 下线当前服务器
3. 回滚容器到 `--previous-version`
4. 健康检查
5. Nacos 重新上线
6. 恢复 SnailJob

与发布的差异：

- **不阻断**：某台回滚失败不会停止后续服务器，尽量多回滚，最终汇总成功/失败数量。
- **必须指定 `--previous-version`**：这是回滚的目标版本，不提供会报错。
- **跳过构建**：回滚不需要重新构建镜像，直接使用服务器本地已有的旧镜像（直传模式下若服务器没有，会尝试从本地导出上传）。

流水线同样支持，且会按 stage **逆序**回滚（与发布顺序相反）：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --rollback
```

### 5.7 仅构建与构建推送

#### 5.7.1 仅构建（--build-only）

只执行 Maven 打包和 Docker 构建，不推送镜像到服务器、不执行发布。适用于提前准备好镜像、或在其他环境构建验证。

```bash
./deploy.sh --project xxx --version 1.0.29 --build-only
```

执行完成后本地会生成 Docker 镜像，但不会传输到任何服务器，也不会触发 Nacos、SnailJob 等操作。`--previous-version` 不需要。

流水线支持：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --build-only
```

#### 5.7.2 构建并推送（--push-only）

构建 + 推送镜像到服务器，但不执行发布（不 Nacos 下线、不更新容器、不健康检查）。适用于提前将镜像传输到服务器，发布时配合 `--skip-build` 加速。

```bash
./deploy.sh --project xxx --version 1.0.29 --push-only
```

不同镜像模式下的行为：

- **直传模式**（`registry.mode: direct`）：执行 docker save → scp → docker load，将镜像传输到所有目标服务器。
- **仓库模式**（`registry.mode: push`）：构建阶段已将镜像推送到 registry，无需额外操作，效果等同 `--build-only`。

流水线支持：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --push-only
```

> **典型用法**：先 `--push-only` 提前传输镜像，发布时 `--skip-build` 跳过构建直接部署，大幅缩短发布窗口。

## 六、首台灰度发布

除 `--canary` 交互模式外，脚本还支持另外两种灰度方式：

### 6.1 定时等待（canary_wait）

如果配置了 `canary_wait`（秒），首台发布成功后会自动等待指定时间再继续（`--canary` 模式下此配置不生效，改为交互确认）：

```yaml
deploy:
  canary_wait: 300   # 首台成功后等待 5 分钟
```

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28
```

### 6.2 使用单独的服务器清单

更稳妥的做法是先创建一个只包含首台服务器的清单，例如 `config/servers-canary.yaml`：

```yaml
servers:
  - ip: "192.168.1.1"
    role: first
```

发布：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --servers-file config/servers-canary.yaml
```

观察业务指标无异常后，再发布剩余服务器：

```bash
./deploy.sh --project xxx --version 1.0.29 --previous-version 1.0.28 --servers-file config/servers.yaml --skip-build
```

## 七、多服务流水线发布

### 7.1 执行全量流水线

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28
```

`xxx` 会按 `infra → business → jobs` 三个阶段发布。`infra` 和 `jobs` 阶段内串行，`business` 阶段内 `xxx` 和 `xxx` 并行发布。

### 7.2 只发布业务服务

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28
```

### 7.3 只发布定时任务

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --clear-summary
```

### 7.4 跳过构建

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --skip-build
```

### 7.5 自定义默认服务器清单

如果流水线中大部分服务都使用同一组服务器，可以通过 `--servers-file` 指定；某个服务需要特殊服务器组时，在 `pipelines.yaml` 中单独配置 `servers_file`。

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28 --servers-file config/servers-prod.yaml
```

### 7.6 流水线中每个服务使用不同版本

在 `config/pipelines.yaml` 的服务项里加上 `version` 和 `previous_version` 即可覆盖命令行传入的全局版本：

```bash
./pipeline.sh --pipeline xxx
```

如果只有个别服务需要特殊版本，其余服务使用命令行默认值：

```bash
./pipeline.sh --pipeline xxx --version 1.0.29 --previous-version 1.0.28
```

其中 `xxx` 流水线里除显式指定版本的服务外，其他服务都会使用 `1.0.29` / `1.0.28`。

## 八、独立工具使用

### 8.1 查询 Nacos 实例

```bash
python scripts/nacos_manager.py list --service xxx
```

### 8.2 手动下线 / 上线某个 IP

```bash
python scripts/nacos_manager.py offline --service xxx --ip 192.168.1.1
python scripts/nacos_manager.py online --service xxx --ip 192.168.1.1 --weight 1.0
```

### 8.3 单独健康检查

```bash
python scripts/health_check.py --host 192.168.1.1 --project xxx
```

### 8.4 单独构建镜像

```bash
python scripts/build.py --project xxx --version 1.0.29
```

### 8.5 SnailJob 管理

```bash
# 备份并暂停足球定时任务（备份 SQL + 任务状态快照到 backups/）
python scripts/snailjob_manager.py pause --group football_group

# 按最新备份快照精确恢复：只恢复备份时运行的任务，备份前暂停的保持暂停
python scripts/snailjob_manager.py resume --group football_group

# 清零 sj_job_summary 计数器
python scripts/snailjob_manager.py clear-summary
```

`resume` 需要 `backups/` 目录下存在该任务组的快照文件（`sj_job_{group}_*.json`），无快照时拒绝恢复并提示人工处理。

## 九、回滚策略

### 9.1 自动回滚

单台服务器发布失败时，脚本会自动：

1. 把 `docker-compose.yml` 中的镜像改回 `--previous-version`。
2. 重新 `docker compose up -d` 拉起旧版本容器。
3. Nacos 恢复上线并设置权重为 1。

如果首台灰度失败，脚本会立即停止继续滚动，其余服务器保持旧版本不受影响。

### 9.2 手动回滚

如果发布已经成功但发现线上问题，可以手动执行旧版本发布：

```bash
./deploy.sh --project xxx --version 1.0.28 --previous-version 1.0.27 --skip-build
```

这里把 `--version` 指定为要恢复的旧版本号，`--previous-version` 指定为更旧的版本号（作为二次回滚兜底）。

### 9.3 不能自动回滚的场景

- 数据库 schema 变更、数据迁移等破坏性升级。
- 配置中心（Nacos 配置）已推送不可兼容的新配置。
- 涉及外部中间件版本升级。

这些场景请在发布前单独准备回滚方案。

## 十、快捷发布方式与增强功能

### 10.1 交互式菜单

无需记忆参数，按向导逐步选择：

```bash
python menu.py      # 或 menu.ps1 / menu.sh / menu.bat
```

流程：选择环境 → 单服务/流水线 → 项目 → 自动查询线上版本（提示当前线上版本与建议新版本）→ 确认执行。菜单内部调用 `deploy.py` / `deploy_pipeline.py`，行为与命令行完全一致。

### 10.2 自动版本号（--auto-version）

SSH 查询服务器上容器当前镜像 tag，自动推导回滚版本与新版本：

```bash
./deploy.sh --project xxx --auto-version
./pipeline.sh --pipeline xxx --auto-version
```

规则：

- `previous-version` = 服务器当前镜像版本，`version` = 当前版本 patch+1（如 1.0.29 → 1.0.30）。
- 与显式 `--version` / `--previous-version` 互斥，同时传入直接报错。
- 与 `--rollback` / `--build-only` / `--push-only` 互斥（这些模式没有“新版本”概念）。
- 服务器上不存在运行中的容器（首次发版）时报错，需手动指定 `--version`。
- 流水线模式下每个服务独立查询各自线上版本。

### 10.3 Git Tag 联动

`config/projects.yaml` 中每个服务新增 `git_repo` 字段，指向其源码仓库（如 xxx → xxx）。发布成功后自动执行：

```bash
cd {git_repo} && git tag v{version} && git push origin v{version}
```

- `--no-git-tag`：跳过打标签。
- `--skip-git-push`：打标签但不推送远端。
- 未配置 `git_repo`、本地无该仓库、tag 已存在或 git 操作失败，仅打日志告警，不阻断发布。
- 发布与全量回滚流程均会触发。
- `git_repo` 支持两种形态：相对 `deploy.project_root` 的本地目录名，或 http(s) 远程 URL（如 `https://git.example.com/order-service.git`）。URL 形态首次构建时自动 clone 到 `project_root` 下同名目录（末段去 `.git`，须为字母数字 `_ -`），此后按本地仓库复用；clone 失败会阻断构建（区别于 checkout/pull 的仅告警），URL 内嵌凭证在日志中自动脱敏。

### 10.4 消息通知（飞书 / 钉钉）

在 `config/deploy.yaml` 增加 `notify` 节：

```yaml
notify:
  enabled: true
  channels: ["feishu", "dingtalk"]
  feishu_webhook: ""       # 留空则读环境变量 NOTIFY_FEISHU_WEBHOOK
  dingtalk_webhook: ""     # 留空则读环境变量 NOTIFY_DINGTALK_WEBHOOK
  dingtalk_secret: ""      # 钉钉加签密钥（可选），留空则读 NOTIFY_DINGTALK_SECRET
  on_events: ["start", "success", "failed", "rollback"]
```

- `enabled` 默认 `false`，开启后才会发送。
- 渠道可同时配置多个；webhook 地址建议用环境变量注入，避免写入配置文件。
- 触发时机：发布开始（start）、成功（success）、失败（failed）、全量回滚完成（rollback）。
- 发送失败只记录告警日志，不影响发布流程。

单独测试或手动通知（Jenkins post 步骤也可用）：

```bash
python scripts/notify.py --event success --project xxx --version 1.0.30 --env prod
```

### 10.5 Web 操作台（智能运维平台大屏）

基于 Flask + SQLite + JWT 的智能运维平台大屏，浏览器里完成**配置管理 + 发布 + 监控**（大屏一体式，支持多项目）：

```bash
pip install -r requirements.txt
python webapp/app.py          # 默认 http://127.0.0.1:5678
```

- 首次启动自动导入 `config/` 旧配置（原 11 个服务 + 4 条流水线 + prod/test 环境），创建默认管理员 `admin/admin`（首次登录强制改密）。
- 大屏主页：服务状态卡片墙（在线/异常/发布中/未部署 + 镜像版本 + Nacos 实例数）、发布趋势/成功率图表、实时任务日志；点击卡片直接发布/回滚。
- 「管理」抽屉（admin）：项目管理、服务/环境/服务器/流水线 CRUD、用户与角色、YAML 导入导出。
- 实时监控：周期默认读取 `DASHBOARD_MONITOR_INTERVAL`（30s），可用 `--monitor-interval N` 覆盖；`0` 仅关闭实时探测与告警引擎，发布调度、日志采集、SSL 到期探测、AI 建议调度仍独立运行。为降低被监控服务器负载：同一服务器被多个环境绑定时整轮只探测 1 次（服务名取并集，环境视图从结果切片）；`docker stats` 仅对配置服务容器采样；CPU 使用率由 `/proc/stat` 累计值跨轮差分计算（不在被监控服务器上做 `sleep` 采样）；扩展指标（swap/网络/磁盘 IO/TCP/docker df）每 3 轮采集 1 次，节流轮复用上一轮缓存且不写快照（数据库写放大 3× → 1×）。
- 后台服务周期独立：发布调度 `--deploy-interval`（默认 30s，或 `DASHBOARD_DEPLOY_INTERVAL`）、AI 建议 `--agent-interval`（默认 30s，或 `DASHBOARD_AGENT_INTERVAL`，`0` 单独关闭）；监控关闭时日志采集回退为 120s 独立轮询。
- 日志采集：周期为监控的 2 倍；按主机聚合为单条 SSH 批量拉取（`@@LOGCUT` 分段标记），同名环境跨项目去重，避免重复追加与重复索引。
- 数据库维护：新库自动启用 `auto_vacuum=INCREMENTAL`，采集器每日执行 FTS optimize + 增量空间回收；存量库执行 `python scripts/db_vacuum.py` 一次性 VACUUM 回收空洞（需先停止平台）。也可在「日志监控 → 服务日志 → ⚙ 保留设置」抽屉（admin）查看存储概况并点「立即清理」手动执行同套策略（删过期索引行、归档过期文件、回收空闲页；与每日自动维护同一实现）。
- 配置存 `data/platform.db`（权威源）；`config/` 目录为导出产物 + CLI 兼容（`deploy.py --config-source yaml`）。
- 数据目录外置（多实例）：`python webapp/app.py --data-dir /srv/deploy-a [--config-dir /srv/cfg-a] [--log-dir /var/log/deploy-a]`。`--data-dir` 决定库文件、`secret_key`、配置导出物（`<data-dir>/export/config/`）与日志默认位置；灰度决策文件（`canary_decision_<任务ID>.json`）也写在它下面。同机跑第二套实例时必须显式指定，否则两套实例争用同一个 `data/platform.db`。不传参时与历史行为一致（项目内 `data/`、`config/`）。
- 平台拉起发布子进程时会注入两个环境变量：`PLATFORM_TASK_ID`（当前任务 ID）与 `PLATFORM_DATA_DIR`（实例数据目录）。发布脚本据此把灰度决策文件、db 模式的库文件与运行时配置目录都定位到本实例；手工在命令行跑 `deploy.py --canary` 时两个变量都不存在，回落到项目内 `data/`，属正常行为。
- `DASHBOARD_SECRET_KEY` 未设置时，密钥持久化在 `<data-dir>/secret_key`（按实例隔离，重启不掉登录态）；该目录不可写时回退内置开发密钥并在启动日志告警——生产务必显式注入。
- 角色：admin（全部）/ operator（发布回滚）/ viewer（只读）。生产部署建议 `--host 0.0.0.0` + 前置 Nginx 等反代，环境变量注入 `DASHBOARD_SECRET_KEY`。

- 发布任务通过子进程调用 `deploy.py` / `deploy_pipeline.py`（`--config-source db --project-id <ID>`），行为与命令行一致；任务串行后台执行、日志实时查看。

### 10.6 配置来源与 CLI 兼容（--config-source）

平台数据库 `data/platform.db` 是配置权威源，CLI 默认自动识别：

- `--config-source auto`（默认）：库中存在该项目（服务/流水线名匹配）则从数据库生成运行时配置，否则回退 `config/` 文件。
- `--config-source yaml`：强制使用 `config/` 目录（Jenkins / 脚本场景不受影响）。
- `--config-source db`：强制数据库，需搭配 `--project-id <项目ID>`；找不到项目/环境时明确报错。

库文件与运行时导出目录（`<data-dir>/runtime/p<项目ID>/<环境>/`）按「`PLATFORM_DATA_DIR` 环境变量 > 项目内 `data/`」解析，所以 Dashboard 拉起的多实例发布任务读写的是本实例的库，不会串到仓库 `data/`；报错信息里也会打印实际路径。

```bash
./deploy.sh --project xxx --project-id 1 --env test --auto-version
./pipeline.sh --pipeline xxx --project-id 1 --env test --auto-version
```

Dashboard「管理 → YAML 导出」把数据库导出到 `data/export/config/`（`--data-dir` 实例为 `<data-dir>/export/config/`），可直接复制为 `config/` 供 yaml 模式使用。

### 10.7 Windows 免安装便携版

面向没有 Python 环境的机器：一个 zip 解压即用，包内置 Python 3.12.10 embeddable 运行时与全部第三方依赖，免安装/免联网/免 pip；解压后双击 `启动平台.bat` 即拉起 Web 操作台（`launcher.py` 从 5678 起自动避让 20 个端口并打开浏览器，`menu.bat` 为交互式菜单）。

出包与发布完整流程（版本号常量 `RELEASE_VERSION`、五阶段门禁 fetch/assemble/leak_check/smoke/zip、三重防泄漏红线、ASCII 发布资产名与 SHA256 校验、双远端 tag 与 `gh release` 收口）见 README「便携版打包与发布（Windows 免安装）」一节。产物目录 `dist-portable/` 已被 `.gitignore` 忽略，不入库；`--skip-smoke` 仅供调试，禁止用于发布。

### 10.8 Java 进程发布（jar_service）

`jar_service` 面向「目标机只跑一个 Java 进程、由 systemd 托管」的后端服务：不构建镜像、不操作容器，jar 按版本目录并存。

前提：

- 目标机为 Linux 且启用 systemd，已安装 JDK/JRE（平台不安装，也不配置 `JAVA_HOME`）；
- `java_bin` 指向目标机上的绝对路径（默认 `/usr/bin/java`）；
- 发布账号可创建/写入 `app_home`；
- systemd 单元由平台生成并接入（管理员操作，见下文），平台不接管人工维护的单元。
- 发布账号需能读取 journal（root 或 `systemd-journal` 组），否则日志采集为空、进程状态仍正常。

配置字段（服务表单「Java 进程」分区，保存在 `services.runtime_config`）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `app_home` | 是 | 应用根目录（绝对路径），如 `/opt/apps/hqb-auth`；其下放 `releases/`、`CURRENT`、`.deploy_version` |
| `java_bin` | 否 | 目标机 Java 绝对路径，默认 `/usr/bin/java` |
| `jvm_opts` | 否 | JVM 参数，如 `-Xms512m -Xmx1g` |
| `app_args` | 否 | 启动参数，如 `--spring.profiles.active=prod` |
| `run_user` | 否 | systemd `User=`；留空以 root 运行（平台不改目标机用户与权限） |
| `work_dir` | 否 | 工作目录；留空默认 `{app_home}/CURRENT` |
| `unit_name` | 否 | systemd 单元名（不含 `.service`）；留空默认同服务名/镜像短名 |
| `port` | 条件 | HTTP/TCP 就绪方式必填，1-65535；仅进程存活方式可留空 |
| `readiness.type` | 否 | `http`（默认）/ `tcp` / `process` |
| `readiness.path` | 否 | HTTP 健康路径，默认 `/actuator/health`，要求响应体含 UP 语义 |
| `readiness.timeout` / `interval` | 否 | 就绪总超时（默认 90s）/ 轮询间隔（默认 5s） |
| `stop_timeout` / `start_timeout` | 否 | systemd `TimeoutStopSec` / `TimeoutStartSec`，默认 60s / 120s |
| `artifact_pattern` | 否 | 可执行 jar 匹配，默认 `target/*.jar`；多命中时排除 sources/javadoc/original 后取体积最大者（fat jar 优先） |
| `env_files` | 否 | 环境变量文件绝对路径数组，写入 `EnvironmentFile=`；文件缺失会导致单元启动失败 |

构建：`jar_service` 固定走 Maven（表单中构建类型锁定）。构建产物拷贝为 `dist/{服务名}-{版本}.jar` 并生成 `.sha256` 旁文件；匹配顺序为项目根 → `build_order` 各模块目录。也可跳过构建，把 jar 与 `.sha256` 手工放入 `dist/`。

单元接入（首次发布前）：

1. 在服务表单填好字段并保存；
2. 点「预览 systemd 单元」，核对 `ExecStart` 是否为 `{java_bin} ... -jar {app_home}/CURRENT/app.jar ...`，以及 `User=`、`EnvironmentFile=`；
3. 管理员发起接入：`POST /api/projects/{项目ID}/services/{服务ID}/unit`（body 可传 `{"env": "prod"}` 指定环境，缺省用项目默认环境）；
4. 接入动作只写 `/etc/systemd/system/{unit}.service`、执行 `daemon-reload` 与 `enable`，不启动单元、不装依赖；每次接入写审计（动作 `onboard_service_unit`）。

发布与回滚：

- 预检：Java/systemd 可用、`app_home` 可写、磁盘余量 ≥ jar 的 1.5 倍；
- 准备：确认单元已接入，且 `ExecStart` 指向 `{app_home}/CURRENT/app.jar`——否则拒绝发布，避免接管人工维护的服务；
- 传输：上传到 `releases/{version}/app.jar`；远端 sha256 相同则跳过，上传后复核校验和；
- 切换：`systemctl stop` → 临时链接 + `mv -Tf` 原子换 `CURRENT` → `systemctl start` → 写 `.deploy_version`；
- 验证：`systemctl is-active`，再按 readiness 探测；失败先抓 `journalctl` 尾部写入任务日志，再触发自动回滚；
- 回滚：链接指回 `releases/{previous_version}` 并重启，不重传 jar；目标机没有该版本目录时跳过并告警。

停机窗口：单实例发布在「停止 → 换链接 → 启动」期间服务不可用，平台不承诺零停机。需要连续可用时请改用容器多实例方案。

失败排查：

- 预检失败：看任务日志中的 Java/systemctl/目录/磁盘提示；
- 「单元未接入」：先执行上面的接入步骤（管理员）；
- 「拒绝接管非平台创建的单元」：现有单元 `ExecStart` 未指向 `CURRENT/app.jar`，请人工确认后重建；
- 未 active 或就绪超时：任务日志已附 `journalctl` 尾部，重点检查 `java_bin`、`env_files` 是否存在、端口占用与 `app_args`。

观测（M5）：

- 设备页 jar 卡片展示实时状态（在线/已停止/未部署/探测失败）、当前版本、在线主机数与运行时长；版本以目标机 `.deploy_version` 为准；
- 点「主机」查看每主机版本、上一版本、PID、运行时长、重启数、CPU/内存；监控关闭时显示「版本快照」，来自 `service_host_state`；
- 点「日志」读取采集器入库的 `journalctl` 日志（采集周期为监控周期的 2 倍，默认 60s；监控关闭时 120s，按 `systemd:<unit>` 独立游标，关键词告警照常生效）；
- 首次采集窗口为最近 5 分钟，之后按游标增量；日志正文中的多行消息逐行保留并带时间戳。

## 十一、Jenkins / Ansible 集成

### 11.1 Jenkins

已内置参数化 Pipeline（`jenkins/Jenkinsfile`），接入步骤详见 `docs/jenkins-setup.md`。核心参数：

| 参数 | 说明 |
| --- | --- |
| `DEPLOY_MODE` | `project`（单服务）/ `pipeline`（流水线） |
| `PROJECT` / `PIPELINE` | 项目名 / 流水线名 |
| `ENV` | 目标环境（prod/test），留空生产 |
| `VERSION` / `PREVIOUS_VERSION` | 版本号（勾选 AUTO_VERSION 时忽略） |
| `AUTO_VERSION` | 自动查询线上版本 patch+1 |
| `SKIP_BUILD` | 跳过 Maven 构建 |
| `CLEAR_SUMMARY` | 发布后清零巡检计数器 |

SSH 密码、飞书/钉钉 webhook 通过 Jenkins Credentials 注入环境变量（`SSH_PASSWORD` / `NOTIFY_FEISHU_WEBHOOK` / `NOTIFY_DINGTALK_WEBHOOK`），构建完成自动推送结果通知。

### 11.2 Ansible

参考 `ansible/playbook.yml` 和 `ansible/inventory.ini`。Ansible 负责主机连通和并发控制，Nacos 与健康检查复用本地 Python 脚本。

## 十二、常见问题

### 12.1 发布前需要手动停止容器吗？

不需要。脚本会通过 SSH 执行 `docker compose stop` 和 `docker compose rm -f` 来自动停止并删除旧容器。

### 12.2 1Panel 还需要操作吗？

不需要。脚本直接修改服务器上的 `docker-compose.yml` 并调用 `docker compose` 命令。1Panel 可以用来查看日志，但发布过程不依赖它。

### 12.3 Nacos 实例下线后为什么还收到流量？

脚本下线后会调用 `wait_for_absence` 等待该 IP 没有健康实例。如果仍有流量，请检查：

- Nacos 中是否还有 Dubbo 服务未下线（补充 `dubbo_services`）。
- 客户端缓存是否过长。
- 是否有其他网关或负载均衡绕过了 Nacos。

### 12.4 健康检查失败怎么办？

脚本会打印失败原因，常见排查点：

- 服务端口未暴露或防火墙拦截。
- Actuator 路径错误（检查 `health_url`）。
- 容器日志出现 `ERROR`、`Exception` 等关键字。
- 健康检查超时过短（调整 `health_timeout`）。

### 12.5 流水线里所有服务都要使用同一个 `--previous-version` 吗？

不是。命令行的 `--previous-version` 只是全局默认值。如果某个服务在 `pipelines.yaml` 中单独配置了 `previous_version`，会优先使用该服务自己的回滚版本号。这样即使微服务之间版本不一致，也能在单条流水线里统一发布。

### 12.6 如何新增一个项目？

1. 在 `config/projects.yaml` 中按现有结构添加项目元数据。
2. 确认 Dockerfile、构建顺序、服务名、健康检查地址正确。
3. 如果是定时任务项目，填写 `snailjob_group`。
4. 如需加入流水线，在 `config/pipelines.yaml` 的对应 stage 中增加服务。

### 12.7 如何新增一条流水线？

1. 在 `config/pipelines.yaml` 中新增流水线名称和 stage。
2. 每个 stage 指定 `parallel` 和 `services`。
3. 通过 `./pipeline.sh --pipeline 新流水线名` 执行。

## 十三、安全与维护建议

- 生产环境务必使用环境变量或密钥管理工具注入密码，不要直接写在配置文件中。
- 第一次使用前，先在测试环境或首台服务器反复验证。
- `sj_job` 备份文件保存在 `./backups/` 目录，请定期清理。
- 建议在 Jenkins Pipeline 中开启审计日志，记录每次发布的版本号、操作人和结果。
- 发布窗口期尽量避开业务高峰，灰度观察时间根据实际业务指标调整。
