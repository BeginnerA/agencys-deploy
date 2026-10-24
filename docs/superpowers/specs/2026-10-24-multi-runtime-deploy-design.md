# 多运行时发布管理扩展设计（首期：Java 可执行 JAR + Linux systemd）

状态：设计已确认，待写实施计划
日期：2026-10-24
范围：发布策略层扩展，不涉及 Kubernetes/WAR/Windows（后续期）

## 1. 背景与目标

平台当前的服务端发布只覆盖 Docker 容器（`deploy_mode=container`）与静态站点压缩包
（`deploy_mode=static_archive`）。纯 Java 可执行 JAR 这类"进程 + systemd 单元"的形态没有
发布通道，只能手工 scp + 手工重启，缺少版本留档、原子切换、就绪判定、失败回滚和审计。

目标：把发布能力从"容器专用"扩展为"通用编排 + 运行时适配器"，首期落地 Linux systemd 托管的
Java 可执行 JAR，且不改变现有容器与静态发布的任何行为。

非目标（首期明确排除）：安装 JDK 或任何系统依赖、接管平台上非自己发布的既有 Java 进程、
免授权的 sudo/系统配置改写、数据库回滚、单实例发布的零停机保证（单实例重启必然有中断窗口）。

## 2. 概念模型：三轴分离

现有配置把构建方式和发布方式混在 `build_type` 与 `deploy_mode` 两个字段里。扩展前先把语义固定：

- `build_type`（构建方式）：`maven` / `frontend`，新增 `none`（产物由外部投入，平台只负责发布）
- `artifact`（产物形态）：`image` / `archive` / `jar`，由 `deploy_mode` 推导，**不作为独立配置项**，
  避免两处填写互相矛盾
- `deploy_mode`（运行时方式 = 发布策略注册表 key）：`container` / `static_archive`，新增 `jar_service`

`deploy_mode` 仍是 `deploy_strategies.base.create_strategy()` 的唯一路由入口。新增一种运行方式
= 新增一个策略文件 + `@register`，不改 `Deployer` 核心。这条既有约定原样保留。

## 3. 配置与数据模型

### 3.1 services 表新增一个 JSON 列

```sql
ALTER TABLE services ADD COLUMN runtime_config TEXT DEFAULT '{}'
```

不继续加标量列。静态发布那轮已经为 `deploy_mode=static_archive` 加了 6 个标量列
（`site_root`/`backup_dir`/`static_dist_dir`/`zip_prefix`/`entry_file`/`file_mode`），Jar 的字段更多
（JVM 参数、启动用户、单元名、超时、环境文件引用等），继续加标量会让 `services` 变成
"每种发布方式各占一列"的形态。库里 `environments.registry/nacos/ssh/build/deploy` 与
`alert_rules.scope` 已经是 JSON 文本列的既有惯例，策略专属配置收敛到一个 JSON 列更一致，
后续 Go/WAR 接入不再触发迁移。

`deploy_mode` 本身保持标量列——它是路由字段，需要可查询、可校验。
**既有静态列不做迁移重构**：已经过验证、已有存量数据与前端表单，改动收益为负。

### 3.2 runtime_config（jar_service）字段约定

```json
{
  "app_home": "/opt/apps/hqb-auth",
  "java_bin": "/usr/bin/java",
  "jvm_opts": "-Xms512m -Xmx1g -XX:+UseG1GC",
  "app_args": "--spring.profiles.active=prod",
  "run_user": "appuser",
  "work_dir": "",
  "unit_name": "hqb-auth",
  "port": 8080,
  "readiness": {"type": "http", "path": "/actuator/health", "timeout": 90, "interval": 5},
  "stop_timeout": 60,
  "start_timeout": 120,
  "artifact_pattern": "target/*-exec.jar",
  "env_files": ["/opt/apps/hqb-auth/.env"]
}
```

- `app_home` 必填，是所有版本目录与符号链接的根
- `work_dir` 缺省取 `CURRENT` 目录（Spring Boot 相对路径配置文件的常见期望位置）
- `readiness.type` 取 `http` / `tcp` / `process`；`process` 表示只判定 `systemctl is-active`
- `env_files` 只记录路径引用，内容由 unit 的 `EnvironmentFile=` 加载，平台不读取、不入库、
  不在日志回显（沿用凭据脱敏的既有约束）
- `artifact_pattern` 是 Maven 构建后在 `target/` 定位可执行 jar 的 glob（默认
  `target/*.jar`，但需排除 `*-sources.jar` 与 `*-original.jar`；Spring Boot 项目推荐
  `target/*-exec.jar` 或 `target/*.jar` 中最先命中的 repackaged 产物，见 6.1）
- 校验在 `config_routes` 写入时执行：必填项、路径为绝对路径、`port` 范围、`readiness.timeout` 为正数

### 3.3 远端目录约定：不可变发布 + 符号链接原子切换

```
{app_home}/releases/{version}/app.jar        每个版本一份，永不覆盖
{app_home}/releases/{version}/.sha256        上传后远端复核用
{app_home}/CURRENT -> releases/{version}     unit 只引用 CURRENT
{app_home}/.deploy_version                   版本标记，与静态策略的标记语义对齐
```

systemd unit 的 `ExecStart` 固定指向 `{app_home}/CURRENT/app.jar`，因此"发布/回滚"永远只是
换链接 + 重启单元，不涉及 unit 改写，回滚是秒级且不需要重传产物。

### 3.4 主机状态的权威来源

**权威状态在主机上**（`CURRENT` 链接 + `.deploy_version` 标记），不在平台库里。这与
`static_archive` 策略的 `query_current_version()` 完全同构，避免平台库与真实机器漂移。

平台侧新增缓存/决策表：

```sql
CREATE TABLE IF NOT EXISTS service_host_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    env TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL,
    host TEXT NOT NULL,
    current_version TEXT DEFAULT '',
    previous_version TEXT DEFAULT '',
    last_task_id INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL,
    UNIQUE(project_id, env, service, host)
);
```

写入时机：发布或回滚任务成功后由编排层回写。用途：设备页与服务详情页展示"每台主机当前跑哪个
版本"、回滚页列出可选目标。**查询线上版本仍以 SSH 读标记为准**，该表不作为发布决策的唯一依据。

### 3.5 本地产物契约

`dist/{service}-{version}.jar` + 同名 `.sha256` 旁文件。目录复用既有部署配置
`deploy.dist_dir`、`deploy.remote_tmp_dir`、`deploy.upload_streams`，不新造目录体系与并发上传实现。

## 4. 策略适配器：`deploy_strategies/jar_service.py`

能力标志：`supports_nacos=True`、`supports_canary=True`、`supports_prefetch=True`、
`supports_restart=True`、`version_scheme="semver"`。

`supports_nacos` 与运行时方式无关——Jar 服务同样注册到 Nacos，优雅上下线仍由编排层按能力标志
调度，这是既有的"Nacos 独立于发布方式"设计意图。实施时需在 `Deployer` 里确认所有 Nacos 分支
都以 `strategy.supports_nacos` 为门，而不是以镜像/compose 为副作用条件。

五阶段映射：

| 阶段 | 行为 | 变更前/后 |
| --- | --- | --- |
| `check_local` | 本地 jar 存在且 sha256 与旁文件一致 | 变更前，失败即抛 |
| `query_current_version` | SSH 首台读 `{app_home}/.deploy_version`；无标记返回 None（首次发布） | 只读 |
| `next_version` | semver patch 递增，复用既有 `bump_patch_version` | 纯计算 |
| `check` | `{java_bin} -version` 可执行、`systemctl` 存在、`app_home` 可写、目标盘剩余空间 ≥ 包大小 × 1.5 | 只读探测；`mkdir -p` 幂等建目录 |
| `prepare` | 幂等确认 unit 已安装且 `ExecStart` 指向 `CURRENT`；缺失时**不自动写系统**，走第 7 节的单元接入流程 | 变更前，缺失即抛 |
| `transfer` | SFTP 上传到 `releases/{version}/app.jar`；远端已存在且 sha256 相同则跳过（与静态包"同尺寸跳过"同构，但用校验和更严） | 只写新版本目录，不动 CURRENT |
| `prefetch` | 覆盖基类，多主机并行预传（`ThreadPoolExecutor`，复用静态策略的实现骨架） | 同上 |
| `switch` | 置位变更标志 → `systemctl stop` → `ln -sfn releases/{v} CURRENT.tmp && mv -Tf CURRENT.tmp CURRENT` → `systemctl start` | 换链接起即已变更 |
| `verify` | `systemctl is-active` → readiness 循环 → 失败时 `journalctl -u {unit} --since=<切换时刻>` 抓尾部日志入任务日志后抛错 | 判定 |
| `rollback` | 链接指回 `releases/{previous_version}` + 重启 + 同样的 verify；历代包不在本机则报"该主机无回滚源"并告警跳过（与静态策略一致） | 已变更 |

### 4.1 一处命名修正

`deployer.container_modified` 现在的语义是"目标状态已被修改，失败时编排层必须尝试回滚"
（`base.py` 模块文档明确写了这条一致性约定），对 Jar/静态场景是误导。改名为
`target_modified`，并保留 `container_modified` 作为 property 别名以兼容既有调用点与测试，
后续再逐步淘汰。这是本次扩展唯一触碰共享编排层语义的动作，风险低且提升可读性。

## 5. 发布与回滚状态机

任务级复用现有 `tasks.status`，**不新增状态枚举**。变化在主机级：

单台主机一次 switch 的内部序列是 `stop → 换链接 → start → 就绪`。只有进入"换链接"才置变更标志；
换链接之前的失败保持旧版本原状，仅恢复 Nacos 上线——沿用现有约定。

失败处置优先级：

1. verify 失败且 `releases/{previous_version}` 存在 → 自动回滚到上一版，任务记 `failed`
   （已回滚）并发告警。
2. 上一版目录缺失 → **不回滚**（无从可滚），保持现状，任务记 `failed`，日志给出
   `主机 + unit + 手动恢复命令`。
3. 滚动发布中某台失败 → 只回滚已变更的主机，未触达主机保持旧版本（与现有滚动行为一致）。
4. 金丝雀 → 复用现有批次决策文件（`canary_decision_<task_id>.json`，实例数据目录解析）与人工卡点；
   Jar 只把"停容器"替换为"停单元"。
5. 显式回滚任务 → 逐主机检查目标版本包是否存在，缺包主机单独列出让操作者决定重传还是跳过，
   不静默成功（对齐 P1-5 已修的"灰度停止/回滚结果语义"结论）。

## 6. 构建、流水线与观测的联动改造

### 6.1 `scripts/build.py`

当前分支逻辑是 `deploy_mode == "static_archive"` 走 zip，否则一律 `docker build`（`is_static`
之后的代码直接拼 `docker build` 命令）。需要改为真正的产物分支：

- `jar_service`：Maven `package` 完成后，按 `runtime_config.artifact_pattern`（见 3.2）在 `target/`
  定位可执行 jar；glob 命中 0 个则报错并列出 `target/` 实际内容，命中多个则取体积最大者
  （repackaged fat jar 必然大于thin jar），并在日志记录选中结果；排除
  `*-sources.jar` / `*-javadoc.jar` / `*-original.jar`。选中后拷为
  `dist/{service}-{version}.jar` 并生成 `.sha256`，全程不执行任何 docker 命令
- `build_type=none`：跳过构建，仅校验 `dist/` 下产物存在且校验和可生成
- `container` / `static_archive` 路径行为不变

### 6.2 `deploy_pipeline.py` 的 `--auto-version`

现在硬绑 `get_current_remote_version()`（内部是 `docker ps`）与 `bump_patch_version()`。流水线里
混入一个 Jar 服务就会取到错版本或直接报错。改为按服务的 `deploy_mode` 分发到
`strategy.query_current_version()` / `strategy.next_version()`，与 `Deployer` 的策略路由保持一致。

### 6.3 观测去 Docker 化

`webapp/monitor.py` 已经按 `deploy_mode` 跳过 `static_archive` 服务（第 952、983 行），同样的
分支骨架扩展到 `jar_service`：新增基于 `systemctl show -p MainPID,MemoryCurrent,CPUUsageNSec`
与 `/proc/{pid}` 的进程指标（RSS、CPU 时间增量、主进程存活）。注意 systemd 属性名大小写敏感，
实际为 `CPUUsageNSec`（部分版本提供 `CPUUsageUsec`），实施时以真机 `systemctl show` 输出为准。

`webapp/log_collector.py` 目前是 `docker logs --since -t` 的增量拉取 + FTS5 索引 +
`log_scan_state` 游标。新增 `journalctl --unit={unit} --since=<游标> --output=json` 分支，解析后
进**同一套** `log_entries` / `log_scan_state` 增量管道与归档清理，时间戳游标的含边界去重语义
（现有代码已专门处理过 `--since` 的 `>=` 重复问题）在 journal 分支同样适用。

`alert_rules` / 通知链路 / 发布窗口 / 定时计划无需改动，它们面向服务而非运行时。

## 7. 权限、单元接入与审计

systemd 单元首次接入是这套设计里**唯一会写目标机系统配置**的动作，单独管控：

1. 平台按 `runtime_config` 生成 unit 内容，在配置页完整预览（含 `User=`、`EnvironmentFile=`、
   `ExecStart=`）。
2. 只有 admin 角色可发起接入，且必须逐次确认；operator 只能发布已经接入的服务。
3. 执行内容固定为：写 `/etc/systemd/system/{unit_name}.service` → `systemctl daemon-reload` →
   `systemctl enable {unit_name}`，不做任何其它系统改写，不装 JDK、不改内核参数、不动防火墙。
4. 每次接入写 `audit_logs`（动作 `service_unit_onboard`，detail 存 unit 内容哈希与目标主机）。
5. 平台对**非自己创建**的单元拒绝接管：`ExecStart` 不含 `{app_home}/CURRENT/app.jar` 时报错，
   要求显式确认或改由人工接入——这是"不接管既有 Java 进程"承诺的技术落点。

发布与回滚复用既有的角色矩阵（admin/operator/viewer）与审批链路，不新增权限维度。

## 8. 前端与 API

- `webapp/routes/config_routes.py`：`deploy_mode` 校验从硬编码元组 `("container", "static_archive")`
  （第 234、314 行两处）改为读策略注册表的 key 集合，避免每加一种方式改两处；`allowed` 字段集
  增加 `runtime_config`，写入前做 JSON 结构与必填项校验。
- `webapp/importer.py` / `webapp/exporter.py`：`runtime_config` 的 YAML 往返（导出时与
  `deploy_mode` 一样仅在非默认时输出，保持配置文件可读）。
- `static/js/config-service-form.js`：`deploy_mode` 增加"纯 Java 进程（systemd）"选项，选中后显示
  `runtime_config` 字段组、隐藏镜像/compose/站点字段；容器与静态分支保持不变。
- `static/js/devices.js` / `deploy_schedules.js`：按 `deploy_mode` 控制文案与入口
  （重启/回滚/金丝雀可用性由策略能力标志驱动，不再由前端硬判）。
- 服务列表与详情页增加运行时方式徽标；回滚页展示 `service_host_state` 的每主机版本视图。

## 9. 测试与验收

全部离线：SSH/SFTP/docker/systemctl 均以桩替换，只用临时库与临时目录，遵循既有测试隔离约束
（`tests/conftest.py` 的会话级默认目录守卫 + 显式 `create_app(data_dir=...)` 注入）。

- 策略纯逻辑单测：目录命名与链接命令拼装、semver 推导、sha256 校验、`runtime_config` 校验分支。
- 五阶段集成测试（记录命令的假 `RemoteServer`）：断言 `switch` 是"换链接 + systemctl 重启"且
  不含任何 docker 调用；断言 `transfer` 命中相同 sha256 时跳过上传；断言 `previous_version`
  包缺失时 `rollback` 不改动主机且返回明确告警；断言 `verify` 失败会触发一次回滚。
- 编排回归：既有容器/静态的全部测试用例行为不变，全量套件基线（当前 644 passed）不得倒退。
- 构建测试：`build_type=maven + deploy_mode=jar_service` 产出 `dist/{svc}-{ver}.jar` 与 `.sha256`，
  且桩化的 `docker` 调用计数为 0。
- 流水线测试：混合流水线（container + jar）在 `--auto-version` 下各自走对版本推导。
- 前端：`node --check` 与既有无头 JS 用例通过；表单在三种 `deploy_mode` 下字段显隐正确。
- `scripts/quality_gate.py` 全绿。

验收标准：

1. 一个 Maven 项目配置成 `jar_service` 后，可从平台完成"构建 → 上传 → 切换 → 就绪"全链路，
   目标机上不产生任何 docker 相关调用。
2. 连续发布 3 个版本后，`releases/` 下三份产物齐全、`CURRENT` 指向最新，
   任意主机可单独回滚到任一历史版本且耗时在秒级（无重传）。
3. 启动失败（人为配错 JVM 参数）时：任务判失败、journal 关键错误进任务日志、主机自动回到
   上一版本、告警送达。
4. 单实例场景重启存在中断窗口是预期行为，日志与前端要明确提示，不得伪装成零停机。
5. 未接入单元的服务无法发布，接入动作需 admin 逐次确认并留下审计。
6. 现有容器与静态发布任务在同一次部署中行为与改动前一致（用同一组回归用例证明）。

## 10. 分期

1. 首期（本设计）：Java 可执行 JAR + systemd，含编排层去 Docker 化（构建分支、流水线版本推导、
   监控与日志分支）。
2. 次期：Go/Rust 单二进制、Node/Python 进程——它们与 Jar 共用 `process_service` 适配器形态，
   只需换构建产物来源与启动命令。
3. 后期：WAR/Tomcat 容器实例托管、Windows 服务。
4. 独立扩展：Kubernetes/Helm 作为另一种编排后端，与本机进程发布不是同一抽象，不塞进策略层。
