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
├── tools.py       26 个运维工具注册表（read/write 两级权限）
├── core.py        对话编排（ReAct 循环：模型思考 → 调工具 → 收敛；批量/SSE 流式双通道）
├── advisor.py     Phase 2 决策引擎：灰度建议 / 失败归因 / 告警归因 / 预检 / 案例沉淀
├── executor.py    Phase 3 发布执行：计划生成 / 审批状态机 / 任务提交 / 后台同步
├── memory.py      Phase 4 长期记忆：故障案例 CRUD + 字符 bigram 相似检索
├── scheduler.py   后台建议调度（daemon 线程，默认 30s 一轮）
├── routes.py      Web API（/api/agent/*，18 条，RBAC 对齐平台）
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
| Web 对话 | 浏览器 `http://127.0.0.1:5678/#/agent`（侧栏「智能运维 → 智能助手」），含 对话/审批单/建议/案例 四个页签 |
| CLI | `python agent/cli.py --once "auth 服务最近有异常吗"` |
| MCP | `python agent/mcp_server.py --db data/platform.db --role operator`（stdio，可接入支持 MCP 的客户端） |

## 三、Phase 1：只读智能运维助手

- **对话**：登录用户在「智能助手」页与 LLM 多轮对话，回复**流式输出**（SSE 打字机效果，工具调用过程可见）；消息持久化到 `agent_messages`，可在 API/页面回溯（会话按用户隔离）。CLI/MCP 入口同样支持流式/批量两种形态。
- **工具**：20 个只读工具 + 6 个写工具（operator/admin 可见），全部以 JSON 结果回给模型：
  - 只读：`list_projects / get_project / list_services / list_environments / env_servers / service_status / recent_tasks / task_detail / list_alerts / alert_detail / search_logs / release_window / canary_configs / cert_monitors / audit_logs / search_cases / case_detail / advice_list / pending_approvals / run_detail`
  - 写：`analyze_alert / precheck / ack_alert / plan_operation / case_save / case_update`
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
- 检索用字符 bigram Jaccard 相似度 + service 过滤，命中案例会作为证据带入下一次建议（归因 → 记忆 → 归因的飞轮）。
- 案例与建议均按 RBAC：viewer 只读，operator 可建/改案例，admin 可删除。

## 七、HTTP API 一览（前缀 /api，JWT Bearer）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | /agent/chat | 对话（批量，body: message, session_id；返回 reply/plan_run_id） | 登录用户 |
| POST | /agent/chat/stream | 对话（SSE 流式，事件：text 正文增量 / tool 工具开始 / tool_result 工具结束 / done 收尾 / error 异常） | 登录用户 |
| GET | /agent/sessions/<id>/messages | 会话消息回溯 | operator/admin |
| GET | /agent/runs | 审批单列表（status 过滤） | 登录用户 |
| GET | /agent/runs/<id> | 审批单详情 | 登录用户 |
| POST | /agent/runs/<id>/decision | 审批（action: approve/reject, force_window） | operator/admin |
| GET | /agent/advice | 建议列表（scope/ref_id 过滤） | 登录用户 |
| POST | /agent/advice/<id>/to_case | 建议沉淀为案例 | operator/admin |
| DELETE | /agent/advice/<id> | 删除建议 | admin |
| POST | /agent/analyze/alert/<id> | 手动告警归因 | operator/admin |
| POST | /agent/precheck | 发布预检（target/env/version） | operator/admin |
| GET/POST | /agent/cases | 案例列表 / 新建 | 新建需 operator/admin |
| GET/PUT/DELETE | /agent/cases/<id> | 案例详情 / 修改 / 删除（删除 admin） | 按列 |
| GET | /agent/config | 网关配置状态（**不回显 api_key**） | admin |
| POST | /agent/config | 保存网关配置（base_url/api_key/model） | admin |
| GET | /deploy/canary-decisions/<task_id> | 既有灰度接口，waiting 时附加 advice 建议 | 登录用户 |

## 八、配置项

| 环境变量 | platform_settings 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| AGENT_LLM_BASE_URL | agent_llm_base_url | "" | OpenAI 兼容端点，如 https://host/v1 |
| AGENT_LLM_API_KEY | agent_llm_api_key | "" | 密钥（GET 配置接口不回显） |
| AGENT_LLM_MODEL | agent_llm_model | "" | 模型名，留空由服务端决定 |
| AGENT_MAX_TOOL_ROUNDS | — | 8 | 单轮对话最大工具调用次数（1-20） |
| AGENT_MAX_RESULT_CHARS | — | 6000 | 工具结果截断长度（500-20000） |

## 九、安全与边界

1. **人工审批不可绕过**：任何写操作工具只生成审批单，执行必经 `decision=approve`。
2. **密钥保护**：api_key 仅环境变量 / DB 读取，不落日志、不出现在工具输出与配置查询响应。
3. **权限分层**：viewer 全链路只读（工具、API、前端按钮三级过滤）。
4. **降级可用**：未配置 LLM 时批量对话返回 400 带配置提示、流式对话返回 SSE `error` 事件（均含 hint）；建议引擎自动降级纯规则（记录 model=rule-based）。
5. **防御性编码**：全部外部输入长度/类型约束；SQL 仅经白名单表名/列名或参数化查询。
