# 到期监控系统设计规格

## 概述

为 agencys-deploy 平台新增通用的「到期监控」功能，支持监控 SSL 域名证书到期、云产品续费、License 授权到期、自定义截止日等场景。用户可配置提前 N 天通知、通知渠道、告警级别等规则，支持全局默认 + 单项覆盖。

## 背景

### 现有 SSL 监控

项目已有基础 SSL 证书监控：
- `monitor.py` 通过 SSH 连接服务器，用 `openssl s_client` 检测 443 端口证书到期时间
- `alert_engine.py` 有内置规则「SSL 证书即将到期」（`metric="ssl_days"`, `threshold=7`）
- 数据存入 `monitor_snapshots` 表的 `ssl_days` 字段

### 现有方案的局限

1. **依赖 SSH**：只能检测服务器上 443 端口的证书，无法独立监控任意域名
2. **功能单一**：仅支持 SSL 证书，无法监控云产品续费、License 到期等
3. **缺少管理界面**：没有集中的域名列表管理和证书状态展示

### 目标

1. 保留现有 SSH 级 SSL 探测（服务器维度）
2. 新增独立的域名 SSL 探测（HTTPS 直连，不依赖 SSH）
3. 泛化为通用到期监控系统，支持多种监控类型
4. 在告警中心新增「到期监控」Tab 统一管理

## 数据模型

### 新增 `custom_monitors` 表

```sql
CREATE TABLE IF NOT EXISTS custom_monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,                -- 监控类型: ssl / cloud_renewal / license / custom
    name TEXT NOT NULL,                -- 显示名称
    target TEXT NOT NULL,              -- 监控目标（域名/产品标识/描述）
    port INTEGER DEFAULT 443,         -- 端口（仅 ssl 类型有效）

    -- 到期信息
    expiry_at INTEGER,                -- 到期时间戳（自动探测或手动填写）
    days_left REAL,                   -- 剩余天数（自动计算或手动填写）

    -- 通知规则覆盖（0/空 = 跟随全局默认）
    warn_days INTEGER DEFAULT 0,      -- 提前通知天数
    severity TEXT DEFAULT '',         -- 告警级别
    channels TEXT DEFAULT '[]',       -- 通知渠道（JSON 数组）

    -- 状态
    enabled INTEGER DEFAULT 1,
    last_checked INTEGER,             -- 最近自动检测时间戳
    detail TEXT DEFAULT '{}',         -- 类型相关的附加信息（JSON）
    error TEXT DEFAULT '',            -- 最近检测错误信息
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(type, target, port)
);
```

### 监控类型定义

| type | 说明 | 数据来源 | detail JSON 示例 |
|------|------|----------|-----------------|
| `ssl` | SSL 域名证书 | 自动探测 | `{"cert_subject": "CN=api.example.com", "cert_issuer": "Let's Encrypt"}` |
| `cloud_renewal` | 云产品续费 | 手动填写 | `{"provider": "阿里云", "product": "ECS", "instance_id": "ecs-bp1xxx"}` |
| `license` | License/授权到期 | 手动填写 | `{"vendor": "JetBrains", "product": "IDEA Ultimate"}` |
| `custom` | 自定义截止日 | 手动填写 | `{"note": "域名 example.com 续费截止"}` |

### 全局配置

存储于 `platform_settings` 表，key = `custom_monitor_config`：

```json
{
  "default_warn_days": 30,
  "default_severity": "P1",
  "default_channels": ["feishu"],
  "ssl_check_interval": 3600
}
```

- `default_warn_days`：全局默认提前通知天数
- `default_severity`：全局默认告警级别
- `default_channels`：全局默认通知渠道
- `ssl_check_interval`：SSL 自动探测间隔（秒）

### 与现有数据的关系

- `monitor_snapshots.ssl_days`：SSH 探测数据，保持不变
- `alert_rules` 中 `metric="ssl_days"` 规则：继续处理 SSH 探测数据
- `custom_monitors` 表：独立域名/到期监控数据
- 两种来源的告警统一显示在告警列表中

## SSL 自动探测模块

### 新增 `webapp/ssl_probe.py`

核心函数 `probe_ssl(domain, port, timeout)`：
- 使用 Python `ssl` 标准库创建 TLS 连接
- 获取证书信息：`subject`（CN）、`issuer`、`notAfter`（到期时间）
- 计算剩余天数
- 返回 `{cert_subject, cert_issuer, cert_expiry, days_left, error}`
- 异常捕获：连接超时、证书无效、DNS 解析失败等均返回 error 字段

### 新增 `webapp/ssl_monitor.py`

`SSLMonitor` 守护线程：
- 在 `app.py` 初始化时启动（与 monitor、alert_engine 同级）
- 每 `ssl_check_interval` 秒（默认 3600）执行一轮
- 遍历 `custom_monitors` 表中 `type="ssl"` 且 `enabled=1` 的记录
- 调用 `probe_ssl()` 更新记录：`last_checked`、`expiry_at`、`days_left`、`detail`、`error`
- 探测结果同时写入 `days_left` 字段供告警引擎直接读取
- 线程安全：独立数据库连接（与 monitor 相同模式）

## 告警引擎集成

### 扩展 `alert_engine.py`

#### `_cycle()` 新增步骤

```python
def _cycle(self):
    snap = self._load_snapshot()
    self._evaluate_metric_rules(snap)
    self._evaluate_status_rules(snap)
    self._evaluate_custom_monitors(snap)   # 新增
    self._check_tasks(snap)
    self._auto_resolve(snap)
    self._escalate(snap)
```

#### `_load_snapshot()` 扩展

新增 `custom_monitors` 和 `custom_monitor_config` 到快照：

```python
snap["custom_monitors"] = dbm.list_records(self._db_path, "custom_monitors",
                                            "enabled=1 AND days_left IS NOT NULL")
snap["custom_monitor_config"] = self._load_custom_monitor_config()
```

#### `_evaluate_custom_monitors(snap)` 逻辑

1. 从 `snap["custom_monitors"]` 读取已加载的监控项
2. 从 `snap["custom_monitor_config"]` 读取全局配置
3. 对每个监控项：
   - 计算有效阈值：`warn_days > 0` 用域名级，否则用全局 `default_warn_days`
   - 计算有效级别：`severity` 非空用域名级，否则用全局 `default_severity`
   - 计算有效渠道：`channels` 非空数组用域名级，否则用全局 `default_channels`
   - 判断 `days_left <= threshold` → 触发告警
4. 指纹格式：`custom_monitor:{id}`
5. 调用现有 `_fire()` 方法

#### 自动恢复

在 `_auto_resolve()` 中新增：
- **仅 SSL 类型**适用自动恢复（`days_left` 由自动探测更新，证书续期后值会增大）
- 当 SSL 类型监控项的 `days_left > threshold` 连续 `RECOVER_ROUNDS` 轮 → `resolved(auto)`
- 使用内存滑动窗口记录历史判定，key 格式 `(env_id=0, host='cm:{id}', metric='custom_monitor')`
- **非 SSL 类型**（cloud_renewal/license/custom）不自动恢复：`days_left` 是手动填写的，不会自动变化，需要用户手动解决告警

#### SSL 探测失败处理

- 探测失败时 `error` 字段记录错误信息，`days_left` 保持上一次成功探测的值
- 如果从未成功探测过（`days_left IS NULL`），不触发告警
- 探测失败本身不产生告警（避免网络抖动导致误报）
- 前端列表显示「检测失败」状态图标和错误信息

#### 种子规则

`DEFAULT_RULES` 新增：

```python
dict(name="自定义监控到期", metric_type="custom_monitor", metric="expiry",
     compare="<", threshold=30, duration_seconds=0,
     scope={"type": "all", "ids": []}, severity="P1",
     channels=["feishu"], escalation_interval=30,
     remark="内置默认规则（到期监控）"),
```

### 规则验证扩展

`_validate_rule` 中新增对 `metric_type="custom_monitor"` 的支持：
- `metric` 固定为 `"expiry"`
- `compare` 固定为 `"<"`
- `threshold` 表示提前天数，必须为正整数

## API 路由

### 到期监控 CRUD（扩展 `alert_routes.py`）

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/alerts/monitors` | 到期监控列表 | 登录用户 |
| POST | `/alerts/monitors` | 添加监控项 | admin/operator |
| PUT | `/alerts/monitors/<id>` | 编辑监控项 | admin/operator |
| DELETE | `/alerts/monitors/<id>` | 删除监控项 | admin/operator |
| POST | `/alerts/monitors/<id>/toggle` | 启用/禁用 | admin/operator |
| POST | `/alerts/monitors/<id>/check` | 手动触发检测（仅 SSL） | admin/operator |
| GET | `/alerts/monitors/config` | 获取全局配置 | 登录用户 |
| PUT | `/alerts/monitors/config` | 更新全局配置 | admin |

### 创建/编辑请求体

```json
{
  "type": "ssl",                    // 必填: ssl / cloud_renewal / license / custom
  "name": "API 网关证书",            // 必填: 显示名称
  "target": "api.example.com",      // 必填: 域名/产品标识
  "port": 443,                      // SSL 类型端口，默认 443
  "expiry_at": null,                // 手动到期时间戳（非 SSL 类型必填）
  "days_left": null,                // 手动剩余天数（非 SSL 类型可选）
  "warn_days": 30,                  // 0 = 跟随全局
  "severity": "P1",                 // 空 = 跟随全局
  "channels": ["feishu", "email"],  // 空数组 = 跟随全局
  "detail": {}                      // 附加信息 JSON
}
```

### 验证规则

- `type` 必须为 `ssl` / `cloud_renewal` / `license` / `custom` 之一
- `name` 和 `target` 必填
- SSL 类型：`target` 为合法域名，`port` 为 1-65535
- 非 SSL 类型：`expiry_at` 或 `days_left` 至少提供一个
- `warn_days` 必须为非负整数
- `severity` 为空或 P0-P3
- `channels` 为合法渠道数组

## 前端设计

### 告警中心新增「到期监控」Tab

#### 页面结构

```
┌─────────────────────────────────────────────────────────────┐
│  告警列表 │ 规则管理 │ 静默窗口 │ 通知渠道 │ [到期监控]       │
├─────────────────────────────────────────────────────────────┤
│  [全局配置] 默认提前 30 天 │ 渠道: 飞书     [+ 添加监控]     │
├─────────────────────────────────────────────────────────────┤
│ 名称              │ 类型   │ 到期时间    │ 剩余  │ 状态 │ 操作│
│───────────────────┼────────┼────────────┼───────┼──────┼────│
│ API 网关证书       │ SSL    │ 2026-09-15 │ 25天  │ ⚠️   │ ⋯  │
│ 阿里云 ECS         │ 云续费  │ 2026-12-01 │ 102天 │ ✅   │ ⋯  │
│ 域名 example.com   │ 自定义  │ 2026-08-25 │  4天  │ 🔴   │ ⋯  │
│ JetBrains License │ 授权    │ 2027-01-01 │ 133天 │ ✅   │ ⋯  │
├─────────────────────────────────────────────────────────────┤
│ 颜色编码：🔴 ≤7天  ⚠️ ≤30天  ✅ >30天  ❌ 检测失败           │
└─────────────────────────────────────────────────────────────┘
```

#### 全局配置区域

- 显示当前全局默认提前天数（可点击编辑）
- 显示当前全局默认通知渠道
- 点击弹出编辑弹窗

#### 列表展示

- 每行显示：名称、类型标签、目标、到期时间、剩余天数（颜色编码）、状态图标
- 操作列：编辑、删除、启用/禁用、手动检测（仅 SSL）
- 支持按类型筛选
- 支持按状态筛选（正常/预警/危险/检测失败）

#### 添加/编辑抽屉

- 类型选择（下拉）：SSL 证书 / 云产品续费 / License 授权 / 自定义
- 名称（必填）
- 目标（必填）：SSL 类型为域名，其他类型为产品标识/描述
- 端口（仅 SSL 类型显示，默认 443）
- 到期时间（非 SSL 类型显示，日期选择器）
- 剩余天数（非 SSL 类型可选，手动填写）
- 提前通知天数（0 = 跟随全局，默认 0）
- 告警级别（空 = 跟随全局）
- 通知渠道（空 = 跟随全局）
- 附加信息（根据类型动态显示）

#### 与现有 Tab 的联动

- 到期监控触发的告警同时出现在「告警列表」Tab
- 可在规则管理 Tab 看到/编辑「自定义监控到期」种子规则
- 静默窗口和通知渠道配置对所有告警类型通用

## 文件变更清单

### 新增文件

| 文件 | 说明 |
|------|------|
| `webapp/ssl_probe.py` | SSL 证书探测核心函数 |
| `webapp/ssl_monitor.py` | SSL 自动探测守护线程 |
| `webapp/static/css/pages/monitors.css` | 到期监控页面样式 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `webapp/db.py` | 新增 `custom_monitors` 表定义；TABLES 白名单；JSON_FIELDS |
| `webapp/alert_engine.py` | 新增 `_evaluate_custom_monitors()`；扩展 `_auto_resolve()`；种子规则 |
| `webapp/routes/alert_routes.py` | 新增到期监控 CRUD API |
| `webapp/app.py` | 初始化 SSLMonitor |
| `webapp/static/js/pages/alerts.js` | 新增「到期监控」Tab 逻辑 |
| `webapp/templates/screen.html` | 新增 Tab 入口 |

## 测试计划

### 单元测试

- `tests/test_ssl_probe.py`：SSL 探测函数（mock ssl 模块）
- `tests/test_custom_monitors.py`：CRUD API 测试
- `tests/test_alert_engine.py`：扩展 `_evaluate_custom_monitors` 测试

### 集成测试

- SSL 探测 → 告警引擎 → 通知投递端到端
- 全局配置 → 域名级覆盖 → 告警触发阈值验证
- 自动恢复：证书续期后告警自动解决

### 前端验证

- 浏览器实测所有 Tab 交互（添加/编辑/删除/启用/禁用/手动检测）
- 颜色编码与状态图标正确显示
- 全局配置编辑生效
- 告警列表联动显示到期监控告警
