# 告警规则作用域 — 指定服务/服务器配置

## 概述

告警规则的作用域配置从纯文本输入升级为级联多选：按规则类型智能筛选可用作用域，指定服务器/服务时先选环境再选目标，后端匹配改用数字 ID。

## 动机

当前问题：
- 规则 scope 的前端输入是纯文本框（手动填逗号分隔的数字 ID），但后端 `_in_scope` 中 server 类型匹配 hostname、service 类型匹配 service name —— 前端填的数字 ID 永远匹配不上
- 无法按环境维度筛选可选的服务器/服务，配置效率低且易出错

## 数据结构

scope 字段格式（向后兼容）：

| 类型 | 格式 | 说明 |
|------|------|------|
| 全部 | `{type: "all", ids: []}` | 不变 |
| 项目 | `{type: "project", ids: [1,2]}` | ids = project_ids |
| 环境 | `{type: "env", ids: [1,2,3]}` | ids = env_ids |
| 服务器 | `{type: "server", ids: [3,4,5], env_ids: [1]}` | ids = server_ids，env_ids 为级联筛选的环境 |
| 服务 | `{type: "service", ids: [10,11], env_ids: [1]}` | ids = service_ids，env_ids 为级联筛选的环境 |

向后兼容规则：
- `env_ids` 缺失时视为空数组（不做环境过滤）
- 旧规则的 ids 中若存的是字符串（hostname/service name），引擎降级为旧逻辑匹配
- 编辑旧规则并保存后，自动切换到新的数字 ID 格式

## 前端设计

### 作用域类型按规则类型智能筛选

| 规则类型 | 可选作用域类型 |
|----------|----------------|
| 指标（metric） | 全部 / 项目 / 环境 / 服务器 |
| 状态（status） | 全部 / 项目 / 环境 / 服务 |
| 日志关键词（log_keyword） | 全部 / 项目 / 环境 / 服务 |

### 级联选择交互

选「全部」「项目」「环境」时：与现有交互类似，改为多选下拉（使用项目已有的 enhanceSelect 组件）。

选「服务器」或「服务」时：
1. 先展示环境多选标签（从全部环境中选择）
2. 选中环境后，自动加载该环境下的服务器/服务列表
3. 在结果中多选目标服务器/服务
4. 取消某环境标签时，该环境下的选项自动移除

### 规则列表展示

规则列表中的作用域列增加展示 scope 详情：
- 服务器类型：显示「服务器(N)：host1, host2, ...」
- 服务类型：显示「服务(N)：svc1, svc2, ...」

## 后端设计

### 新增 API 端点

1. `GET /api/alerts/scope/servers?env_ids=1,2`
   - 返回指定环境下的服务器列表
   - 响应：`[{id: 3, host: "192.168.1.100"}, ...]`
   - 数据来源：env_servers + servers 表 JOIN

2. `GET /api/alerts/scope/services?env_ids=1,2`
   - 返回指定环境下部署的服务列表
   - 响应：`[{id: 10, name: "hqb-auth"}, ...]`
   - 数据来源：通过 env_ids 查出关联的 project_id 集合，再查 services 表（WHERE project_id IN ...），去重返回
   - 注意：services 表按 project 组织，环境通过 project_id 间接关联

3. `GET /api/alerts/scope/environments`
   - 返回全部环境列表（供级联选择第一级使用）
   - 响应：`[{id: 1, name: "prod", project_name: "hqb"}, ...]`

### `_in_scope` 方法改造

```
_in_scope(rule, env, server_id=None, service_id=None, host=None, service=None)
```

匹配逻辑：
1. `type == "all"` → 放行
2. `type == "project"` → 检查 env.project_id 是否在 ids 中
3. `type == "env"` → 检查 env.id 是否在 ids 中
4. `type == "server"` → 先检查 env_ids（若非空，env.id 须在其中），再检查 server_id 是否在 ids 中
5. `type == "service"` → 先检查 env_ids（若非空，env.id 须在其中），再检查 service_id 是否在 ids 中

向后兼容：
- 如果 ids 中的元素是字符串（非数字），降级为旧逻辑（hostname/service name 匹配）
- server_id 或 service_id 为 None 时，降级使用 host/service name 匹配

### 引擎评估适配

`_evaluate_metric_rules`：
- 构建 hostname → server_id 映射（从 servers 表）
- 调用 `_in_scope` 时传入 server_id

`_evaluate_status_rules`：
- 构建 service name → service_id 映射（从 services 表）
- 调用 `_in_scope` 时传入 service_id

`trigger_log_alert`：
- 在此方法内部查询 service name → service_id 映射（查 services 表 WHERE name=service），无需调用方改动

### 规则校验更新

`_validate_rule` 中 scope 校验：
- 允许 env_ids 字段（须为整数列表）
- server/service 类型的 ids 须为整数列表（新规则）

## 涉及文件

| 文件 | 改动 |
|------|------|
| `webapp/alert_engine.py` | `_in_scope` 改造 + 引擎评估适配（server_id/service_id 解析） |
| `webapp/routes/alert_routes.py` | 新增 3 个 scope 数据 API + 规则校验更新 |
| `webapp/static/js/pages/alerts.js` | 规则抽屉作用域区域重写 + 规则列表 scope 展示 |
| `webapp/static/css/pages/alerts.css` | 级联选择器样式 |
| `tests/test_alert_engine.py` | 新增 scope 匹配测试用例 |
| `tests/test_alert_routes.py` | 新增 scope API 测试用例 |

## 不做的事

- 不做 scope 的批量导入/导出格式变更（现有 YAML 导出保持兼容）
- 不做作用域的拖拽排序
- 不做跨规则的作用域模板/复制功能
