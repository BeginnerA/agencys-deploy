# 进程族运行时扩展设计（次期：Go/Rust 单二进制 + Node/Python 进程）

状态：设计已确认，待写实施计划
日期：2026-10-28
范围：发布策略层扩展第二批。依赖首期（jar_service + M5 观测面）已合并的机制，不涉及
Kubernetes/WAR/Windows（后续期）。

## 1. 背景与目标

首期把"进程 + systemd 单元"发布通道做成了 `jar_service`：不可变版本目录 + `CURRENT`
符号链接原子切换、就绪探测、失败回滚、journal 日志采集与每主机版本视图。设计文档
（`2026-10-24-multi-runtime-deploy-design.md` §10）明确次期接入 Go/Rust 单二进制与
Node/Python 进程——它们与 Jar 共用同一适配器形态，只差构建产物来源与启动命令。

目标：把 `jar_service` 的实现泛化为进程族共享基类，新增四种发布方式
（`go_binary` / `rust_binary` / `node_process` / `python_process`），复用全部既有编排、
观测与回滚机制；既有容器、静态、jar 三种方式行为零变化。

已确认的三个决策（2026-10-28 与使用方澄清）：

1. **范围**：四个运行时全做，分两批实施——先 Go/Rust（单文件产物，与 jar 复用度最高），
   后 Node/Python（引入目录产物传输路径）。每批独立可验收。
2. **构建**：平台机构建为主（每种运行时一个 `build_type`），`build_type=none`
   （产物外部投入）作通用兜底。Rust 交叉编译受限，文档引导 Linux/WSL 构建或 `none`。
3. **Python 依赖**：平台管依赖——wheelhouse 离线安装 + 目标机 `app_home/venv`
   独立虚拟环境；目标机不联网、不写系统 site-packages。

适配器架构选型：**方案 A——抽取 `ProcessServiceStrategy` 共享基类，jar 改为薄子类
（行为零变化），四个新模式各为薄子类**。否决了"单一 process_service 模式内嵌 runtime
字段"（破坏 deploy_mode=注册表 key 约定、需迁移存量 jar 服务）与"复制四份实现"
（与 `derive_unit_name()` 统一的初衷背道而驰）。

## 2. 范围与非目标

**做**：四种新 deploy_mode 的端到端发布链路（构建 → 上传 → 切换 → 就绪 → 回滚）、
监控探测 / journal 日志 / 每主机版本视图对进程族全量放行、服务表单与文档。

**不做（非目标）**：

- 目标机仍仅限 Linux + systemd；Windows 服务、macOS 不做。
- 平台不在目标机安装任何系统依赖（JDK/Node/Python/python3-venv 均需预装，文档写明）；
  目标机不联网装依赖。
- 不接管非平台创建的单元（沿用 ExecStart 特征串门槛）。
- 单实例重启必有中断窗口，文档与 UI 不承诺零停机（全局约束延续）。
- Node 只支持"直接 node 入口文件"启动，不支持 `npm start` 等包装器；
  Python 只支持脚本入口文件，不支持 `python -m` 模块启动（两者可作为后续增强）。
- Windows 平台机自动交叉编译 Rust 不做（未配置 target triple 时明确报错引导）。

## 3. 模型与配置

### 3.1 注册表扩展

- `deploy_mode` 新增 4 个 key：`go_binary`、`rust_binary`、`node_process`、
  `python_process`。各对应一个策略文件 + `@register`，`create_strategy()` 路由不变。
- `build_type` 新增 `go` / `rust` / `node` / `python`；既有 `maven`/`frontend`/`none`
  不变。表单联动：`go_binary→go`、`rust_binary→rust`、`node_process→node`、
  `python_process→python`。
- 版本语义全部 semver（`bump_patch_version`），与 jar 一致。

### 3.2 runtime_config：共享字段 + 模式专属字段

共享字段与 jar 完全一致：`app_home`（必填，绝对路径）、`run_user`、`work_dir`、
`unit_name`、`port`、`readiness{type,path,timeout,interval}`、`stop_timeout`（缺省 60）、
`start_timeout`（缺省 120）、`env_files`（绝对路径数组，平台不读取内容）、`app_args`。

| 模式 | 专属字段 | ExecStart 形态 |
| --- | --- | --- |
| `jar_service` | `java_bin`、`jvm_opts`、`artifact_pattern`（现状不变） | `{java_bin} {jvm_opts} -jar CURRENT/app.jar {app_args}` |
| `go_binary` | 无 | `{app_home}/CURRENT/app {app_args}` |
| `rust_binary` | 无 | 同 go |
| `node_process` | `node_bin`（缺省 `/usr/bin/node`）、`start_file`（必填，相对 CURRENT，如 `dist/server.js`） | `{node_bin} CURRENT/{start_file} {app_args}` |
| `python_process` | `requirements`（缺省 `requirements.txt`，相对项目根）、`start_file`（必填）、`python_version`（wheel 解析用，如 `"3.11"`，缺省取平台机版本并在日志提示核对） | `{app_home}/venv/bin/python CURRENT/{start_file} {app_args}` |

**readiness 缺省值按模式分派**：jar 维持现状（`http` + `/actuator/health`，向后兼容）；
其余四种缺省 `type=process`（只判 `systemctl is-active`），因为有健康端点的服务应显式配置
`http`/`tcp` + `port`，没有端点的不应被一个猜出来的默认路径卡住发布。

### 3.3 远端目录约定：与 jar 完全同构

```
{app_home}/releases/{version}/          不可变版本目录
  go/rust:  app                          二进制（transfer 后 chmod 0755）
  jar:      app.jar（现状不变）
  node/python: 解压平铺的完整目录（源码 + 依赖 + .sha256 标记）
{app_home}/releases/{version}.tar.gz    archive 模式传输中的压缩包（解压后删除）
{app_home}/releases/{version}.tmp/      archive 解压暂存（原子 mv 的中转）
{app_home}/CURRENT -> releases/{version}
{app_home}/.deploy_version              版本标记
{app_home}/venv/                        仅 python_process：跨版本共享的虚拟环境
```

unit 的 `ExecStart` 只引用 `CURRENT`（或 venv），发布与回滚永远只是"换链接 + 重启单元"，
不改 unit、不重传产物。`SuccessExitStatus=143` 保留在共享 unit 模板：对装了 SIGTERM
处理器的运行时（Java、部分 Python 应用）有效，对默认被信号终止的运行时无害。

### 3.4 配置解析与校验：`process_config.py`

新建 `deploy_strategies/process_config.py`：共享字段解析（parse）+ 按模式分派的校验器
注册表（validate）。jar 专属字段与缺省值留在 `jar_config.py`，其公共函数签名
（`parse_runtime_config` / `validate_runtime_config` / `derive_unit_name` /
`artifact_filename`）不变，内部改为复用共享层——既有导入与测试不破。
`service_unit.render_unit()` 的 ExecStart 拼装按 3.2 表格钩子化，其余行
（User/WorkingDirectory/EnvironmentFile/Timeout*）共用。

`start_file` 校验：相对路径（不以 `/` 开头）、不含 `..` 段与换行（unit 注入最低防线）。

## 4. 策略层：基类与子类

### 4.1 `deploy_strategies/process_service.py`

`ProcessServiceStrategy(ProcessService 基类)` 从 `jar_service.py` 主体抽出，持有：

- 类属性 `is_process = True`（供 `process_modes()` 收集）。
- 能力标志与 jar 相同：`supports_nacos/canary/prefetch/restart = True`，
  `version_scheme = "semver"`——灰度、滚动、自动回滚、Nacos 上下线由编排层按标志自动生效。
- 钩子（子类必填/可选）：
  - `artifact_kind`：`"file"`（go/rust/jar）| `"archive"`（node/python）
  - `artifact_name(version)`：dist 产物名 `{service}-{version}[.jar|.tar.gz|无扩展名]`
  - `remote_entry(version)`：远端入口路径（`releases/{v}/app`、`app.jar`、None=archive）
  - `exec_start_argv(cfg)`：ExecStart 拼装（3.2 表格）
  - `exec_start_marker()`：接管门槛特征串（prepare 与 `onboard_unit` 共用：
    `{app_home}/CURRENT/app`、`…/app.jar`、`…/CURRENT/{start_file}`、`{app_home}/venv/bin/python`）
  - `runtime_probe(server)`：check 阶段附加探测——jar 验 `java_bin`、node 验 `node_bin`、
    python 验 `python3 -m venv` 可用；go/rust 无
  - `ensure_runtime(server, version)`：版本级运行时保障，prepare（新版本）与
    rollback（目标版本）共用；python 的 venv 保障（§6）挂在这里，其余模式无动作

五阶段行为全部在基类实现，语义与 jar 现状逐字一致：

- `check_local`：dist 产物存在 + `.sha256` 旁文件一致。
- `check`：`systemctl` 存在、`app_home` 可写、磁盘余量（file 模式 ×1.5，archive ×3）、
  `runtime_probe`。
- `transfer`：见 4.2。`prefetch`：并行预传，复用现有实现不动。
- `switch` / `rollback`：停单元 → `mv -Tf` 换链接 → 起单元 → 写 `.deploy_version`。
- `verify`：`is-active` → readiness 循环（http/tcp/process）→ 失败先抓 journal 尾部。
- `rollback_source_missing`：`releases/{previous}` 不存在即判该主机回滚失败。

### 4.2 transfer 的两条路径

**file**（go/rust/jar）：与 jar 现状完全一致——SFTP 上传到 `releases/{v}/app`，
远端 `sha256sum` 复核，已存在且相同则跳过。

**archive**（node/python）：

1. 上传 `{svc}-{v}.tar.gz` → `releases/{v}.tar.gz`，远端 sha256 复核；
2. `rm -rf releases/{v}.tmp` → `mkdir` → `tar -xzf` 解压平铺；
3. 写入 `.sha256`（压缩包校验和）到解压目录；node 额外
   `chmod -R u+x node_modules/.bin 2>/dev/null || true`（Windows 打包丢执行位的保险）；
4. `mv -Tf {v}.tmp {v}` 原子就位（同文件系统）→ 删除 `{v}.tar.gz`；
5. 幂等：`{v}` 已存在且 `.sha256` 匹配 → 跳过；存在但缺失/不匹配 → 视为上次发布中断
   残骸，`rm -rf {v}` 重建并在日志说明（该目录的唯一写入者就是本次 transfer，
   不违反"不可变"约定）。

## 5. 构建与依赖（scripts/build.py）

工具链定位仿 `find_mvn()`：`build.go_home` / `build.cargo_home` / PATH，缺失时给出
可操作的报错。产物统一落 `dist/`（复用 `deploy.dist_dir`），并生成 `.sha256` 旁文件；
`build_type=none` 跳过构建只做产物校验（现状不变）。

| build_type | 平台机动作 | 产物 |
| --- | --- | --- |
| `go` | `CGO_ENABLED=0 GOOS=linux GOARCH={build.go_arch，缺省 amd64} go build {可选 ldflags} -o dist/{svc}-{ver} {build.go_main，缺省 .}` | `{svc}-{ver}` + `.sha256` |
| `rust` | `cargo build --release --target {build.rust_target}`；Windows 主机未配置 triple 时明确报错（引导配 triple、WSL 或 none）；产物从 `target/{triple}/release/{build.rust_bin，缺省服务名}` 拷出 | `{svc}-{ver}` + `.sha256` |
| `node` | `npm ci`（全量）→ 存在 `scripts.build` 则 `npm run build` → `npm prune --omit=dev` → stdlib `tarfile` 打包项目目录（含 node_modules，排除 `.git`、`.env*`、`*.log`、`node_modules/.cache`） | `{svc}-{ver}.tar.gz` + `.sha256` |
| `python` | `pip download -r {requirements} -d {临时 wheels 目录} --platform manylinux2014_x86_64 --only-binary=:all: --implementation cp --python-version {runtime_config.python_version}` | `{svc}-{ver}.tar.gz`（源码 + `requirements.txt` + `wheels/`）+ `.sha256` |

- `python` 的 `--only-binary=:all:` 使 sdist（无 Linux wheel、需现场编译的包）在
  download 阶段即失败并列出包名，不悄悄放过。`--platform` 取 manylinux 兼容集，
  实施时按目标机 glibc 选型（manylinux2014 为保守缺省）。
- `node` 的 `npm ci` 要求 `package-lock.json`（文档写明前提）。
- 全程不执行任何 docker 命令（与 jar 分支同口径，测试断言 docker 调用计数为 0）。

## 6. Python venv 生命周期（平台管依赖）

wheels 随版本不可变保存在 `releases/{v}/wheels/`；venv 是 `app_home` 级共享资产：

1. `prepare` 阶段（变更前，失败即抛、不动旧版）：
   `{app_home}/venv/pyvenv.cfg` 不存在 → `python3 -m venv {app_home}/venv`；
2. 计算 requirements 内容 sha256 → 与 `{app_home}/venv/.req_hash` 比对：一致则跳过；
3. 不一致 → `venv/bin/pip install --no-index --find-links releases/{v}/wheels
   -r releases/{v}/requirements.txt` 离线安装 → 写入新 `.req_hash`；
4. 回滚到旧版本时同一套判定：`.req_hash` 不同 → 先用该版本
   `releases/{old}/wheels/` 重装再重启，耗时写入任务日志，不静默。

目标机前提（文档写明，平台不代装）：`python3` + `python3-venv`（含 ensurepip）。
venv 创建/安装只发生在 `app_home` 内，不写系统 site-packages、不联网。

## 7. 观测与编排收敛

进程族模式清单由注册表自动收集，不手工维护：`base.py` 增加
`process_modes() -> list`（遍历 `_REGISTRY`，取 `getattr(cls, "is_process", False)`），
`deploy_strategies/__init__.py` 导出。

替换点（行为：四种新模式获得与 jar 完全相同的观测面）：

- `webapp/monitor.py` 约 5 处 `deploy_mode == "jar_service"` → `in process_modes()`；
  进程探测（`jar_probe.py`，按 unit 探 PID/RSS/CPU）与 journal 采集本身按 unit 工作，
  逻辑不动。
- `webapp/log_collector.py`：journal 采集 SQL 门（`deploy_mode='jar_service'`）与
  `_load_jar_services` 等判定 → 进程族；采集键仍为 `systemd:<unit>`，
  归档清理按 `env + file_path` 匹配的 M5 结论不受影响。
- `webapp/routes/config_routes.py`：每主机版本 API 的"非进程模式拒绝"校验、
  重启任务的单元支持 → `in process_modes()`。
- `webapp/deploy.py`：`service_host_state` 回写键（`service_name or name`）不变，
  触发条件放行进程族。

文件名不改（`jar_probe.py` / `jar_hosts.js` 等历史命名保留）；触碰到的文件内部，
函数/变量命名顺手泛化（如 `_load_jar_services` → `_load_process_services`），
不做专门的全局重命名。

发布编排层（`deploy.Deployer`、`deploy_pipeline.py` 的 `--auto-version` 策略分发、
金丝雀/审批/通知）零改动——只认能力标志与策略接口。

## 8. 前端与 API

- `config-service-form.js`：下拉新增 4 项并联动 `build_type`；runtime_config 结构化
  字段组 = 共享组（app_home/run_user/unit_name/port/readiness/超时/env_files/app_args）
  + 模式专属组，与现有 jar 表单同形态；镜像短名与 Git 仓库降为可选（与 jar 一致）。
- `webapp/static/js/jar_hosts.js` 与设备页：`jarHostTableHTML` 直接复用；
  文案中"jar 服务"泛化为"进程服务"，状态标签/快照提示不变。
- 导入导出：`runtime_config` 已是 JSON 透传，零改动（insert-only 语义保持）。
- README / `docs/usage.md`：新增四种方式的说明与前提（go/cargo/npm/pip 平台机构建前提；
  目标机 Node/Python/python3-venv 前提；Rust 交叉编译限制；停机窗口提示）。

## 9. 测试与验收

全部离线，沿用既有约束：桩 SSH/SFTP/systemctl/journalctl/docker 与 go/cargo/npm/pip
子进程；`tempfile.mkdtemp()`、`create_app(data_dir=...)` 注入，不读写仓库 `data/`；
不新增第三方依赖；`scripts/quality_gate.py` 中 ruff/node 缺失时 SKIP；只做本地 commit，
不执行 push。

新增覆盖：

- 构建：四种 build_type 用记录命令的桩断言命令行、环境变量（GOOS/GOARCH）、产物与
  `.sha256`；`pip download` 缺 wheel 时报错列包名；docker 调用计数为 0。
- transfer：file/archive 两路径 + 幂等（同 sha256 跳过、半包复核失败、`.tmp` 残骸重建、
  `.sha256` 标记写入）。
- Python：venv 首次创建 / requirements 变更重装 / 回滚时按 hash 重装；venv 创建失败
  在 prepare 抛错且不触达旧版。
- 策略：四模式五阶段集成（假 `RemoteServer`），断言 switch 不含 docker 调用、
  ExecStart 特征串门槛拒绝非平台单元、rollback 缺源告警跳过。
- 观测放行：monitor / log_collector / hosts API 对四种新模式生效；jar 既有用例
  全量零回归（基类抽取的回归门）。
- 前端：`node --check` + 无头 JS 用例（表单四模式显隐、build_type 联动）。
- `jar_config` 公共函数签名不变性的回归。

验收标准：

1. 配置为四种新方式的任一服务，均可完成"构建 → 上传 → 切换 → 就绪 →（失败）自动回滚"
   全链路，目标机零 docker 调用。
2. 既有 container / static_archive / jar_service 的全部行为与改动前一致
   （同一组回归用例证明，基类抽取不改语义）。
3. Python 服务发布后：venv 在 `app_home` 内、目标机无外网访问记录、系统目录零写入。
4. Node/Python 回滚到 requirements 不同的旧版本时自动重装 venv 并在任务日志可见。
5. 四种新模式在设备页具备与 jar 相同的实时状态、journal 日志与每主机版本视图。
6. 停机窗口提示出现在文档与表单文案，不承诺零停机。

## 10. 实施里程碑

两批各自独立验收，每步 `quality_gate.py` 全绿、既有容器/静态/jar 用例零倒退：

**批 A（单文件产物：Go/Rust）**

1. **M1 基类抽取**：`process_service.py` 基类 + `process_config.py` 共享层 +
   `jar_service` 子类化 + `process_modes()`；764 个既有测试为回归门。
2. **M2 Go/Rust**：`build.py` 的 `go`/`rust` 分支、两个策略子类、`service_unit`
   ExecStart 钩子化、配置校验、表单接入。
3. **M3 收敛与文档**：观测/hosts/重启放行、文案泛化、README/usage、全量回归。

**批 B（目录产物：Node/Python）**

4. **M4 archive 传输**：`artifact_kind=archive` 的 transfer/prefetch 路径与幂等。
5. **M5 Node**：`node` 构建分支（ci/build/prune/tar）+ `node_process` 策略 + 表单。
6. **M6 Python**：`python` wheelhouse 构建 + `python_process` 策略 + venv 生命周期
   （prepare/rollback）。
7. **M7 收尾**：表单/文档补齐、全量回归、quality gate。

## 11. 风险与遗留

- **Rust 交叉编译**：Windows 平台机缺省不可用（报错引导），文档给出 WSL/Linux/none
  三条路；不在本期做工具链自动化。
- **manylinux 选型**：`--platform` 缺省 manylinux2014，若目标机更老（CentOS 7 以下）
  或包无兼容 wheel，会构建失败并明示包名——属可接受失败面，不引入平台侧编译。
- **venv 与回滚**：requirements 变化时回滚有一次性重装耗时（分钟级，取决于 wheel 数量），
  任务日志可见；不阻塞切换语义。
- **Node 执行位**：Windows 打 tar 丢失 +x 已用解压后 chmod 兜底；若发现其它
  权限敏感场景（setuid 等）不支持，属非目标。
- **`python -m` / `npm start` 启动形态**：本期不支持（见 §2），若使用方提出再作为
  独立增强评估。
