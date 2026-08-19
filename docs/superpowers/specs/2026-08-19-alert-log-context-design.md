# 告警详情日志上下文 — 查看命中行前后完整错误日志

## 概述

日志关键词告警（log_keyword）触发后，在告警详情抽屉中展示命中日志行前后各 100 行的完整上下文，命中行与关键词高亮，帮助排障人员直接定位错误现场，无需跳转日志中心手动搜索。

## 动机

当前问题：
- 日志告警的 `description` 只保存命中行本身，`target_info.context` 实际也只存了 1 行
- 告警详情抽屉仅展示描述、目标、时间、投递历史，无法查看完整错误日志
- 排障时需要人工跳转日志中心，按环境/服务/关键词重新搜索，效率低

## 数据链路

### 触发侧（写入命中位置）

`log_collector.py` 扫描日志关键词时，日志行已写入 `log_entries` 表（`_index_log_entries` 在 `_scan_keywords_in_entries` 之前执行），且每条日志的 `rowid` 在索引时已拿到：

1. `_index_log_entries`：将每条日志的 `rowid` 收集进返回值（当前丢弃）
2. `_collect_container`：拿到 rowid 列表，与 entries 一一对应
3. `_scan_keywords_in_entries`：命中时把该行的 `entry_id`（rowid）和 `ts` 传给 `trigger_log_alert`
4. `trigger_log_alert`：新增 `entry_id`、`hit_ts` 参数，写入 `target_info`：

```json
{"env": "prod", "service": "hqb-auth", "keyword": "ERROR",
 "entry_id": 12345, "hit_ts": 1787000000}
```

现有 `target_info.context` 字段保留不动（向后兼容）。

### 查询侧（详情实时查询）

新增接口 `GET /api/alerts/<alert_id>/logs?lines=100`：

- 取告警 `target_info` 与规则 `metric_type`；非 log_keyword 规则或告警缺失位置信息时返回 `available: false`，带 `reason` 区分场景：
  - `not_log_alert`：规则类型非 log_keyword（前端不渲染区块）
  - `no_position`：旧告警无 entry_id/hit_ts（前端提示未记录日志位置）
  - `expired`：before/after 均无数据，命中行也缺失（日志已超保留期被清理）
- `lines` 参数钳制 1~200，默认 100
- 按 `env + service` 过滤 `log_entries`，以 `(ts, id)` 为复合游标取三段：
  - **before**：`ts < hit_ts OR (ts = hit_ts AND id < entry_id)`，`ORDER BY ts DESC, id DESC LIMIT lines`，翻转回正序
  - **hit**：`WHERE id = entry_id` 精确取命中行
  - **after**：`ts > hit_ts OR (ts = hit_ts AND id > entry_id)`，`ORDER BY ts ASC, id ASC LIMIT lines`
- 返回 `{available, reason?, env, service, keyword, hit, before, after, lines}`

复合游标 `(ts, id)` 的必要性：`ts` 是秒级精度，同一秒可能有多条日志（高吞吐错误日志场景），只用 ts 会错位；`id` 自增保证同秒内的稳定次序；按 `env + service` 过滤避免其他容器日志混入。

## 前端设计

`alerts.js` 的 `openAlertDetail` 中，在元信息之后、"通知投递历史"之前新增区块 **"📜 日志上下文"**：

- 打开详情时与投递历史并行加载 `/api/alerts/<id>/logs?lines=100`
- 渲染：等宽字体日志行列表（复用日志中心行样式风格），每行 `[HH:MM:SS] content`，超出高度滚动；content 先 HTML 转义再高亮关键词，避免 XSS
- 命中行高亮：醒目背景色 + 左边框标记
- 关键词高亮：`<mark>` 标签，关键词来自 `target_info.keyword`
- 顶部信息条：`命中行前 N 行 · 后 N 行`（不足时如实显示，如"仅前 32 行"）
- 边界提示（按后端 `reason` 分发）：
  - `not_log_alert` → 不渲染该区块
  - `no_position`（旧告警）→ "该告警未记录日志位置，无法展示上下文"
  - `expired`（超保留期）→ "日志已超出保留期，无法展示上下文"
  - 加载失败 → 错误提示 + 重试按钮（与投递历史区块既有模式一致）

## 边界与错误处理

| 场景 | 处理 |
|---|---|
| 命中行在日志开头/结尾 | before/after 只返回实际存在的行，数量如实标注 |
| 同秒多条日志 | 复合游标 `(ts, id)` 保证前后段不串行 |
| 命中行已被清理但前后行还在 | hit 缺失时用 before 最后一行兜底标记，仍展示；before 空则用 after 第一行 |
| before/after 均无数据 | `reason: expired`，前端提示日志已超保留期 |
| 旧告警（无 entry_id） | `reason: no_position`，前端提示 |
| 非日志类告警 | `reason: not_log_alert`，前端不渲染区块 |
| lines 参数越界 | 后端钳制 1~200 |

## 涉及文件

| 文件 | 改动 |
|------|------|
| `webapp/alert_engine.py` | `trigger_log_alert` 增加 `entry_id`/`hit_ts` 参数并写入 target_info |
| `webapp/log_collector.py` | `_index_log_entries` 返回 rowid 列表；`_scan_keywords_in_entries` 传递命中行 entry_id/ts |
| `webapp/routes/alert_routes.py` | 新增 `GET /api/alerts/<id>/logs` 接口 |
| `webapp/static/js/pages/alerts.js` | 详情抽屉新增"日志上下文"区块及加载逻辑 |
| `webapp/static/css/pages/alerts.css` | 日志上下文区块样式（命中行/关键词高亮） |
| `tests/test_alert_log_context.py` | 新增：三段查询行数/顺序、同秒场景、边界行、旧告警返回 |

## 测试计划

后端单测（`tests/test_alert_log_context.py`）：
- 构造 `log_entries` 数据 + 带 entry_id 的告警记录，验证 before/hit/after 行数与顺序
- 同秒多条日志：验证复合游标不串行
- 命中行在文件开头/结尾：验证缺段返回
- 命中行缺失：验证 before 最后一行兜底标记
- 旧告警（无 entry_id）：验证 `reason: no_position`
- 非日志类规则告警：验证 `reason: not_log_alert`
- before/after 均空：验证 `reason: expired`
- lines 参数越界：验证钳制

前端手工验证：
- 打开日志告警详情：命中行/关键词高亮、滚动、数量标注正确
- 打开旧告警：提示"该告警未记录日志位置"
- 打开指标/状态/任务告警：不渲染日志上下文区块
- 打开已超保留期的告警：提示"日志已超出保留期"

## 不做的事

- 不做告警触发时的日志快照入库（DB 膨胀，且收敛机制下快照不刷新）
- 不做物理文件/归档 gz 读取（保留期 7 天对排障足够，文件读取复杂度不成比例）
- 不做详情内联完整日志查看器（分页/搜索/时间过滤），保留期内的深度排查引导用户去日志中心
- 不做告警 → 日志中心的跳转链接（本期范围外，用户未选择该方案）
