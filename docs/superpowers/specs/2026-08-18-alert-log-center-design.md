# 智能告警系统与日志中心设计规格

> 日期：2026-08-18
> 状态：已由用户确认（逐节确认）
> 范围：智能告警系统（规则引擎/分级/升级收敛/静默/多渠道/确认跟踪）+ 日志中心（SSH 采集/全文检索/上下文/告警联动/归档清理），以及现有 alerts 页前端动态告警向后端规则引擎的迁移
> 硬约束：不引入任何新基础设施（无 ES/Loki/Prometheus），全部在现有 Flask 单进程内实现；零新 Python 依赖；Docker/Windows 双形态部署不变

## 1. 背景与目标

现有 `notify.py` 仅做发布事件通知（飞书/钉钉），`monitor.py` 已有 30s 周期探测（主机 CPU/内存/磁盘/负载、容器资源、服务在线状态、Nacos 实例、HTTP 健康检查）但数据仅存进程内缓存、无持久化无告警；`alerts.js` 的告警页是纯前端动态计算（任务失败+服务离线），无后端存储、无确认跟踪、无规则；`task_manager.py` 仅支持单任务日志文件查看，无集中式日志管理。

**目标：**

1. **智能告警系统**：基于阈值的规则引擎（CPU>80% 持续 5 分钟、服务下线、磁盘满等）、P0-P3 分级、升级/收敛、静默/抑制（维护窗口）、多渠道通知（邮件/短信/企业微信/Webhook，保留飞书/钉钉）、告警确认与处理跟踪（谁确认、何时处理）。
2. **日志中心**：从各服务器容器实时采集日志（docker logs）、全文检索（关键字/时间范围/服务名）、错误日志前后文关联查看、日志 ERROR/Exception 自动触发告警、历史日志自动归档与过期清理。

## 2. 关键决策（与用户确认）

| 决策点 | 结论 |
|---|---|
| 范围组织 | 整体设计、分阶段实施（先告警引擎+渠道，再日志采集+检索，最后联动+归档） |
| 实现方案 | 方案 A：自研轻量引擎（进程内规则评估+SSH 增量采集+FTS5 索引），不引入外部组件 |
| 日志采集 | 平台主动 SSH 拉取 `docker logs --since` 增量，复用现有 SSH 凭据与连接池 |
| 存储检索 | 按日期目录文件存储 + SQLite FTS5 全文索引（正文存文件，表存元数据定位） |
| 通知渠道 | 邮件 SMTP、企业微信机器人、通用 Webhook、阿里云短信（+保留飞书/钉钉复用） |
| 规则配置 | UI 可视化配置 + 内置默认规则种子（可编辑/禁用） |
| 生命周期 | firing → acked → resolved 状态机；指标恢复自动 resolved；ack 超时升级 |
| 静默 | 一次性+周期窗口，按规则/环境/服务器作用域；窗口内不通知但记录，结束后仍在 firing 补发 |
| 保留策略 | 按日期目录 + gzip 归档（默认保留 7 天、归档 30 天，服务级可覆盖） |
| 日志联动 | 服务级 `log_error_keywords` 默认 + 规则引擎可覆盖/追加 |
| 存量告警 | 任务失败/服务下线迁移到后端规则引擎，notify.py 事件通知保留 |
| 采集频率 | 与监控同周期（30s），日志量大的服务可配置低频（60s/120s） |

## 3. 技术方案

| 项 | 决策 |
|---|---|
| 技术栈 | 原生 HTML/CSS/JS + Python 标准库 + SQLite FTS5，零新依赖 |
| 新增模块 | `alert_engine.py`（规则评估/状态机/升级收敛/静默）、`alert_channels.py`（通知渠道）、`log_collector.py`（SSH 增量采集/FTS5 索引/归档）、`alert_routes.py`、`log_routes.py` |
| 调度 | 扩展现有 `MonitorScheduler` 守护线程模式：`AlertEvaluator` 30s 评估（读 monitor 探测缓存）、`LogCollector` 30s 采集（复用 SSH 凭据）、每日归档清理任务 |
| 数据流 | 探测缓存 → AlertEvaluator 滑动窗口判定 → alerts 表 + 通知渠道分发；采集文件 → FTS5 + log_entries → 检索 API |
| 迁移 | 复用 db.py 幂等迁移模式新增 7 张表 + services 表加列；内置默认规则种子首次启动自动导入 |
| 前端 | alerts 页改造（列表/确认/解决/规则管理/静默窗口/历史统计）+ logs 页新增服务日志 Tab |

## 4. 数据模型（7 张新表）

### alert_rules（告警规则）
```
id, name, metric_type(metric/status/log_keyword), compare(>/</>=/<=), threshold,
duration_seconds(持续多久才触发), scope(JSON: 全部/项目/环境/服务器/服务),
severity(P0-P3), channels(JSON 渠道列表), escalation_interval(分钟, 默认30),
enabled, remark, created_at, updated_at
```

### alerts（告警实例，按指纹收敛后的单条活跃告警）
```
id, rule_id, fingerprint(收敛键: 规则+目标+关键词), status(firing/acked/resolved),
severity, subject, description, target_info(JSON: 服务器/服务/容器/上下文行),
first_seen_at, last_seen_at, ack_by, ack_at, resolved_by, resolved_at,
resolved_type(auto/manual), notify_count, escalation_level, escalation_at
```

### alert_events（通知记录）
```
id, alert_id, channel, target, payload(JSON), ok, detail, created_at
```

### silence_windows（静默窗口）
```
id, name, type(one_time/recurring), start_at, end_at, weekdays(JSON, 周期窗口用),
scope(JSON: 规则/环境/服务器), enabled, remark, created_at
```

### log_entries（日志索引元数据，正文存文件）
```
id, ts, env, service, container, file_path, line_offset, line_count, keywords(JSON 命中)
```
全文索引用 FTS5 虚拟表 `log_fts(ts, env, service, content)`，与主表 rowid 关联。

### log_scan_state（采集游标，进程重启可恢复）
```
id, env, server, container, service, last_ts, last_line, updated_at
```

### platform_settings（平台设置，key-value，存通知渠道配置等）
```
id, key(唯一), value(JSON), updated_at
```

**既有表扩展**（幂等迁移加列）：
- `services` 表新增：`log_retention_days`（覆盖全局保留期，默认 NULL 用全局值）、`log_collect_interval`（低频采集秒数，默认 NULL 用全局 30s）
- `log_error_keywords` 字段为已有列，直接复用

## 5. 告警引擎核心逻辑

### 5.1 规则评估（AlertEvaluator 每 30s 一轮）

- **指标类规则**（CPU/内存/磁盘/负载）：读 monitor 探测缓存，用滑动窗口历史环形缓冲判断**连续满足阈值 ≥ duration_seconds** 才触发，避免抖动。
- **状态类规则**（服务下线/容器未部署/容器异常退出）：monitor 缓存服务状态（running/not_deployed/unknown），状态异常即触发，duration_seconds 可选。
- **日志关键词规则**：由 LogCollector 轮次触发（见 §7 日志告警联动），命中即触发，含上下文前后 5 行。

### 5.2 状态机

```
firing ──ack──▶ acked ──resolve──▶ resolved
   │              │
   └──指标恢复──▶ resolved (resolved_type=auto)
```

- **ack**：记录 `ack_by`（当前登录用户）/`ack_at`，ack 后停止重复通知。
- **resolve**：人工解决记录人/时间；指标恢复自动 resolved。
- **收敛**：同 `fingerprint`（规则+目标+关键词）只保留一条活跃告警，重复触发仅更新 `last_seen_at`。

### 5.3 升级

- ack 后超过 `escalation_interval`（默认 30 分钟）仍未 resolved → severity 提升一级（最多 P0）、`escalation_level` +1，按新级别重新通知并记录 `alert_events`。

### 5.4 静默窗口

- 告警首次触发及每次通知前判断是否命中窗口（一次性：start_at<=now<=end_at；周期：weekdays+每日起止）。
- 作用域匹配：窗口 scope 与告警的规则/环境/服务器任意匹配即生效。
- 窗口内：照常记录 alerts（firing）、不发送通知；窗口结束后仍在 firing → 补发一次。

### 5.5 默认规则种子（首次启动自动导入）

| 规则 | 阈值 | 级别 |
|---|---|---|
| 主机 CPU 过高 | >80% 持续 5min | P2 |
| 磁盘使用率过高 | >90% 持续 5min | P2 |
| 服务下线 | 状态异常立即 | P1 |
| 容器异常退出 | 状态异常立即 | P2 |
| 日志 ERROR 命中 | 关键词即触发 | P3 |

### 5.6 通知渠道（alert_channels.py）

统一接口 `send(alert, channel_config) -> (ok, detail)`；渠道：邮件 SMTP（标准库 smtplib）、企业微信机器人（POST JSON，与飞书/钉钉同模式）、通用 Webhook（POST + 自定义 JSON 模板）、阿里云短信（HTTP 调用，AccessKey/签名/模板 Code）、飞书/钉钉复用 notify.py。

渠道配置存 `platform_settings` 表（key 如 `channel.email`、`channel.sms_aliyun`），沿用 notify.py 的 `env_or_config` 模式支持环境变量覆盖（如 `ALERT_SMTP_HOST`、`ALERT_ALIYUN_AK`），UI 提供渠道配置表单；发送结果写 alert_events；失败重试 2 次指数退避。

## 6. 日志中心

### 6.1 采集（LogCollector 每 30s）

- 读 log_scan_state 游标 → 对每台服务器的在线容器执行 `docker logs --since <游标> -t --no-color <容器>` → 解析追加到文件 → 更新游标（基于容器日志时间戳，重启不丢不重）。
- 降级容错：单容器失败跳过不阻塞其他；连续 3 轮失败记采集日志不触发业务告警。
- 磁盘保护：单服务日文件 >200MB 滚动（`service-YYYY-MM-DD-NNN.log`）；单容器单轮拉取上限 5MB。

### 6.2 存储与检索

```
data/logs/collect/<env>/<service>/YYYY-MM-DD[-NNN].log     ← 活跃
data/logs/archive/<env>/<service>/YYYY-MM-DD[-NNN].log.gz  ← 归档
```

- 每轮采集后批量写 log_entries + FTS5；关键词命中检测同轮完成。
- 检索 API：`GET /api/logs?env=&service=&keyword=&from=&to=&page=`，FTS5 MATCH + 时间/服务过滤，归档按需解压。
- 上下文 API：`GET /api/logs/context?env=&service=&ts=&lines=` 按行偏移取前后 N 行。

### 6.3 归档与清理（每日定时）

- 超过保留期（默认 7 天，服务表 `log_retention_days` 可覆盖）→ gzip 转 archive，清理对应 FTS5/元数据。
- 归档超过归档期（默认 30 天）→ 删除文件。

## 7. 日志告警联动

- **默认**：采集轮内按服务表 `log_error_keywords` 匹配增量日志行，命中 → 生成日志关键词告警（含上下文 5 行 + 文件定位）。
- **规则覆盖/追加**：`metric_type=log_keyword` 规则（作用域可跨服务/按环境）命中后按规则级别/渠道通知；规则停用回落默认关键词。
- **去重收敛**：同服务同关键词 30 分钟内只触发一次（更新 last_seen_at 不重复通知）。

## 8. 前端改造

### alerts 页（改造现有）

- 告警列表：级别徽标（P0-P3 红橙黄蓝）、状态（firing/acked/resolved）、收敛计数、时间线。
- 操作：确认（ack）、解决（resolve）、单条静默 24h；筛选（级别/状态/时间/服务器）。
- 新增 Tab：**规则管理**（增删改查、启停）、**静默窗口**（一次性/周期、作用域选择）。
- 历史统计：按级别/天聚合的告警趋势（ECharts，复用现有图表模式）。

### logs 页（改造现有）

- 新增「服务日志」Tab：环境/服务/容器 + 关键词 + 时间范围 → 检索结果列表；点击行 → 上下文抽屉（前后 5 行，命中行高亮）。
- 与任务日志 Tab、审计日志 Tab 并存。

## 9. 错误处理与测试

- **错误处理**：通知失败重试 2 次指数退避；SSH 采集失败隔离不级联；规则评估异常单条跳过记日志；后台线程异常不退出（wrap + 重入保护）。
- **测试**（Python 单测，无新依赖）：规则滑动窗口判定、状态机迁移（firing→acked→resolved→auto）、升级触发、收敛合并、静默窗口匹配（一次性/周期/作用域）；增量解析、游标推进、归档滚动、FTS5 检索与上下文取行；渠道 payload 构造与失败重试（mock 网络）。

## 10. 分阶段实施

1. **阶段一（告警引擎）**：db.py 迁移 + alert_engine.py + alert_channels.py + alert_routes.py + alerts 页改造 + 默认规则种子。
2. **阶段二（日志中心）**：log_collector.py + log_routes.py + logs 页服务日志 Tab + 归档清理。
3. **阶段三（联动收尾）**：日志关键词联动 + 历史统计图表 + 端到端联调。
