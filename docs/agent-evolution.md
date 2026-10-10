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
├── tools.py       36 个运维工具注册表（read/write 两级权限）
├── core.py        对话编排（ReAct 循环：模型思考 → 调工具 → 收敛；批量/SSE 流式双通道）
├── advisor.py     Phase 2 决策引擎：灰度建议 / 失败归因 / 告警归因 / 预检 / 案例沉淀
├── executor.py    Phase 3 发布执行：计划生成 / 审批状态机 / 任务提交 / 后台同步
├── memory.py      Phase 4 长期记忆：故障案例 CRUD + 字符 bigram 相似检索
├── digest.py      周期巡检日报：健康/告警/发布聚合 → 通知渠道定时推送
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
- **会话管理**：对话页支持多会话（下拉切换/新建/重命名/删除，`localStorage` 记住当前会话）；`float` 会话由全站右下角悬浮助手产生，也可在此管理。会话标题自动取首条用户消息前 40 字，可手动重命名（存 `agent_session_meta`，空名恢复自动标题；删除会话同步清理）。列表/删除/重命名仅 operator/admin（默认自己的会话；admin 可 `scope=all`/`user_id=` 跨用户管理，含 CLI 的 id=0 会话），消息回溯同样按用户隔离。
- **工具**：20 个只读工具 + 16 个写工具（operator/admin 可见），全部以 JSON 结果回给模型：
  - 只读：`list_projects / get_project / list_services / list_environments / env_servers / service_status / recent_tasks / task_detail / list_alerts / alert_detail / search_logs / release_window / canary_configs / cert_monitors / audit_logs / search_cases / case_detail / advice_list / pending_approvals / run_detail`
  - 写：`analyze_alert / precheck / ack_alert / plan_operation / case_save / case_update` + 平台配置管理与闭环：`manage_projects`（项目 CRUD，仅 admin）、`manage_services`（服务注册 CRUD，仅 admin；git_repo 支持远程 URL）、`manage_environments`（环境 CRUD + 跨项目 clone 含服务器绑定，仅 admin；ssh 口令输出脱敏）、`manage_servers`（服务器注册/绑定/解绑，仅 admin；bind 为 upsert，口令入参不回显）、`manage_schedules`（定时计划 CRUD/启停/立即执行）、`manage_windows`（发布窗口 CRUD，仅 admin）、`manage_canary`（灰度策略 CRUD/设默认）、`canary_decision`（灰度运行中决策 continue/stop/rollback）、`set_alert_status`（告警 resolve/reopen）、`send_digest`（立即推送巡检日报）
- **对话式项目接入端到端**：对话中给 git 地址与目标服务器即可走通「建项目 → 注册服务 → 建环境 → 绑定服务器 → 生成发布审批单」全链：`manage_projects` 建项目（code 白名单/唯一校验与 Web 一致）→ `manage_services` 注册服务（`git_repo` 填 http(s) 远程 URL 时**首次构建自动 clone** 到 `deploy.project_root` 下同名目录，clone 失败阻断构建、URL 内嵌凭证日志脱敏；本地形态拒../../../盘符防穿越）→ `manage_environments` 建环境（新项目无环境时 `plan_operation` 会拒单；**clone 动作从既有项目复制环境六配置块 + 服务器绑定**，多项目共享基础设施场景一键接入）→ `manage_servers` 绑定目标服务器（bind upsert：未注册先注册再绑定，幂等）→ `plan_operation` 生成审批单，人工审批后构建部署。均仅 admin，动作全落审计（`agent_create_project` 等）。
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
对话/工具 plan_operation → agent_runs(awaiting_approval) → 人工审批(Web/API)
    ├─ approve → create_task + task_manager.submit_task（复用平台通道）
    │            后台 _watch_run 轮询任务终态 → 回写 status/summary → 完成闭环
    ├─ reject   → 关闭审批单（记审批人与意见）
    └─ 执行中    → 进程重启后 scheduler 轮询 sync_executing_runs 收敛状态
```

- **计划生成**：`plan_operation(op=deploy/rollback/restart, target=服务或流水线名, env, version, previous_version, canary/skip_build/auto_version/no_git_tag)` 校验目标存在、版本格式、与平台一致的回滚语义（rollback 时 version=回滚目标），产出可预览的 CLI 命令行，绝不直接执行。
- **定时计划立即执行**：`manage_schedules(action=run)` 生成 `kind=schedule_run` 审批单（与 Web 端 run_schedule 等效但必经人工审批）；block 窗口拒单、warn 窗口审批需显式 force_window，通过后按调度器同语义创建任务并后台同步终态。
- **建议一键采纳**：失败归因建议（有回滚版本）→ `POST /api/agent/advice/<id>/to_run` 自动生成回滚审批单（参数从 evidence.task 推导，与手动建单同一安全链路）；灰度决策建议 → 直接写决策文件（仅当任务仍在 waiting，否则 409 提示）；告警归因/预检建议无可执行动作，明确拒绝。已完结审批单可 `POST /api/agent/runs/<id>/to_case` 一键沉淀案例（含关联归因建议），Web 端建议卡片/审批单详情均有对应按钮。
- **建议生命周期**：`agent_advice.status` 三态——`pending` 待处理 → `adopted` 已采纳（to_run 成功自动标记，终态，带 `action_ref` 关联审批单/任务）/ `dismissed` 已忽略（人工，可重开回 pending）。前端建议列表按状态过滤（默认待处理），已处理建议不再堆积；建议详情展示状态、操作人与关联案例反查。
- **审批单状态机**：`awaiting_approval → executing → success / failed`（或 `rejected`）。重复审批 409；viewer 无审批权（403）。
- **发布窗口联动**：计划生成时检测窗口——block 直接拒单；warn 记 `window_warning`，审批时必须显式勾选「已确认窗口风险」才放行（`force_window`），放行后任务携带 `--force-window`。
- **可观测与审计**：审批人 / 时间 / 意见、任务 id、执行摘要全部落库；「审批单」页签可直接看到任务终态与日志摘要；操作进 `audit_logs`。

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

- **聚合内容**（全部来自平台既有事实源，单次聚合毫秒级）：服务健康统计（监控缓存，异常服务逐个列出）、未解决告警（按 P0/P1 优先，Top 5 明细）、近 24h 发布任务（成功/失败计数 + 失败清单）、待审审批单数、启用中定时计划与 24h 内 upcoming。
- **发送**：复用告警六渠道（`alert_channels.send_custom`，同一套配置检查与重试，**不写 alert_events**——日报不是告警）。
- **调度**：挂在建议调度线程同一循环，每轮检查 cron 是否到点（48h 补发窗口：重启跨日不漏发、停机超 2 天不补发）；多实例用 `scheduler_locks` 独立锁名 `agent_digest` 互斥，拿锁后双检防重复发送。
- **配置**：`platform_settings` 键 `agent_digest`（JSON：enabled/cron/channels/title/last_sent_at/last_result）；Web 端「智能助手 → 📤 巡检日报」弹窗维护（仅 admin），支持内联预览与手动立即发送；对话工具 `send_digest` 亦可立即推送。
- **发送历史**：每次实际发送落一条 `agent_digest_logs`（成功失败都记，含 ok/total 渠道数与逐渠道明细），Web 弹窗展示最近 10 次结果；`GET /api/agent/digest/logs` 可追溯，失败排查不再依赖单点 last_result。

## 八、HTTP API 一览（前缀 /api，JWT Bearer）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | /agent/chat | 对话（批量，body: message, session_id；返回 reply/plan_run_id） | 登录用户 |
| POST | /agent/chat/stream | 对话（SSE 流式，事件：text 正文增量 / tool 工具开始 / tool_result 工具结束 / done 收尾 / error 异常） | 登录用户 |
| GET | /agent/sessions | 会话列表（mine：默认本人；admin 加 scope=all 看全部；含标题/条数/归属/摘要） | operator/admin |
| PUT | /agent/sessions/<id>/title | 重命名会话（仅本人；空串恢复自动标题） | operator/admin |
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

## 九、配置项

| 环境变量 | platform_settings 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| AGENT_LLM_BASE_URL | agent_llm_base_url | "" | OpenAI 兼容端点，如 https://host/v1 |
| AGENT_LLM_API_KEY | agent_llm_api_key | "" | 密钥（GET 配置接口不回显） |
| AGENT_LLM_MODEL | agent_llm_model | "" | 模型名，留空由服务端决定 |
| AGENT_MAX_TOOL_ROUNDS | — | 8 | 单轮对话最大工具调用次数（1-20） |
| AGENT_MAX_RESULT_CHARS | — | 6000 | 工具结果截断长度（500-20000） |
| — | agent_digest | enabled=0, cron="0 9 * * *", channels=[] | 巡检日报配置（JSON，Web 弹窗维护） |

> 数据表：建议生命周期列（`agent_advice.status/status_at/status_by/action_ref`，存量库 `_migrate` 幂等补列）、发送历史表 `agent_digest_logs`（sent_at/ok_count/total_count/channels/detail/summary）与会话重命名元数据 `agent_session_meta`（session_id+user_id 唯一，仅手动重命名时写入）已在 SCHEMA 与迁移中定义。

## 十、安全与边界

1. **人工审批不可绕过**：任何写操作工具只生成审批单，执行必经 `decision=approve`。
2. **密钥保护**：api_key 仅环境变量 / DB 读取，不落日志、不出现在工具输出与配置查询响应。
3. **权限分层**：viewer 全链路只读（工具、API、前端按钮三级过滤）；发布窗口管理仅 admin（与 Web 端一致）。
4. **降级可用**：未配置 LLM 时批量对话返回 400 带配置提示、流式对话返回 SSE `error` 事件（均含 hint）；建议引擎自动降级纯规则（记录 model=rule-based）。
5. **防御性编码**：全部外部输入长度/类型约束；SQL 仅经白名单表名/列名或参数化查询。
