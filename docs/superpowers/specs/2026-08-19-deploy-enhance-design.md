# 发布流程增强 — 设计规格

## 概述

在现有发布骨架（`task_manager.py` + `deploy_pipeline.py` + `Deployer`）基础上，增加 6 项企业级发布管控能力：角色权限管控、定时/周期发布、发布窗口管控、发布影响分析、灰度自动推进、发布对比。采用**内置调度器 + 可选外部 webhook** 混合模式，与现有 `MonitorScheduler` / `AlertEngine` 架构一致。

---

## 1. 需求决策汇总

| 功能 | 决策 |
|------|------|
| 审批流程 | 仅角色权限管控（admin/operator 可发布，viewer 不可），不引入审批状态机 |
| 定时发布 | 一次性定时 + 周期性计划，两种模式 |
| 窗口管控 | 软警告 + 用户确认后可强制执行 |
| 影响分析 | 共机分析 + 依赖拓扑图（dubbo_services + 手动配置） |
| 灰度自动化 | 时间窗口 + 健康检查双条件满足才自动推进 |
| 发布对比 | 镜像版本 + Nacos/环境配置差异 |
| 实现模式 | 内置调度器 + 外部 webhook 触发接口（为未来 CI/CD 集成预留） |

---

## 2. 数据模型

### 2.1 `deploy_schedules` — 定时/周期发布计划

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 所属项目 |
| type | TEXT | `once`（一次性）/ `cron`（周期性） |
| name | TEXT | 计划名称 |
| ref_type / ref_id | TEXT / INTEGER | 引用 project 或 pipeline |
| env | TEXT | 目标环境 |
| version / previous_version | TEXT | 版本号 |
| extra | TEXT (JSON) | 额外参数（--auto-version 等） |
| schedule_at | INTEGER | once 模式：执行时间戳 |
| cron_expr | TEXT | cron 模式：表达式（如 `0 2 * * 3`） |
| enabled | INTEGER | 是否启用 |
| last_run_at / next_run_at | INTEGER | 上次/下次执行时间 |
| created_by | INTEGER FK → users | 创建人 |
| created_at | INTEGER | 创建时间 |

### 2.2 `release_windows` — 发布窗口/封网期

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 所属项目（0=全局） |
| name | TEXT | 窗口名称（如"春节封网"） |
| start_at / end_at | INTEGER | 起止时间戳 |
| block_level | TEXT | `warn`（软警告）/ `block`（硬拦截） |
| repeat_yearly | INTEGER | 是否每年重复（用于节假日） |
| created_by | INTEGER FK → users | 创建人 |
| created_at | INTEGER | 创建时间 |

### 2.3 `service_dependencies` — 服务依赖关系

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 所属项目 |
| source_service | TEXT | 调用方服务名 |
| target_service | TEXT | 被调用方服务名 |
| dep_type | TEXT | `dubbo` / `http` / `manual` |
| created_at | INTEGER | 创建时间 |

UNIQUE(source_service, target_service, project_id)

### 2.4 `deployment_snapshots` — 发布快照（扩展）

在现有基础上增加 `nacos_config_hash` 字段，用于记录发布时的 Nacos 配置摘要。

| 新增字段 | 类型 | 说明 |
|----------|------|------|
| nacos_config_hash | TEXT | 发布时 Nacos 配置的 MD5 摘要 |
| env_vars_hash | TEXT | 环境变量的 MD5 摘要 |

### 2.5 `canary_configs` — 灰度策略配置

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 自增主键 |
| project_id | INTEGER FK | 所属项目 |
| name | TEXT | 策略名称 |
| steps | TEXT (JSON) | 批次步骤定义 |
| is_default | INTEGER | 是否为默认策略 |
| created_at | INTEGER | 创建时间 |

`steps` JSON 格式：
```json
[
  {"batch": 1, "servers": 1, "observe_minutes": 5, "auto_advance": true},
  {"batch": 2, "servers": 2, "observe_minutes": 10, "auto_advance": true},
  {"batch": 3, "servers": "all", "observe_minutes": 0, "auto_advance": false}
]
```

---

## 3. 后端架构

### 3.1 DeployScheduler 调度器

延续 `MonitorScheduler` / `AlertEngine` 的守护线程模式，新增 `DeployScheduler`：

```
DeployScheduler._loop()  每 30s 一轮：
  1. 扫描 deploy_schedules 中 enabled=1 且 next_run_at <= now 的计划
  2. 对每个到期计划：
     a. 检查 release_windows → 命中窗口？
        - block_level=block → 跳过，记录日志
        - block_level=warn → 创建任务标记 forced=1，正常执行
        - 未命中 → 正常创建任务
     b. 调用 task_manager.submit_task() 创建并提交任务
     c. 更新 last_run_at / next_run_at
     d. cron 模式：根据 cron_expr 计算下一次执行时间
     e. once 模式：执行后 enabled=0（单次）
```

### 3.2 任务生命周期扩展

```
现有流程：  pending → running → success/failed
新增分支：
  定时任务：  scheduled → (到期) → pending → running → ...
  灰度任务：  running → canary_waiting → (双条件通过) → running → ...
                                  → (超时未通过) → paused → 人工介入
```

### 3.3 Webhook 触发接口

新增 REST API 端点，供外部 CI/CD 系统调用：

```
POST /api/deploy/webhook
  Headers: X-Webhook-Token: <configured_token>
  Body: {
    "project_id": 1,
    "env": "prod",
    "version": "v1.2.3",
    "pipeline_id": 5,        // 可选
    "extra": {}               // 可选
  }
  → 验证 token → 检查 release_windows → 创建任务 → 返回 task_id
```

---

## 4. 核心业务逻辑

### 4.1 灰度自动推进（双条件）

扩展现有 `Deployer` 的金丝雀逻辑：

```
灰度执行流程：
  1. 读取 canary_config.steps，例如：
     [{batch:1, servers:1, observe:5min, auto:true},
      {batch:2, servers:2, observe:10min, auto:true},
      {batch:3, servers:all, observe:0, auto:false}]  ← 最后一批人工确认

  2. 每批次执行：
     a. 部署到目标服务器（调用现有 deploy_one_server）
     b. 任务状态 → canary_waiting
     c. 启动观察计时器

  3. 双条件判定（每 30s 检查一次）：
     条件 A：observe 时间已过（started_at + observe_minutes <= now）
     条件 B：健康检查通过（HTTP 探活 + monitor 缓存无告警）
     → 两个条件同时满足 → 自动推进下一批
     → observe 超时未通过（如 2x observe_minutes）→ 暂停等待人工介入

  4. 最后一批（auto=false）：
     部署完成后不自动推进，等待人工确认全量发布成功
```

### 4.2 发布影响分析

```
发布前影响分析：
  1. 共机分析：查询 servers 表，找出目标服务部署的服务器，
     列出同机部署的其他服务
  2. 依赖拓扑：查询 service_dependencies 表 + services.dubbo_services 字段，
     构建有向图，展示上下游调用链
  3. 影响范围：基于拓扑图 BFS 遍历，计算 1 跳 / 2 跳影响范围
  4. 返回结构化数据供前端渲染拓扑图
```

### 4.3 发布对比

```
发布对比逻辑：
  1. 每次发布成功后，在 deployment_snapshots 中记录：
     - 镜像版本（各服务的 docker image tag）
     - nacos_config_hash（Nacos 配置摘要）
     - env_vars_hash（环境变量摘要）
  2. 对比时，选取两次发布记录，逐字段比较：
     - 镜像版本变化：升级 / 回滚 / 未变
     - Nacos 配置：hash 不同则标记"配置已变更"，展示差异详情
     - 环境变量：同上
```

### 4.4 发布窗口管控

```
窗口检查逻辑：
  1. 手动发布 / 定时调度 触发时，检查 release_windows：
     - 当前时间是否落在某个窗口区间内
     - repeat_yearly=1 的窗口，将年份替换为当前年份后比较
  2. block_level=block → 拒绝执行，返回错误信息
  3. block_level=warn → 返回警告信息，前端弹窗确认
     - 用户确认后带 force=true 参数重新提交
     - 任务标记 forced=1 记录在案
```

---

## 5. 前端页面设计

### 5.1 页面结构

新增"发布中心"导航入口，下设子页面：

| 页面 | 路径 | 功能 |
|------|------|------|
| 定时计划 | /deploy/schedules | 定时/周期计划的 CRUD + 启停 |
| 发布窗口 | /deploy/windows | 封网期/窗口期的日历视图 + 列表 |
| 灰度策略 | /deploy/canary | 灰度批次配置 + 可视化 |
| 依赖拓扑 | /deploy/topology | 服务依赖关系管理 + 影响分析 |
| 发布对比 | /deploy/compare | 选择两次发布进行差异对比 |

### 5.2 定时发布管理页

- 顶部工具栏：环境筛选 + 新建计划按钮
- 计划列表表格：名称、类型（一次性/周期性）、目标环境、下次执行时间、状态（启用/停用）、操作
- 新建弹窗：类型切换（once/cron）、项目/环境选择、版本号、执行时间/CRON 表达式配置
- 操作：编辑、删除、启用/停用、立即执行

### 5.3 发布窗口管控页

- 月历视图：用色块标记封网期（红色=block，黄色=warn）
- 窗口列表：名称、时间范围、拦截级别、操作
- 新建/编辑弹窗：名称、时间范围、是否每年重复、拦截级别选择

### 5.4 窗口软警告弹窗

- 手动发布时若命中 warn 级别窗口，弹出确认对话框
- 显示窗口名称、时间范围
- 提供"强制发布"按钮（带二次确认）和"取消"按钮

### 5.5 灰度策略配置页

- 策略列表 + 新建/编辑
- 策略编辑区：可视化批次条（1台→2台→全量），每批可配置观察时间和是否自动推进
- 拖拽调整批次顺序

### 5.6 依赖拓扑 & 影响分析页

- SVG 力导向图展示服务依赖关系
- 点击节点高亮上下游调用链
- 右侧面板：共机分析、上下游服务列表
- 支持手动添加/编辑依赖关系

### 5.7 发布对比页

- 两个下拉选择器选择要对比的发布记录（或"最近两次"）
- 对比表格：服务名、旧版本、新版本、变化标记（↑升级 ↓回滚 =未变）
- Nacos 配置差异展示（如有变更）

---

## 6. API 设计

### 6.1 定时计划

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/deploy/schedules | 获取计划列表 |
| POST | /api/deploy/schedules | 创建计划 |
| PUT | /api/deploy/schedules/:id | 更新计划 |
| DELETE | /api/deploy/schedules/:id | 删除计划 |
| POST | /api/deploy/schedules/:id/toggle | 启用/停用 |
| POST | /api/deploy/schedules/:id/run | 立即执行 |

### 6.2 发布窗口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/deploy/windows | 获取窗口列表 |
| POST | /api/deploy/windows | 创建窗口 |
| PUT | /api/deploy/windows/:id | 更新窗口 |
| DELETE | /api/deploy/windows/:id | 删除窗口 |
| GET | /api/deploy/windows/check | 检查当前是否在窗口内 |

### 6.3 灰度策略

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/deploy/canary-configs | 获取策略列表 |
| POST | /api/deploy/canary-configs | 创建策略 |
| PUT | /api/deploy/canary-configs/:id | 更新策略 |
| DELETE | /api/deploy/canary-configs/:id | 删除策略 |

### 6.4 依赖拓扑

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/deploy/topology/:project_id | 获取项目拓扑数据 |
| POST | /api/deploy/topology | 添加依赖关系 |
| DELETE | /api/deploy/topology/:id | 删除依赖关系 |
| GET | /api/deploy/impact/:project_id | 发布影响分析（共机+上下游） |

### 6.5 发布对比

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/deploy/snapshots/:project_id | 获取发布快照列表 |
| GET | /api/deploy/compare | 对比两次发布记录 |

### 6.6 Webhook

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/deploy/webhook | 外部系统触发发布 |

---

## 7. 文件结构

### 7.1 新增文件

| 文件 | 职责 |
|------|------|
| `webapp/deploy_scheduler.py` | DeployScheduler 守护线程 |
| `webapp/routes/deploy_enhance_routes.py` | 发布增强 API 路由 |
| `webapp/static/js/pages/deploy_schedules.js` | 定时计划前端逻辑 |
| `webapp/static/js/pages/deploy_windows.js` | 发布窗口前端逻辑 |
| `webapp/static/js/pages/deploy_canary.js` | 灰度策略前端逻辑 |
| `webapp/static/js/pages/deploy_topology.js` | 依赖拓扑前端逻辑 |
| `webapp/static/js/pages/deploy_compare.js` | 发布对比前端逻辑 |
| `webapp/static/css/pages/deploy-enhance.css` | 发布增强样式 |

### 7.2 修改文件

| 文件 | 修改内容 |
|------|---------|
| `webapp/db.py` | 新增 5 张表的建表语句 + migration |
| `webapp/task_manager.py` | 新增 `scheduled` / `canary_waiting` / `paused` 状态处理 |
| `deploy_pipeline.py` | Deployer 增加灰度自动推进逻辑 |
| `webapp/app.py` | 注册新路由 + 启动 DeployScheduler |
| `webapp/static/js/app.js` | 添加"发布中心"导航入口 |
| `webapp/static/js/router.js` | 添加新页面路由 |
| `webapp/static/js/layout.js` | 添加侧边栏菜单项 |

---

## 8. 错误处理

| 场景 | 处理方式 |
|------|---------|
| 定时任务执行时命中 block 窗口 | 跳过执行，记录日志，更新 next_run_at |
| 灰度观察期健康检查失败 | 任务状态 → paused，通知相关人员，等待人工介入 |
| 灰度观察期超时（2x observe） | 同上 |
| Webhook token 无效 | 返回 401 |
| 依赖拓扑循环依赖 | 添加时检测，拒绝并提示 |
| CRON 表达式无效 | 创建时校验，拒绝无效表达式 |

---

## 9. 测试计划

| 测试项 | 验证内容 |
|--------|---------|
| 定时调度 | once 模式准时触发、cron 模式正确计算下次时间 |
| 窗口管控 | block 窗口拒绝执行、warn 窗口 force=true 可绕过 |
| 灰度推进 | 双条件满足后自动推进、健康检查失败暂停 |
| 影响分析 | 拓扑图正确展示依赖、BFS 影响范围计算准确 |
| 发布对比 | 镜像版本差异正确标记、配置 hash 对比准确 |
| Webhook | token 验证、正常创建任务、窗口检查 |
| 权限 | viewer 无法访问发布相关 API |

---

## 10. 约束与假设

- **角色体系**：复用现有 admin/operator/viewer，admin 和 operator 拥有发布权限
- **健康检查数据源**：复用 `monitor.py` 的探活结果和 `alert_engine.py` 的告警缓存
- **CRON 解析**：使用 Python `croniter` 库（需加入 requirements.txt）
- **拓扑图渲染**：前端使用 SVG + 力导向算法（d3-force 或自实现简化版）
- **单进程约束**：DeployScheduler 与 MonitorScheduler 一样在主进程内以守护线程运行
