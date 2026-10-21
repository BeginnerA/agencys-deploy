# AI 智能运维助手（Agent 演进）

在 Web 操作台（发布 / 监控 / 告警）之上叠加的 AI 层，分四个阶段渐进落地：

| 阶段 | 能力 | 入口 |
| --- | --- | --- |
| Phase 1 | 只读智能运维助手（LLM + 工具 + 对话） | Web「智能助手」/ CLI / MCP |
| Phase 2 | 决策点 AI 化（灰度 / 失败归因 / 告警归因 / 发布预检） | 建议调度 + Web 展示 + 灰度决策页挂接 |
| Phase 3 | 发布执行 Agent（自然语言 → 计划 → 人工审批 → 执行 → 审计） | Web 审批单 / 对话生成计划 |
| Phase 4 | MCP Server + 长期记忆故障案例库 | MCP stdio / 案例卡片 / 检索复用 |

设计红线：**审批永远人工**；执行**复用现有 tasks 通道**（不绕过发布流程）；LLM 只做语言组织与补充推理，规则引擎保证基础结论；LLM 网关不可用时**自动降级**为纯规则，功能不中断。

## 一、模块结构

```
agent/
├── config.py      LLM 网关配置（环境变量 > platform_settings 表 > 默认值）
├── llm.py         OpenAI 兼容 function-calling 客户端（零第三方依赖，requests 直连）
├── tools.py       49 个运维工具注册表（read/write 两级权限）
├── core.py        对话编排（ReAct 循环：模型思考 → 调工具 → 收敛；批量/SSE 流式双通道）
├── advisor.py     Phase 2 决策引擎：灰度建议 / 失败归因 / 告警归因 / 预检 / 案例沉淀
├── executor.py    Phase 3 发布执行：计划生成 / 审批状态机 / 任务提交 / 后台同步
├── memory.py      Phase 4 长期记忆：故障案例 CRUD + 字符 bigram 相似检索
├── digest.py      周期巡检日报：健康/告警/发布/到期聚合 → 通知渠道定时推送
├── scheduler.py   后台建议调度（daemon 线程，默认 30s 一轮；顺带检查日报到点）
├── routes.py      Web API（/api/agent/*，29 条，RBAC 对齐平台）
├── cli.py         终端对话入口（交互式 / --once 单轮）
└── mcp_server.py  MCP stdio server（JSON-RPC 2.0，零依赖）
```

数据落库（`data/platform.db` 自动建表）：`agent_runs`（审批单）、`agent_messages`（会话）、`agent_advice`（建议）、`agent_cases`（案例）、`platform_settings`（网关配置）。

## 二、快速开始

### 1. 配置 LLM 网关

任选其一（环境变量优先）：

```powershell
# 方式 A：环境变量（推荐，密钥不进库）
$env:AGENT_LLM_BASE_URL = "https://your-gateway/v1"   # 任意 OpenAI 兼容端点
$env:AGENT_LLM_API_KEY  = "sk-xxx"
$env:AGENT_LLM_MODEL    = "qwen-max"                  # 可省略，由服务端决定
```

```powershell
# 方式 B：Web「智能助手 → 网关配置」（admin 可编辑，持久化到 platform_settings）
```

### 2. 启动 Web（含建议调度）

```powershell
python webapp/app.py
# [agent] AI 建议调度已启动，周期 30s
```

- `--agent-interval 0` 关闭 AI 建议调度（灰度/失败/告警自动归因将不运行，对话与预检仍可用）
- 建议调度随监控（`--monitor-interval`）开启而启动

### 3. 三种使用入口

| 入口 | 方式 |
| --- | --- |
| Web 对话 | 浏览器 `http://127.0.0.1:5678/#/agent`（侧栏「智能运维 → 智能助手」），含 对话/审批单/建议/案例 四个页签；另有全站右下角**悬浮智能助手**（独立 float 会话，任意页面随时对话，进入智能助手页自动隐藏避免重复） |
| CLI | `python agent/cli.py --once "auth 服务最近有异常吗"` |
| MCP | `python agent/mcp_server.py --db data/platform.db --role operator`（stdio，可接入支持 MCP 的客户端） |

## 三、Phase 1：只读智能运维助手

- **对话**：登录用户在「智能助手」页与 LLM 多轮对话，回复**流式输出**（SSE 打字机效果，工具调用过程可见）；消息持久化到 `agent_messages`，可在 API/页面回溯（会话按用户隔离）。CLI/MCP 入口同样支持流式/批量两种形态。
- **会话管理**：对话页支持多会话（下拉切换/新建/重命名/置顶/删除/搜索，`localStorage` 记住当前会话）；`float` 会话由全站右下角悬浮助手产生，也可在此管理。会话标题自动取首条用户消息前 40 字，可手动重命名（存 `agent_session_meta`，空名恢复自动标题）；置顶（pinned，同表存列）在列表排序中优先于最近活动，仅本人可操作；搜索（`q` 参数）按标题/最后消息模糊过滤，前端搜索框 300ms 防抖触发；删除会话同步清理元数据（防孤儿行）。列表/删除/重命名/置顶仅 operator/admin（默认自己的会话；admin 可 `scope=all`/`user_id=` 跨用户管理，含 CLI 的 id=0 会话），消息回溯同样按用户隔离。
- **工具**：24 个只读工具 + 25 个写工具（写工具 operator/admin 可见），全部以 JSON 结果回给模型：
  - 只读：`list_projects / get_project / list_services / list_environments / env_servers / service_status / monitor_metrics / service_topology / recent_tasks / task_detail / list_alerts / alert_detail / search_logs / logs_context / release_window / canary_configs / cert_monitors / audit_logs / search_cases / case_detail / advice_list / pending_approvals / run_detail / live_status`
  - 写：`analyze_alert / precheck / ack_alert / plan_operation / decide_run / case_save / case_update` + 平台配置管理与闭环：`manage_projects`（项目 CRUD，仅 admin）、`manage_services`（服务注册 CRUD，仅 admin；git_repo 支持远程 URL）、`manage_environments`（环境 CRUD + 跨项目 clone 含服务器绑定，仅 admin；ssh 口令输出脱敏）、`manage_servers`（服务器注册/绑定/解绑，仅 admin；bind 为 upsert，口令入参不回显；test 为 SSH 连通测试，服务器级/环境级凭据回退）、`manage_pipelines`（发布流水线 CRUD，仅 admin；stages 双形态——服务名数组/嵌套数组/原生对象，悬空引用创建侧拒绝）、`manage_schedules`（定时计划 CRUD/启停/立即执行）、`manage_windows`（发布窗口 CRUD，仅 admin）、`manage_canary`（灰度策略 CRUD/设默认）、`canary_decision`（灰度运行中决策 continue/stop/rollback）、`set_alert_status`（告警 resolve/reopen）、`manage_alert_rules`（告警规则 CRUD/启停；校验镜像 Web 端含跨字段约束，update 合并复校验，scope.ids 悬空拒绝）、`manage_silences`（告警静默窗口 CRUD；one_time epoch / recurring HH:MM+weekdays，scope 悬空拒绝）、`manage_log_storage`（日志存储治理，仅 admin：status 存储概况/settings 保留期 1~365/cleanup 手动清理与每日自动维护同一逻辑）、`manage_dependencies`（服务依赖关系 CRUD，operator/admin；create 含环检测、服务名悬空拒绝，配合 service_topology 查影响面与发布顺序）、`manage_monitors`（到期监控管理，operator/admin：证书/域名/云续费/license/自定义到期项 CRUD/启停；domain 创建自动 WHOIS，check 手动探测（ssl 探测证书/domain 刷新 WHOIS），whois 临时查询域名到期，config 全局默认阈值写仅 admin）、`refresh_monitor`（立即触发一轮监控采集刷新，operator/admin）、`manage_config_backup`（仅 admin：action=export 将 DB 配置全量导出为 YAML / action=import 从 config/ 目录逐文件回灌）、`send_digest`（立即推送巡检日报）
- **对话式项目接入端到端**：对话中给 git 地址与目标服务器即可走通「建项目 → 注册服务 → 建环境 → 绑定服务器 → 生成发布审批单」全链：`manage_projects` 建项目（code 白名单/唯一校验与 Web 一致）→ `manage_services` 注册服务（`git_repo` 填 http(s) 远程 URL 时**首次构建自动 clone** 到 `deploy.project_root` 下同名目录，clone 失败阻断构建、URL 内嵌凭证日志脱敏；本地形态拒../../../盘符防穿越）→ `manage_environments` 建环境（新项目无环境时 `plan_operation` 会拒单；**clone 动作从既有项目复制环境六配置块 + 服务器绑定**，多项目共享基础设施场景一键接入）→ `manage_servers` 绑定目标服务器（bind upsert：未注册先注册再绑定，幂等）→ `plan_operation` 生成审批单，审批（Web 审批单页或对话中 `decide_run`）后构建部署。多服务分阶段发布用 `manage_pipelines` 编排（stages 用服务名数组，落库为 Web 原生 service_id 形态；`plan_operation` target 填流水线名即可整链发布，build_first=true 先统一构建全部镜像再逐 stage 发布）。均仅 admin，动作全落审计（`agent_create_project` 等）。
- **循环**：最多 8 轮工具调用（`AGENT_MAX_TOOL_ROUNDS`）；工具结果超长自动截断（`AGENT_MAX_RESULT_CHARS`）；第 8 轮仍未收敛则输出当前事实总结。
- **角色注入**：系统提示按角色分级——viewer 只见只读工具；`plan_operation` 等写工具仅 operator/admin 可用，且在服务端二次校验。

## 四、Phase 2：决策点 AI 化

四个决策点全部遵循：**规则引擎先给结论 → 相似案例召回 → LLM 润色（可选）→ 落 `agent_advice`**。同类建议 TTL 去重，避免调度轰炸：

| 场景 | scope | 触发 | 去重 TTL | 输出 |
| --- | --- | --- | --- | --- |
| 灰度继续/停止/回滚 | `canary` | 灰度决策文件 waiting（调度轮询）或 `GET /api/deploy/canary-decisions/<id>` 挂接 | 300s | 结论 continue/stop/rollback + 置信度 |
| 发布失败归因 | `task_failed` | 近 10 分钟 failed 任务（调度轮询） | 6h | 回滚建议（含目标版本）/ 首次发布人工排查 |
| 告警归因 | `alert` | 未解决 P0/P1 告警（调度轮询）/ 手动触发 | 600s | 命中模式 / 关联发布 / 处置建议 |
| 发布预检 | `precheck` | 手动（对话 / API / 按钮） | — | 检查清单 + passed 结论 |

**灰度建议规则优先级**（硬证据优先）：首台探测失败 → `rollback`(0.9)；服务非 online → `rollback`(0.8)；近 30 分钟错误日志 → `stop`(0.75)；P0/P1 活跃告警 → `stop`(0.7)；否则 `continue`。无回滚版本（首次发版）时 rollback 自动降级为 stop。决策文件为 `data/canary_decision_<task_id>.json`，建议写入后由人工在大屏灰度弹窗确认。

**发布预检检查项**：目标存在且启用 → 环境绑定服务器 → 版本号格式（x.y.z，可选）→ 同版本 24h 重复发布告警 → 发布窗口（block 禁发 / warn 需确认）→ 近 12h 失败发布 → 服务当前监控状态。任何一项 block 则整体不通过。

## 五、Phase 3：发布执行 Agent

```
对话/工具 plan_operation → agent_runs(awaiting_approval, 记发起会话 session_id) → 人工审批(Web/对话 decide_run)
    ├─ approve → create_task + task_manager.submit_task（复用平台通道）
    │            后台 _watch_run 轮询任务终态 → 回写 status/summary + 向发起会话回写一条助手消息 → 完成闭环
    ├─ reject   → 关闭审批单（记审批人与意见）
    └─ 执行中    → 进程重启后 scheduler 轮询 sync_executing_runs 收敛状态
```

终态回写会话：`_watch_run`（后台线程）与 `sync_executing_runs`（调度器）共用 `_finalize_terminal`，以 `WHERE status='executing'` 条件更新作 CAS，仅一方赢得收敛后发布 agent 事件并回写消息，杜绝双发/重复；无 session_id 的审批单（Web 直接创建/建议采纳）仅收敛状态不回写会话。前端对话页监听 agent/tasks 事件，按最大消息 id 增量重绘，实时呈现执行结果。

对话审批 `decide_run`（operator/admin）与 Web 审批单页同等效力：用户在对话中明确指示批准/驳回后，先核对 pending_approvals/run_detail 再执行；决策者身份（ctx 用户）落审计（`agent_approve`/`agent_reject` + `via:agent`）；含发布窗口告警时必须用户已确认风险并显式 `force_window=true`。安全红线不变：LLM 不得未经用户明确指示自行调用 decide_run。

**告警管理对话化**：`manage_alert_rules`/`manage_silences`（operator/admin，与 Web 端规则/静默页同等效力）补齐告警闭环的监测策略端——调阈值、改作用域、「发版期间静默某环境告警 2 小时」等诉求直接在对话完成。规则校验镜像 Web 端（metric_type×metric 枚举、指标规则禁 service 作用域等跨字段约束、metric 型 duration ≤ 1200），update 为局部更新且合并复校验防跨字段失配；静默 one_time 用 epoch 秒、recurring 用 HH:MM+weekdays。对话侧比 Web 更严的一处：scope 引用悬空 id 直接拒绝（防 LLM 幻觉 id 落库成死配置，错误信息引导先查 list_* 工具）。同步修复引擎缺陷：静默 scope.envs/servers 按实体 id 配置而告警 target_info 携带环境名/主机名，旧版直接比对从不命中——评估轮快照加载时统一把 id 翻译为名字集合再匹配（`_translate_silence_scope`，非 id 元素原样保留兼容名字直填）。

**观测与治理对话化**：补齐排障/巡检/空间治理的对话断点。`logs_context`（read）按条目 id 查看某条日志前后 N 行（id 来自 `search_logs` 结果的 id 字段——本档同步补上该字段闭合链路）；实现镜像 Web 日志中心的跨平台定位算法（line_offset 为逻辑字节偏移，Windows 文本模式 \r\n 需补偿），路径遍历防护内建（realpath 必须落在 data/logs/collect 内）。`monitor_metrics`（read）查历史与扩展指标：metric=response_times（服务响应时间历史，含成功率/均值/极值汇总）/ snapshots（每台服务器最新快照：swap/网络/磁盘IO/TCP 连接/重启/运行时长/docker 占用/证书天数；env_id 映射为环境名、docker 字节换算 MB）——`service_status` 看实时状态，本工具看历史趋势，归因/巡检场景的硬数据源。`manage_log_storage`（仅 admin，与 Web 日志中心同等效力）：action=status（存储概况：日志行数/DB 占用与空闲页/目录大小）/ settings（保留期 log_retention_days/log_archive_days 1~365，不传参查当前值）/ cleanup（立即按保留策略清理过期日志并回收空间，与每日自动维护同一套 `run_manual_cleanup` 逻辑；并发抢锁 busy 直接拒绝）；审计 action 名 `agent_update_log_settings`/`agent_manual_log_cleanup`（via:agent 区分通道）。

**依赖拓扑与快照对话化**：`service_topology`（read）统一 Web 端拓扑/影响/发布顺序/快照五视图——metric=topology（项目依赖全图，手动依赖 + dubbo_services 自动检测）/ impact（单服务上下游直接 + 两跳间接，发布前影响评估）/ publish_order（Kahn 拓扑排序，被依赖的先发布；has_cycle 提示拆环）/ snapshots（部署快照列表）/ compare（两次快照间服务版本差异；project+env 对比最近两次或 old_id+new_id 手动指定）。`manage_dependencies`（operator/admin）：source 依赖 target（target 先发布）；create 含环检测（BFS：target 可达 source 即成环拒绝）+ 服务名悬空拒绝（对话侧比 Web 更严，防幻觉死配置）；list 返回带记录 id 供 delete。同步补全平台缺口：`deployment_snapshots` 表此前仅测试写入（快照对比功能空转）——`task_manager._save_deploy_snapshot` 在任务成功终态后落快照，覆盖 Web/Agent/调度/webhook 全通道；image_versions 优先解析子进程日志的自动版本行（流水线每服务 `[name] 自动版本: ... 新版本 X`，单服务无前缀行归属任务 name），固定版本时单服务记 {name: version}、流水线展开 stages 全部服务同版本；restart 不落快照；失败静默绝不阻断终态回写。

**到期监控对话化**：`manage_monitors`（operator/admin，与 Web 到期监控页同等效力）补齐「证书/域名/云续费/license 到期」的对话入口——添加监控后自动告警提醒（warn_days 阈值 + severity 分级 + 通知渠道）。action=list（days_left 实时重算，0=已过期）/ create（type=ssl/cloud_renewal/license/custom/domain；domain 无 expiry_at 时自动 WHOIS 查询到期时间，非 ssl/domain 类型必须显式给 epoch 秒到期时间）/ update / delete / toggle / check（手动探测：ssl 类型即时探测证书剩余天数并回写，domain 类型 WHOIS 刷新到期时间与错误信息）/ whois（临时查询域名到期，无需建监控项，返回后引导 create 建立持续监控）/ config（全局默认值：default_warn_days/default_severity/default_channels/ssl_check_interval；写仅 admin，与 Web 权限对齐）。校验镜像 Web 端（type/severity/channels 枚举、port 1-65535、warn_days 非负）；与只读工具 `cert_monitors`（紧凑视图，最多 50 条）互补，list 返回含禁用项在内的全量记录及记录 id。

**运维辅助对话化**：三个高频小动作收入对话。`manage_servers` 新增 action=test（仅 admin，与 Web `/servers/test` 同等效力）：按 host 或 server_id 定位已注册服务器，服务器级 ssh 凭据优先、缺省回退首个绑定环境 ssh 段、再回退 root:22，复用 `webapp.monitor._ssh_exec`（惰性 import）执行 `echo ok` 探连通与耗时（失败返回 ok=False 属正常结果而非工具错，仅探测成功才写审计 `agent_test_server`）——接入绑定后验连通、故障排查场景常用。`refresh_monitor`（operator/admin，与 Web `/monitor/refresh` 同等效力）：`get_monitor().refresh()` 立即触发一轮采集，数据过时或刚改完配置想看最新状态时先刷一轮（采集器未启动则拒绝）。`manage_config_backup`（仅 admin，与 Web `/config/export|import` 同等效力）：action=export 把 DB 配置全量导出为 YAML 到 `data/export/config/`（单项目兼容名可直接复制为 config/，多项目按 code 加后缀）；action=import 从项目 `config/` 目录逐文件回灌 DB（逐文件结果见 results，文件不存在也计入失败项）；复用 `webapp.exporter.export_all`/`webapp.importer.import_from_config`（与 Web 同一实现），根目录从 tools.py 位置推导与 config_routes 同源；审计 `agent_export_config`/`agent_import_config`（via:agent）。

**新能力回灌日报**：到期监控体系建成后，巡检日报原本只聚合健康/告警/发布/审批/计划五块，从未纳入到期——新能力与旧聚合面存在口径断点。`digest._expiring` 补齐第六块：复用 `AlertEngine._parse_tiered` 与告警引擎同口径判定（阶梯取最宽档、否则逐项 warn_days>0 优先、回退全局 default_warn_days），将临近到期项按剩余天数升序写进日报（已到期标❗）；单点异常降级为该节不可用不阻断整报，口径一致确保不会「日报说无到期、告警却已触发」。工具面未变（`send_digest` 已存），仅描述补到期。

**前端实时通道立项**：平台页面的数据新鲜度原本全靠盲轮询——全量盘点得 7 处独立 `setInterval`（dashboard 5s、devices 3s/5s、deploy 2s/5s、alerts 15s、agent 15s、schedules 10s、logs 3s 追尾）加 app.js 三处全局角标轮询，既浪费请求又有最长一个周期的延迟。立项采用 **SSE 而非 WebSocket**：需求本质是 server→browser 的单向「有新数据了，去重取」通知，SSE 零新依赖（requirements 只有 Flask，不引入 flask-socketio/gevent）、不改运行模型（`app.run(threaded=True)` 单进程多线程，Dockerfile CMD 同源），跨进程收不到事件的风险由前端兜底轮询承接；浏览器侧沿用平台既有 SSE 消费范式（`fetch` + ReadableStream 逐行解析 `data:` 帧——EventSource 无法携带 Authorization 头，与 JWT 认证不兼容）。

- **事件总线**（`webapp/events.py`，进程内）：`subscribe(topics)`/`publish(topic, data)`/`next_event(sub, timeout)`；每订阅者一条 `maxsize=100` 有界队列，写满只置 `gap` 标记并计 `dropped`、**绝不抛错影响业务**（`publish` 内部全吞异常返回 False）；队列曾溢出且当前排空时补一帧 `{"topic":"resync"}` 提示前端全量重取；全局单调 `seq` 便于排障；订阅者上限 64，超限拒接新连接。事件只作**信号**、不带业务数据契约，客户端收到后仍走原有 REST 取数，避免为推送另立一份易漂移的数据结构。
- **生产者埋点取「唯一漏斗」**：任务状态只在 `db.update_task`（`status` 变化才推）推、告警/运行单/建议状态只在 `db.update_record` 的**表→主题映射表** `_STATUS_TOPICS`（`alerts`→alerts、`agent_runs`/`agent_advice`→agent，且载荷统一为 `{table,id,status}`）推——后者一处即覆盖 route 层手工 ack/resolve/reopen/批量与引擎自动恢复，比在 5 个处理点重复埋点更不易漏、也不会双发；映射表**显式不含 `tasks`**（该表走 `update_task` 专属漏斗，双埋会造成每个状态两帧）；`db.create_task` 是 Web/CLI/Agent/调度/webhook 六个创建入口的共同漏斗，在此推 `tasks` 的 `created` 事件（排队项新建也是列表变化源）；`monitor._cycle/refresh` 推 `{round, envs}`；`alert_engine._fire` 只在**新建告警**分支推（同指纹每轮重复命中不推，防事件风暴）；`advisor.save_advice` 推 `agent`；`LogCollector` 在**轮末**汇总一帧 `logs`（本轮新增行数 + 涉及服务，无增量不推，避免每容器一帧把已绑页面打成重取风暴）。
- **事件化边界靠「是否本进程写入」判定**：服务日志由采集器线程写 `log_entries`（进程内）→ 事件化；**任务日志文件由发布子进程直接写**（`/api/tasks/<id>/log` 走 `tail_log(task["log_file"])`，跨进程收不到总线事件）→ 发布抽屉 2s、日志中心 3s 的**增量追尾轮询必须保留**，只把「终态收尾与列表徽标刷新」交给 `tasks` 事件。此取舍写进代码注释，防止后来者误删轮询导致日志不再追尾。
- **前端双态调度**（`webapp/static/js/live.js`）：`liveBind(topic | topics[], fn, fallbackMs) → off()`，一个刷新函数可吃多个事件源（如设备卡片墙同时盯 `monitor`+`tasks`）——**多主题必须绑一条**，绑多条会让合并窗口各自失效、同一事件重复重取。通道 online 时靠事件驱动（同绑定 400ms 合并，沿用轮询性能口径）并清掉兜底定时器；connecting/degraded 时按各自 `fallbackMs` 退回轮询——**任何时刻都存在取数路径，不存在「既不推也不轮」的死角**；页面 hidden 释放长连接并降频至 ≥60s、回前台立即重连（已 online 且可见时跳过重建，防可见性抖动重复订阅）；沿用已退役的 `poll()` “回调返回 false 即自动解绑”契约（异步回调返回 Promise 需先解包再判；`poll()` 本体已在本批删除，详见下段）。落地节奏：先接 dashboard / alerts / 全局角标跑马灯三处，随后一次收口余下五处（deploy 列表 5s、devices 卡片墙 3s 与服务日志 5s、agent 15s、schedules 10s、logs 任务下拉 3s）——**平台已无盲轮询，仅存两处刻意保留的子进程日志追尾轮询**。页面改造统一走 `liveOffs` + `clearLiveOffs()` + `destroy()` 解绑，`render()` 仍返回定时器数组（现为空数组）以守路由契约。
- **重连退避只能以 `hello` 帧为凭**（实测踩坑）：首版在 HTTP 200 就复位退避计数，而服务端订阅满时会返回 **200 + `error` 帧并立即收尾**，于是 online→closed→degraded→1s 重连形成风暴（deploy 页 13s 内约 12 次 `/api/stream`）。改为收到 `hello` 才复位 `attempt`、收到 `error` 置 `capped` 并把下次退避抬到 30s 下限。
- **通道可视**：顶栏通道指示灯做成胶囊芯片（`.live-chip` + 在线呼吸光环，复用既有 `.dot.ok/warn/gray` 与主题 token，不引入新视觉语系；原 `.tb-btn` 32px 方壳会把圆点+文字挤掉），点击展开详情小面板（定位范式同 `#userMenu`）显示通道状态 / 最近事件时间 / 订阅主题 / 一键重连，面板开着时随事件跳动刷新；`GET /api/stream?topics=` 订阅须在**生成器体内**发生（视图里订阅会在「客户端一帧未读即断开」时因生成器从未启动、`finally` 不执行而泄漏订阅者），超限改发 `error` 帧并立即收尾；死连接要等下一次写帧（事件或 15s 心跳）才因 BrokenPipe 退出生成器，回收窗口上界即心跳周期。
- **Agent 侧**：新增只读工具 `live_status`（三级角色全可见）返回订阅数/累计发布/丢弃数/主题与 monitor、alert_engine、log_collector 是否在跑及其周期，用于回答「数据为什么不即时更新」。
- **浏览器实测（临时库 + MONITOR_INTERVAL=20 的一次性 UI 服务器）**：五页通道 online 时空载 30~62s 均为 **0 次取数请求**（原节奏应为 2~4 次），`/api/stream` 全程只 1 条连接；创建任务后 **22~75ms** 内即看到对应页重取（事件驱动而非等到下一轮轮询）；设备卡片墙按 monitor 周期 20s 节奏重取；跨页路由切换后旧页绑定不再发请求（`destroy` 解绑有效）；五页 0 console 报错。

- **计划生成**：`plan_operation(op=deploy/rollback/restart, target=服务或流水线名, env, version, previous_version, canary/skip_build/auto_version/no_git_tag)` 校验目标存在、版本格式、与平台一致的回滚语义（rollback 时 version=回滚目标），产出可预览的 CLI 命令行，绝不直接执行。
- **定时计划立即执行**：`manage_schedules(action=run)` 生成 `kind=schedule_run` 审批单（与 Web 端 run_schedule 等效但必经人工审批）；block 窗口拒单、warn 窗口审批需显式 force_window，通过后按调度器同语义创建任务并后台同步终态。
- **建议一键采纳**：失败归因建议（有回滚版本）→ `POST /api/agent/advice/<id>/to_run` 自动生成回滚审批单（参数从 evidence.task 推导，与手动建单同一安全链路）；灰度决策建议 → 直接写决策文件（仅当任务仍在 waiting，否则 409 提示）；告警归因/预检建议无可执行动作，明确拒绝。已完结审批单可 `POST /api/agent/runs/<id>/to_case` 一键沉淀案例（含关联归因建议），Web 端建议卡片/审批单详情均有对应按钮。
- **建议生命周期**：`agent_advice.status` 三态——`pending` 待处理 → `adopted` 已采纳（to_run 成功自动标记，终态，带 `action_ref` 关联审批单/任务）/ `dismissed` 已忽略（人工，可重开回 pending）。前端建议列表按状态过滤（默认待处理），已处理建议不再堆积；建议详情展示状态、操作人与关联案例反查。
- **审批单状态机**：`awaiting_approval → executing → success / failed`（或 `rejected`）。重复审批 409；viewer 无审批权（403）。
- **发布窗口联动**：计划生成时检测窗口——block 直接拒单；warn 记 `window_warning`，审批时必须显式勾选「已确认窗口风险」才放行（`force_window`），放行后任务携带 `--force-window`。
- **可观测与审计**：审批人 / 时间 / 意见、任务 id、执行摘要全部落库；「审批单」页签可直接看到任务终态与日志摘要；操作进 `audit_logs`。

**前端交互打磨第一批（键盘可达 / 覆盖层契约 / 通知治理 / 请求合流）**：实时通道解决了「数据什么时候新」，交互面暴露的是另一类问题——量化审计得：全站 `aria-` 仅 4 处、`role=` 0 处、`:focus-visible` 0 条（输入类有 `:focus` 描边但按钮/自绘控件无任何焦点态）、无 `prefers-reduced-motion`；16 处 `<span class="chip">` 用 `el.onclick=` 绑交互（键盘不可聚焦也不响应 Enter）；`toastError` 131 处且同屏无上限无去重（一轮轮询失败能堆十几条盖住整屏）；`modalRoot` 只有 confirm/prompt/改密三个使用者且均无 Esc 与焦点契约。结论：**集中收口在 `components.js` + `api.js` + 两份 CSS 即覆盖全站，无需逐页改模板**。

- **键盘可达走「自动增强」而不是逐元素改标签**：判定「是不是控件」不硬编类名（页面会不断新增）而看 `typeof el.onclick === 'function'`（项目统一赋值式写法）；跳过原生可交互标签（`KB_NATIVE`）、跳过已自带 `role` 的作者已处理控件、跳过 `.sel-pop` 后代（下拉选项不该逐个进 Tab 序列）；补 `tabIndex=0` + `role=button` + 短文本（≤40 字）`aria-label`（需折叠空白：模板里的换行+缩进会把无障碍名带成多行）。Enter/Space 用 **document 委托**而非逐元素挂监听（onclick 常在 DOM 插入后的下一个任务才赋值，委托不受先后影响）；插入时随 MutationObserver 扫描 + 1s 防抖整页补扫兜住异步绑定。实测 12 个路由/标签页「有 onclick 却不可键盘操作」的漏网数为 0（侧栏 `.nav-item`、面包屑、筛选 chip、任务名、搜索下拉项全部纳入）；补扫成本：184 节点页面 0.2ms，800 行可点击表格（2585 节点）压力用例 0.6ms。
- **Esc 只做「层栈仲裁」**：`pushEscLayer(handler)` 入栈、只有栈顶消费，监听挂 **document 捕获阶段**——注册在同一目标上的监听器无法靠 `stopPropagation` 互拒，而挂冒泡阶段则自绘下拉的面板 Esc 会被外层抽屉抢走。抽屉/确认框/输入框/改密框/搜索面板/下拉面板六处接入；抽屉被新抽屉直接覆盖时旧层也必须出栈（残留层会吞掉后续每一次 Esc）。实测：抽屉+下拉、抽屉+确认框均为 2 层，逐层关对、关完层栈归 0。
- **焦点契约**：`focusTrap(card, onClose, {focusBack})` 打开时记 `document.activeElement`、关闭后归还；确认框初始焦点给「取消」（Enter 误按不会执行危险操作，实测需 Tab 一次到「确认」才删得掉任务）；归还前用 `getClientRects()` 判可见（`offsetParent` 对 `position:fixed` 元素恒为 null，会误判成已隐藏），触发元素已随菜单/面板收起时落到调用方给的兜底元素（菜单项→改密弹窗：不做兜底焦点就掉回 body）；搜索下拉项点击先 `input.focus()` 再开抽屉，否则抽屉关闭时焦点无处可还。
- **toast 治理**：同屏上限 4（超出淘汰最早一条）+ 同文案合并「×N」+ 合并重置计时 + `dead` 标志（淡出中的那条不复用，否则新文案会被它的移除定时器连带删掉）；`#toastRoot` 挂 `role="status" aria-live="polite" aria-atomic="false"` 使读屏能播报。
- **`api()` GET 在途合流**（上档挂账收口）：拆出 `_apiCall`，同 URL 的 GET 在途时共用一个 Promise（失败也共享、各自 catch）——一个事件同时打到多个绑定时不再重发；**任何非 GET 请求先清空合流表**，防变更后误读到变更前发出的旧快照。实测：9 次并发同 3 个 URL → 3 次 fetch（对照组 9 次）；POST 后同 URL 的 GET 重发（2 次）；表无残留。
- **视觉降级闸门**：`:focus-visible` 统一焦点环（2px `--primary` + 2px offset，chip 另给 `--shadow-glow`）——不用 `:focus` 是为了鼠标点击后不留残留环；`@media (prefers-reduced-motion: reduce)` 一条全局闸门替代十余处动画逐条判定。
- **实测修出的自引入阻断缺陷**（写进代码注释）：`focusTrap` 返回的关闭函数最初不带参数，而调用方一律 `end(() => resolve(v))` —— 弹窗能关、`resolve` 被静默丢弃、`await confirmDialog()` 的处理链永久挂起。**全量 pytest 474 绿也照不出来**，只有浏览器里真跑一遍删除流程才暴露。Promise 化的 UI 契约必须实跑。
- **浏览器实测（临时库 + 一次性 UI 服务器）**：上述所有路径（Tab 走查 / Enter / Space 触发 / 逐层 Esc / 焦点归还 / toast 上限与 ×N 合并 / GET 合流）逐项验证通过，12 个路由与标签页 **0 console error / 0 warn**，抽屉关闭后 5s 内无残留取数请求。
- **刻意留挂账**：抽屉遮罩点击无条件关闭（自动脏检测会误伤筛选框，风险大于收益）；`#userMenu` 未接 Esc 层（本来也没有 Esc 契约，不构成回归）；弹窗内 Tab 可跑到背页元素（未做完整 focus-trap 循环）；`addEventListener('click')` 绑定的元素不在自动增强判定内（项目统一用 `el.onclick=`，当前无此类入口）。
- **顺手清掉上一档遗留的死代码**：全站迁到 `liveBind` 后，`api.js` 的 `poll()` 盲轮询封装已无任何调用点（全仓 grep 仅余定义与两句注释引用），留着只会诱导新页面退回定时轮询，故删除并在原位置注明“为何需要定时器的两处（发布抽屉 2s / 日志中心 3s 子进程日志追尾）直接用自己的 setInterval”；`live.js` 里两处“沿用 poll() 契约”的注释改为自包含描述。

**前端无障碍第二批（色彩对比度 / 表单关联 / 错误播报）**：第一批解决「键盘能否操作」，本批解决「能否看清、读屏能否听懂」。静态审计量化得四条缺口——① 文本 token `--text-3` 在原色号 `#5A7296` 下于最深面板仅 3.2~3.7（11~12px 小字不达 WCAG AA 4.5），且多处「亮色填充 + 浅色文字」（chip/pill/badge/preview/`⚡`logo/头像/悬浮球）白字比值低至 1.55；② 全站 130 个 `.field`、111 个裸 `<label>` 而 `for` 仅 2 处，字段与控件无程序化关联；③ `aria-describedby`/`aria-invalid`/`aria-required` 全站 0 处，校验失败只标红、读屏零播报；④ 结论沿用第一批——**集中收口在 `components.js` + 两份 CSS 即覆盖全站，无需逐页改模板**。

- **对比度度量法**：比值 \(=(L_1+0.05)/(L_2+0.05)\)，小字需 4.5、大字（≥24px，或 ≥18.66px 且 ≥700 粗）需 3.0。**渐变背景不能读 `background-color`（为空会算出假比值）**——须解析各 `background-image` 色标、按 alpha 逐层合成、取最差候选端点。据此核算表（`_probe/contrast.js`，一次性）定端点改色。
- **色彩整改（token 化，不逐处硬编）**：`--text-3` 提亮为 `#859BBB`（最差合成底上 4.66，仍暗于 `--text-2` 的 5.42，层级靠字号与亮度拉开）；新增 `--on-bright:#04121F` 作「亮色填充上的专用深字」——凡 primary/accent/warning/success/danger 做底一律用它（白字在这些底上仅 1.7~3.4），chip/pill/badge/preview/原 `.btn.primary` 硬编值全部收归此 token；`⚡`logo 与用户头像的渐变端点 `#0066FF→#0090FF`（深字在 `#0066FF` 上仅 3.91 不达 4.5，换 `#0090FF` 端最差 5.79），悬浮球 26px 属大字（5.09）无需改端点、仅补 `color:var(--on-bright)`。
- **表单关联集中收口 `enhanceForms`**（挂在既有 `scanClickable` 末尾，随 MutationObserver + 1s 防抖补扫，与第一批键盘增强同一入口）：控件判定 `A11Y_CTRL`（`input:not([type=hidden]),select,textarea`）；已有名走 `hasAnyName`（aria-label/aria-labelledby/被 label 包裹/`for` 指向）跳过；无名则按链兜底——`.field` 有 `.f-label` 取其纯文本 `aria-label`（`fLabelText` 剔除 `.req`/`.q` 星标）→ 否则 `looseLabels` 找无 `for` 的裸 label（**直子优先**：「字段标题+内嵌子选项块」写法用全后代扫会多算一个）→ 唯一归属者给原生 `for`、多兄弟给 `aria-labelledby`；筛选条「`<span>项目</span> <select>`」用前兄弟纯文本启发式（≤20 字、不含控件、非 button/a/.btn/label）补名。纯文本 label 无法覆盖的「标题+多控件」组升级 `role=group` + `aria-labelledby`。
- **换肤下拉的属性镜像**：原生 `select` 被 `display:none`、`.sel-face`(role=combobox tabindex=0) 才是焦点，无障碍属性必须镜像到 face。`MIRROR_ATTRS`（label/labelledby/describedby/required/invalid/title）经 `MutationObserver(sel,{attributes,attributeFilter})` 持续同步——因写入时序不定，单向赋值会漏，观察者兜住后续任意时刻的改写（`sessSel` 即靠 `title` 镜像得名）。
- **错误播报（WCAG 3.3.1）**：`config-form-helpers.js` 的 `.f-err` 加 `role="alert"`（清文字即不播报、写入即播报）、`.req` 加 `aria-hidden="true"`（星标不脏化无障碍名）；`fieldErr()` 有错时 `aria-invalid=true` + 显示 f-err、清除时移 `aria-invalid` + 清空文本；face 的 `aria-invalid` 由镜像承担。**CSS 特异性坑**：`div.f-label`(0,1,0) 若改成 `<label>` 会被 `.field label{}`(0,1,1) 覆写样式——只加 `aria-*` 属性不改标签名。
- **浏览器量化复测（临时库 + `MONITOR_INTERVAL=20` 一次性 UI 服务器，仓库外 `_probe/`）**：同 DOM 前后对比，各路由「可点击/表单控件无无障碍名」失败数 **dashboard 13→0 / devices 33→0 / deploy 16→0 / alerts 18→0 / logs 14→0 / agent 13→0 / config 25→0 / 服务表单抽屉 39→0**；校验链路实测——空值保存后 `sName` `aria-invalid=true`、`.f-err[role=alert]` 显「服务名必填」且 `display:block`、`aria-describedby` 两目标（f-hint+f-err）均在，补值再存 `aria-invalid` 归 null、文本清空；各页 `dupIds` 为空；全路由 **0 console error/warn**。
- **度量假阳性教训（写进方法论）**：`navigate_page` 到「仅 hash 不同」的 URL **不重载页面**，会用旧 `components.js` + 上轮遗留 DOM + 泄漏的内联覆盖变量，误报「fab-btn 白字 1.77」「下拉未命名」——须 `type=reload, ignoreCache=true` 真重载，probe 先清注入变量再测 after、注入旧值测 before。Flask 生产 Jinja 缓存编译结果，改动不生效须置 `TEMPLATES_AUTO_RELOAD`。
- **刻意留挂账**：`logTaskSel` 仍无名（前兄弟是 1px 装饰分隔 span，且 `logs.js` 正被并行会话占着）；`alertKeyword`/`chatInput` 仅 placeholder 兜底名；`looseLabels` 判为 owners>1 的字段不猜归属；devices `.mon-switch` 空 label 需页面级修改；44px 触控目标尺寸属后续批次。

**前端无障碍第三批（地标 / 跳转链接 / 表格语义）**：前两批解决「看得清、读得懂」，本批解决「跳得快、结构清晰」（WCAG 2.4 Navigable / 1.3.1 Info）。静态审计得三缺口——① 全站无 skip link，键盘用户进主内容需 Tab 跳过整个侧栏+顶栏；② 侧栏是一堆可点 `.nav-item`  div（第一批已纳入键盘）但整个导航区不是 `nav` 地标；③ 12+ 张数据表的 `thead th` 均无 `scope`、表无 caption/名（读屏无法将单元格与列头对应）。

- **skip link**：screen.html `<body>` 首位插 `<a class="skip-link" href="#content">跳到主内容</a>`、`#content` 加 `tabindex=-1`（非原生可聚焦元素需此才接得住跳转焦点）；layout.css `.skip-link` 默认 off-screen（`top:-64px`）、`:focus/:focus-visible` 时才滑入 `top:var(--sp-3)`，亮底配 `--on-bright` 深字、焦点环用 `--text-1`（深色页背景上可见）。踩坑：`transition:top var(--t-fast)` 是误用（`--t-fast` 本身已是完整简写值 `all .15s var(--ease)`，展开成 `top all ...` 非法使 computed transition 退化为 `all`）→改 `transition:var(--t-fast)`。另：`:focus` 在自动化窗口未获系统焦点时不匹配（`document.hasFocus()=false`、`window.focus()` 无效，Chromium 行为），规则经 CSSOM 核实选择器与特异性正确，真实键盘用户 Tab 首站即生效。
- **地标**：router.js `renderSidebar` 把 `<div class="nav-scroll">` 改 `<nav class="nav-scroll" aria-label="主导航">`（nav 默认块级、CSS 按 `.nav-scroll` 类命中，与 div 等价无视觉回归）；`<aside id="sidebar">` 加 `aria-label="侧边导航"`（complementary 地标多个时需名才可区分）。顶栏 `<header>`/`<main>`/`<footer>` 本就是 banner/main/contentinfo 原生地标。
- **表格语义集中收口 `enhanceTables`**（接 `scanClickable` 末尾，与前两批同一入口）：列头一律补 `scope=col`（本域 thead 全为列头、无行头，只补未声明的）；表名四级回退——同 `.panel` 标题 → 祖先前序标题（`.panel-title/.section-title/.page-title`）→ 活动页签（`.tab-btn.active`/`.sub-tabs .chip.active`，剔除 emoji 前缀）→ 路由名（`currentRoute.name`），全落空不编造。**表头裸复选框**（`monCheckAll` 无 label 包裹、无前兄弟，走不到控件路径）补 `aria-label=全选`（表头里的复选框＝整列全选开关）。
- **浏览器量化复测（临时库 + 一次性 UI 服务器，仓库外 `_probe/`）**：6 张表跨 4 条取名路径全绿——config 用户表 5 列头/名“用户管理”（祖先 page-title）、logs 审计 5/“审计日志”（同 panel-title）、alerts 规则 9/“规则管理”（活动页签，📏 已剔除）、到期监控 11/“到期监控”、deploy 任务 9/“任务列表”（sub-tab）、dashboard 健表 14/“服务器状态”（同 panel）；全选复选框 `aria-label=全选`；各页 `dupIds` 为空；alerts/dashboard 路由 **0 console error/warn**；全量 pytest 492 passed。
- **刻意留挂账**：行内 toggle 复选框（alerts `alert-row-check` / `.mon-switch`、devices `.mon-switch`）包裹空 label，逐行命名需行上下文（哪条告警/服务），属页面级；skip-link 显形不可在无头未聚焦窗口测视觉（机制经 CSSOM 坐实）。

## 六、Phase 4：MCP Server 与案例库

### MCP stdio

```powershell
python agent/mcp_server.py --db data/platform.db --role operator --user ops
```

零依赖实现 `initialize / ping / tools/list / tools/call`，`tools/call` 复用 tools.py 执行（含角色过滤）。MCP 客户端通过 stdio 配对即可把平台查询/审批计划能力接入 Agent 工具链。

### 故障案例库（长期记忆）

- 告警归因 / 失败归因生成建议后可「一键沉淀为案例」（`POST /api/agent/advice/<id>/to_case`，对话工具 `case_save` 亦可）；案例可补全 `root_cause / action / result / lesson / tags`。
- 检索用字符 bigram Jaccard 相似度 + service/tag 过滤，命中案例会作为证据带入下一次建议（归因 → 记忆 → 归因的飞轮）。
- 案例检索与溯源：列表支持 `tag` 参数过滤（JSON tags 参数化 LIKE）；详情页附「相似案例」推荐（`search_cases` 新增 `exclude_id` 排除自身，点击跳转）；来源三态标识（manual 手工 / advice 建议沉淀 / agent_run 执行沉淀）在列表卡片与详情展示，详情页标签可点击发起过滤。
- 案例⇆建议互链：案例详情来源行的「建议 #n」与建议详情的关联案例条目均可点击互跳（详情缓存未命中时自动回退取单条接口）；`case_update` 对话工具字段白名单与 Web PUT 对齐（补齐 symptom/service/env/version）。
- 案例与建议均按 RBAC：viewer 只读，operator 可建/改案例，admin 可删除。

## 七、周期巡检日报

- **聚合内容**（全部来自平台既有事实源，单次聚合毫秒级）：服务健康统计（监控缓存，异常服务逐个列出）、未解决告警（按 P0/P1 优先，Top 5 明细）、近 24h 发布任务（成功/失败计数 + 失败清单）、待审审批单数、启用中定时计划与 24h 内 upcoming、临近到期监控项（证书/域名/订阅等，与告警引擎同口径复用 `AlertEngine._parse_tiered` 判定阶梯/单阈值，按剩余天数升序列出）。
- **发送**：复用告警六渠道（`alert_channels.send_custom`，同一套配置检查与重试，**不写 alert_events**——日报不是告警）。
- **调度**：挂在建议调度线程同一循环，每轮检查 cron 是否到点（48h 补发窗口：重启跨日不漏发、停机超 2 天不补发）；多实例用 `scheduler_locks` 独立锁名 `agent_digest` 互斥，拿锁后双检防重复发送。
- **配置**：`platform_settings` 键 `agent_digest`（JSON：enabled/cron/channels/title/last_sent_at/last_result）；Web 端「智能助手 → 📤 巡检日报」弹窗维护（仅 admin），支持内联预览与手动立即发送；对话工具 `send_digest` 亦可立即推送。
- **发送历史**：每次实际发送落一条 `agent_digest_logs`（成功失败都记，含 ok/total 渠道数与逐渠道明细），Web 弹窗展示最近 10 次结果；`GET /api/agent/digest/logs` 可追溯，失败排查不再依赖单点 last_result。

## 八、HTTP API 一览（前缀 /api，JWT Bearer）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | /agent/chat | 对话（批量，body: message, session_id；返回 reply/plan_run_id） | 登录用户 |
| POST | /agent/chat/stream | 对话（SSE 流式，事件：text 正文增量 / tool 工具开始 / tool_result 工具结束 / done 收尾 / error 异常） | 登录用户 |
| GET | /agent/sessions | 会话列表（mine：默认本人；admin 加 scope=all 看全部；含标题/条数/归属/摘要/置顶；q 参数按标题/最后消息模糊搜索） | operator/admin |
| PUT | /agent/sessions/<id>/title | 重命名会话（仅本人；空串恢复自动标题） | operator/admin |
| PUT | /agent/sessions/<id>/pin | 置顶/取消置顶会话（仅本人；body: pinned 布尔，置顶优先于最近活动排序） | operator/admin |
| DELETE | /agent/sessions/<id> | 删除整段会话（不可恢复；默认删自己的，admin 可 user_id= 指定） | operator/admin |
| GET | /agent/sessions/<id>/messages | 会话消息回溯（默认仅本人；admin 可 user_id= 跨用户审计） | operator/admin |
| GET | /agent/runs | 审批单列表（status 过滤） | 登录用户 |
| GET | /agent/runs/<id> | 审批单详情 | 登录用户 |
| POST | /agent/runs/<id>/decision | 审批（action: approve/reject, force_window） | operator/admin |
| POST | /agent/runs/<id>/to_case | 已完结审批单一键沉淀案例 | operator/admin |
| GET | /agent/advice | 建议列表（scope/ref_id/status 过滤，status: pending/adopted/dismissed） | 登录用户 |
| GET | /agent/advice/<id> | 建议单条详情（登录用户） | 登录用户 |
| GET | /agent/advice/badge | 待处理建议计数（全局导航角标） | 登录用户 |
| POST | /agent/advice/<id>/to_case | 建议沉淀为案例 | operator/admin |
| POST | /agent/advice/<id>/to_run | 建议一键采纳（失败归因→回滚审批单；灰度→写决策，自动标 adopted） | operator/admin |
| POST | /agent/advice/<id>/dismiss | 忽略待处理建议（仅 pending，可重开） | operator/admin |
| POST | /agent/advice/<id>/reopen | 重开已忽略建议（回 pending） | operator/admin |
| DELETE | /agent/advice/<id> | 删除建议 | admin |
| GET/POST | /agent/digest/config | 巡检日报配置查看 / 保存（enabled/cron/channels/title） | admin |
| GET | /agent/digest/preview | 预览当前日报文本（不发送） | operator/admin |
| POST | /agent/digest/send | 手动立即发送一次日报 | operator/admin |
| GET | /agent/digest/logs | 日报发送历史（最近 N 条） | operator/admin |
| POST | /agent/analyze/alert/<id> | 手动告警归因 | operator/admin |
| POST | /agent/precheck | 发布预检（target/env/version） | operator/admin |
| GET/POST | /agent/cases | 案例列表（keyword/service/tag/advice_id/exclude_id 过滤）/ 新建 | 新建需 operator/admin |
| GET/PUT/DELETE | /agent/cases/<id> | 案例详情 / 修改 / 删除（删除 admin） | 按列 |
| GET | /agent/config | 网关配置状态（**不回显 api_key**） | admin |
| POST | /agent/config | 保存网关配置（base_url/api_key/model） | admin |
| GET | /deploy/canary-decisions/<task_id> | 既有灰度接口，waiting 时附加 advice 建议 | 登录用户 |
| GET | /stream | 页面实时通道（SSE，`text/event-stream`）；topics=monitor/alerts/tasks/agent/logs 过滤，15s 心跳，带 `X-Accel-Buffering: no` | 登录用户 |

## 九、配置项

| 环境变量 | platform_settings 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| AGENT_LLM_BASE_URL | agent_llm_base_url | "" | OpenAI 兼容端点，如 https://host/v1 |
| AGENT_LLM_API_KEY | agent_llm_api_key | "" | 密钥（GET 配置接口不回显） |
| AGENT_LLM_MODEL | agent_llm_model | "" | 模型名，留空由服务端决定 |
| AGENT_MAX_TOOL_ROUNDS | — | 8 | 单轮对话最大工具调用次数（1-20） |
| AGENT_MAX_RESULT_CHARS | — | 6000 | 工具结果截断长度（500-20000） |
| — | agent_digest | enabled=0, cron="0 9 * * *", channels=[] | 巡检日报配置（JSON，Web 弹窗维护） |

> 数据表：建议生命周期列（`agent_advice.status/status_at/status_by/action_ref`，存量库 `_migrate` 幂等补列）、发送历史表 `agent_digest_logs`（sent_at/ok_count/total_count/channels/detail/summary）与会话元数据 `agent_session_meta`（session_id+user_id 唯一，存手动重命名标题与置顶态 pinned，仅本人操作时写入；存量库 `_migrate` 幂等补 pinned 列）已在 SCHEMA 与迁移中定义。

## 十、安全与边界

1. **人工审批不可绕过**：任何写操作工具只生成审批单，执行必经 `decision=approve`。
2. **密钥保护**：api_key 仅环境变量 / DB 读取，不落日志、不出现在工具输出与配置查询响应。
3. **权限分层**：viewer 全链路只读（工具、API、前端按钮三级过滤）；发布窗口管理仅 admin（与 Web 端一致）。
4. **降级可用**：未配置 LLM 时批量对话返回 400 带配置提示、流式对话返回 SSE `error` 事件（均含 hint）；建议引擎自动降级纯规则（记录 model=rule-based）。
5. **防御性编码**：全部外部输入长度/类型约束；SQL 仅经白名单表名/列名或参数化查询。
