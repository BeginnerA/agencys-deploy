# 到期监控系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为平台新增通用到期监控功能，支持 SSL 域名证书自动探测、云产品续费/License/自定义截止日手动管理，全局默认 + 单项覆盖的规则配置，统一告警引擎集成。

**Architecture:** 新建 `ssl_probe.py`（纯 HTTPS 探测）+ `ssl_monitor.py`（守护线程）独立于 SSH 监控；`custom_monitors` 表存储监控项和到期信息；告警引擎新增 `_evaluate_custom_monitors()` 评估方法；前端在告警中心新增「到期监控」Tab。

**Tech Stack:** Python ssl 标准库、SQLite、Flask、vanilla JS（与现有前端一致）

---

## 文件结构

### 新增文件

| 文件 | 职责 |
|------|------|
| `webapp/ssl_probe.py` | SSL 证书探测核心函数 `probe_ssl(domain, port, timeout)` |
| `webapp/ssl_monitor.py` | SSL 自动探测守护线程 `SSLMonitor` |
| `webapp/static/css/pages/monitors.css` | 到期监控 Tab 页面样式 |
| `tests/test_ssl_probe.py` | SSL 探测函数单元测试 |
| `tests/test_ssl_monitor.py` | SSL 监控线程集成测试 |
| `tests/test_custom_monitors.py` | 到期监控 CRUD API + 告警引擎集成测试 |

### 修改文件

| 文件 | 变更 |
|------|------|
| `webapp/db.py` | SCHEMA 新增 `custom_monitors` 表；JSON_FIELDS 新增条目；TABLES 白名单新增 |
| `webapp/alert_engine.py` | DEFAULT_RULES 新增种子规则；`_load_snapshot()` 扩展；新增 `_evaluate_custom_monitors()`；`_auto_resolve()` 扩展 SSL 自动恢复 |
| `webapp/routes/alert_routes.py` | 新增到期监控 CRUD API（8 个路由） |
| `webapp/app.py` | 初始化 SSLMonitor 守护线程 |
| `webapp/static/js/pages/alerts.js` | 新增「到期监控」Tab 逻辑 |
| `webapp/static/css/pages/alerts.css` | 追加到期监控 Tab 样式 |
| `webapp/templates/screen.html` | 引入 monitors.css |

---

### Task 1: 数据库 Schema — custom_monitors 表

**Files:**
- Modify: `webapp/db.py` (SCHEMA, JSON_FIELDS, TABLES)
- Test: `tests/test_db_extensions.py`

- [ ] **Step 1: 编写失败测试**

在 `tests/test_db_extensions.py` 末尾追加：

```python
class TestCustomMonitorsTable(unittest.TestCase):
    """custom_monitors 表：建表 + CRUD + JSON 字段自动序列化"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_read(self):
        now = int(time.time())
        mid = dbm.create_record(self.db, "custom_monitors", dict(
            type="ssl", name="测试证书", target="api.example.com", port=443,
            warn_days=30, severity="P1", channels=["feishu"],
            enabled=1, detail={"cert_subject": "CN=api.example.com"},
            error="", created_at=now, updated_at=now))
        row = dbm.get_record(self.db, "custom_monitors", mid)
        self.assertIsNotNone(row)
        self.assertEqual(row["type"], "ssl")
        self.assertEqual(row["target"], "api.example.com")
        # JSON 字段自动反序列化
        self.assertIsInstance(row["channels"], list)
        self.assertEqual(row["channels"], ["feishu"])
        self.assertIsInstance(row["detail"], dict)
        self.assertEqual(row["detail"]["cert_subject"], "CN=api.example.com")

    def test_list_enabled(self):
        now = int(time.time())
        dbm.create_record(self.db, "custom_monitors", dict(
            type="ssl", name="A", target="a.com", port=443,
            enabled=1, days_left=10, created_at=now, updated_at=now))
        dbm.create_record(self.db, "custom_monitors", dict(
            type="ssl", name="B", target="b.com", port=443,
            enabled=0, days_left=5, created_at=now, updated_at=now))
        rows = dbm.list_records(self.db, "custom_monitors", "enabled=1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "A")

    def test_unique_constraint(self):
        now = int(time.time())
        dbm.create_record(self.db, "custom_monitors", dict(
            type="ssl", name="A", target="dup.com", port=443,
            enabled=1, created_at=now, updated_at=now))
        with self.assertRaises(Exception):
            dbm.create_record(self.db, "custom_monitors", dict(
                type="ssl", name="B", target="dup.com", port=443,
                enabled=1, created_at=now, updated_at=now))
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_db_extensions.py::TestCustomMonitorsTable -v`
Expected: FAIL — `ValueError: 非法表名: custom_monitors`

- [ ] **Step 3: 实现 custom_monitors 表**

在 `webapp/db.py` 的 SCHEMA 字符串末尾（`idx_ms_env_host_ts` 索引之后、`"""` 之前）追加：

```sql
CREATE TABLE IF NOT EXISTS custom_monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    target TEXT NOT NULL,
    port INTEGER DEFAULT 443,
    expiry_at INTEGER,
    days_left REAL,
    warn_days INTEGER DEFAULT 0,
    severity TEXT DEFAULT '',
    channels TEXT DEFAULT '[]',
    enabled INTEGER DEFAULT 1,
    last_checked INTEGER,
    detail TEXT DEFAULT '{}',
    error TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(type, target, port)
);
```

在 `JSON_FIELDS` 字典中新增：

```python
    "custom_monitors": {"channels", "detail"},
```

在 `TABLES` frozenset 中新增 `"custom_monitors"`：

```python
TABLES = frozenset(("projects", "services", "environments", "servers",
                    "env_servers", "pipelines", "users", "tasks", "audit_logs",
                    "alert_rules", "alerts", "alert_events",
                    "silence_windows", "platform_settings",
                    "log_entries", "log_scan_state",
                    "deploy_schedules", "release_windows",
                    "service_dependencies", "deployment_snapshots",
                    "canary_configs",
                    "response_time_history", "monitor_snapshots",
                    "custom_monitors"))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_db_extensions.py::TestCustomMonitorsTable -v`
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/db.py tests/test_db_extensions.py
git commit -m "feat: add custom_monitors table schema and CRUD support"
```

---

### Task 2: SSL 探测函数 — ssl_probe.py

**Files:**
- Create: `webapp/ssl_probe.py`
- Test: `tests/test_ssl_probe.py`

- [ ] **Step 1: 编写失败测试**

创建 `tests/test_ssl_probe.py`：

```python
"""SSL 证书探测函数测试（mock ssl 模块，不依赖真实网络）"""
import datetime
import unittest
from unittest.mock import patch, MagicMock

from webapp.ssl_probe import probe_ssl


class TestProbeSsl(unittest.TestCase):
    """probe_ssl 单元测试：mock ssl.wrap_socket 避免真实网络连接"""

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_successful_probe(self, mock_sock_cls, mock_ctx_fn):
        """成功探测：返回证书信息和剩余天数"""
        # 模拟证书 notAfter: "Sep 15 12:00:00 2026 GMT"
        future = datetime.datetime.utcnow() + datetime.timedelta(days=60)
        not_after = future.strftime("%b %d %H:%M:%S %Y GMT")
        # 构造 mock 证书
        mock_cert = {
            "subject": ((("commonName", "api.example.com"),),),
            "issuer": ((("commonName", "Let's Encrypt"),),),
            "notAfter": not_after,
        }
        mock_sock = MagicMock()
        mock_sock.getpeercert.return_value = mock_cert
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("api.example.com", 443, timeout=10)
        self.assertIsNone(result["error"])
        self.assertEqual(result["cert_subject"], "CN=api.example.com")
        self.assertEqual(result["cert_issuer"], "CN=Let's Encrypt")
        self.assertIsNotNone(result["cert_expiry"])
        self.assertAlmostEqual(result["days_left"], 60, delta=1)

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_connection_timeout(self, mock_sock_cls, mock_ctx_fn):
        """连接超时：返回 error 字段"""
        mock_sock = MagicMock()
        mock_sock.settimeout = MagicMock()
        mock_sock.connect.side_effect = TimeoutError("timed out")
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("bad.example.com", 443, timeout=5)
        self.assertIsNotNone(result["error"])
        self.assertIn("timed out", result["error"].lower() + result["error"])
        self.assertIsNone(result["cert_subject"])

    @patch("webapp.ssl_probe.ssl.create_default_context")
    @patch("webapp.ssl_probe.socket.socket")
    def test_dns_failure(self, mock_sock_cls, mock_ctx_fn):
        """DNS 解析失败：返回 error 字段"""
        mock_sock = MagicMock()
        mock_sock.settimeout = MagicMock()
        mock_sock.connect.side_effect = OSError("Name resolution failed")
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_ctx.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)
        mock_ctx_fn.return_value = mock_ctx

        result = probe_ssl("nonexistent.invalid", 443, timeout=5)
        self.assertIsNotNone(result["error"])
        self.assertIsNone(result["cert_subject"])
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_ssl_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.ssl_probe'`

- [ ] **Step 3: 实现 ssl_probe.py**

创建 `webapp/ssl_probe.py`：

```python
"""SSL 证书探测：纯 HTTPS 连接获取证书到期信息（不依赖 SSH）"""
import socket
import ssl
import datetime


def probe_ssl(domain: str, port: int = 443, timeout: int = 10) -> dict:
    """探测域名 SSL 证书，返回证书信息和剩余天数

    Returns:
        dict: {cert_subject, cert_issuer, cert_expiry, days_left, error}
        - 成功时 error=None
        - 失败时 error=错误信息, 其余字段=None
    """
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as s:
            s.settimeout(timeout)
            s.connect((domain, port))
            cert = s.getpeercert()
            # 解析到期时间: "Jun 15 12:00:00 2026 GMT"
            not_after = cert.get("notAfter", "")
            expiry_dt = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
            days_left = (expiry_dt - datetime.datetime.utcnow()).days
            # 提取 CN
            subject = _extract_cn(cert.get("subject", ()))
            issuer = _extract_cn(cert.get("issuer", ()))
            return {
                "cert_subject": f"CN={subject}" if subject else "",
                "cert_issuer": f"CN={issuer}" if issuer else "",
                "cert_expiry": int(expiry_dt.timestamp()),
                "days_left": days_left,
                "error": None,
            }
    except Exception as e:
        return {
            "cert_subject": None,
            "cert_issuer": None,
            "cert_expiry": None,
            "days_left": None,
            "error": str(e),
        }


def _extract_cn(rdn_sequence) -> str:
    """从 X.509 RDN 序列中提取 commonName"""
    for rdn in rdn_sequence:
        for attr_type, attr_value in rdn:
            if attr_type == "commonName":
                return attr_value
    return ""
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_ssl_probe.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/ssl_probe.py tests/test_ssl_probe.py
git commit -m "feat: add SSL certificate probe function (ssl_probe.py)"
```

---

### Task 3: SSL 监控守护线程 — ssl_monitor.py

**Files:**
- Create: `webapp/ssl_monitor.py`
- Test: `tests/test_ssl_monitor.py`

- [ ] **Step 1: 编写失败测试**

创建 `tests/test_ssl_monitor.py`：

```python
"""SSL 监控线程测试：验证探测循环和数据库更新"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from webapp import db as dbm
from webapp.ssl_monitor import SSLMonitor


class TestSSLMonitor(unittest.TestCase):
    """SSLMonitor 守护线程：单轮探测更新 custom_monitors 记录"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.now = int(time.time())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_ssl_domain(self, target, port=443, **kw):
        fields = dict(type="ssl", name=f"test-{target}", target=target, port=port,
                      enabled=1, created_at=self.now, updated_at=self.now)
        fields.update(kw)
        return dbm.create_record(self.db, "custom_monitors", fields)

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_probe_updates_record(self, mock_probe):
        """探测成功：更新 last_checked, days_left, detail"""
        mock_probe.return_value = {
            "cert_subject": "CN=api.example.com",
            "cert_issuer": "CN=Let's Encrypt",
            "cert_expiry": self.now + 86400 * 60,
            "days_left": 60,
            "error": None,
        }
        mid = self._add_ssl_domain("api.example.com")
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        row = dbm.get_record(self.db, "custom_monitors", mid)
        self.assertEqual(row["days_left"], 60)
        self.assertIsNotNone(row["last_checked"])
        detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
        self.assertEqual(detail["cert_subject"], "CN=api.example.com")
        self.assertEqual(row["error"], "")

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_probe_failure_preserves_old_data(self, mock_probe):
        """探测失败：保留旧 days_left，记录 error"""
        mock_probe.return_value = {
            "cert_subject": None, "cert_issuer": None,
            "cert_expiry": None, "days_left": None,
            "error": "Connection timed out",
        }
        mid = self._add_ssl_domain("bad.example.com",
                                   days_left=30, last_checked=self.now - 3600)
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        row = dbm.get_record(self.db, "custom_monitors", mid)
        # days_left 保留旧值
        self.assertEqual(row["days_left"], 30)
        # error 记录本次失败
        self.assertEqual(row["error"], "Connection timed out")

    @patch("webapp.ssl_monitor.probe_ssl")
    def test_skips_disabled(self, mock_probe):
        """禁用的监控项不探测"""
        self._add_ssl_domain("disabled.example.com", enabled=0)
        mon = SSLMonitor(self.db, interval=3600)
        mon._run_once()
        mock_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_ssl_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.ssl_monitor'`

- [ ] **Step 3: 实现 ssl_monitor.py**

创建 `webapp/ssl_monitor.py`：

```python
"""SSL 自动探测守护线程：周期检测 custom_monitors 中 type=ssl 的域名证书"""
import json
import sys
import threading
import time

from . import db as dbm
from .ssl_probe import probe_ssl


class SSLMonitor:
    """SSL 证书自动探测守护线程

    每 interval 秒遍历 custom_monitors 表中 type=ssl 且 enabled=1 的记录，
    调用 probe_ssl() 更新证书到期信息。独立数据库连接（与 monitor 相同模式）。
    """

    def __init__(self, db_path: str, interval: int = 3600):
        self._db_path = db_path
        self._interval = max(60, int(interval))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="ssl-monitor")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:
                print(f"[ssl-monitor] 探测周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _run_once(self):
        """一轮探测：遍历所有启用的 SSL 监控项"""
        rows = dbm.list_records(self._db_path, "custom_monitors",
                                "type='ssl' AND enabled=1")
        for row in rows:
            try:
                self._probe_one(row)
            except Exception as e:
                print(f"[ssl-monitor] 探测 {row['target']} 异常: {e!r}",
                      file=sys.stderr)

    def _probe_one(self, row: dict):
        """探测单个域名并更新记录"""
        domain = row["target"]
        port = row.get("port") or 443
        result = probe_ssl(domain, port, timeout=10)
        now = int(time.time())
        if result["error"]:
            # 探测失败：保留旧 days_left，记录 error
            dbm.update_record(self._db_path, "custom_monitors", row["id"], dict(
                last_checked=now,
                error=result["error"],
                updated_at=now))
        else:
            # 探测成功：更新所有字段
            detail = json.dumps({
                "cert_subject": result["cert_subject"],
                "cert_issuer": result["cert_issuer"],
            }, ensure_ascii=False)
            dbm.update_record(self._db_path, "custom_monitors", row["id"], dict(
                last_checked=now,
                expiry_at=result["cert_expiry"],
                days_left=result["days_left"],
                detail=detail,
                error="",
                updated_at=now))

    def probe_one_now(self, monitor_id: int) -> dict:
        """手动触发单个监控项的探测（供 API 调用）"""
        row = dbm.get_record(self._db_path, "custom_monitors", monitor_id)
        if not row or row.get("type") != "ssl":
            return {"error": "监控项不存在或非 SSL 类型"}
        self._probe_one(row)
        return dbm.get_record(self._db_path, "custom_monitors", monitor_id)


_ssl_monitor = None


def get_ssl_monitor() -> "SSLMonitor":
    global _ssl_monitor
    return _ssl_monitor


def init_ssl_monitor(app) -> None:
    """启动 SSL 监控线程"""
    global _ssl_monitor
    if _ssl_monitor is None:
        interval = app.config.get("SSL_CHECK_INTERVAL", 3600)
        _ssl_monitor = SSLMonitor(app.config["DB_PATH"], interval=interval)
    _ssl_monitor.start()
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_ssl_monitor.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/ssl_monitor.py tests/test_ssl_monitor.py
git commit -m "feat: add SSL monitor daemon thread (ssl_monitor.py)"
```

---

### Task 4: 告警引擎集成 — _evaluate_custom_monitors

**Files:**
- Modify: `webapp/alert_engine.py`
- Test: `tests/test_alert_engine.py` (追加测试)

- [ ] **Step 1: 编写失败测试**

在 `tests/test_alert_engine.py` 末尾追加：

```python
class TestCustomMonitorAlerts(unittest.TestCase):
    """到期监控告警：全局默认 + 域名级覆盖 + 自动恢复"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def _engine(self, interval=30):
        return AlertEngine(self.db, interval=interval, monitor=None)

    def _add_monitor(self, days_left=5, warn_days=0, severity="",
                     channels="[]", mtype="ssl"):
        now = int(time.time())
        return dbm.create_record(self.db, "custom_monitors", dict(
            type=mtype, name=f"test-{days_left}d", target="test.com",
            port=443, days_left=days_left, warn_days=warn_days,
            severity=severity, channels=channels,
            enabled=1, created_at=now, updated_at=now))

    def _set_global_config(self, warn_days=30, severity="P1",
                           channels=None):
        import json
        cfg = {"default_warn_days": warn_days, "default_severity": severity,
               "default_channels": channels or ["feishu"],
               "ssl_check_interval": 3600}
        now = int(time.time())
        dbm.create_record(self.db, "platform_settings", dict(
            key="custom_monitor_config", value=json.dumps(cfg), updated_at=now))

    def test_fires_with_global_default(self):
        """全局默认 30 天，监控项 days_left=5 → 触发告警"""
        self._set_global_config(warn_days=30, severity="P1")
        mid = self._add_monitor(days_left=5)
        e = self._engine()
        e._seed_rules()
        snap = e._load_snapshot()
        e._evaluate_custom_monitors(snap)
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("test.com", alerts[0]["subject"])

    def test_domain_override_threshold(self):
        """域名级 warn_days=3 覆盖全局 30 天，days_left=5 → 不触发"""
        self._set_global_config(warn_days=30)
        self._add_monitor(days_left=5, warn_days=3)
        e = self._engine()
        e._seed_rules()
        snap = e._load_snapshot()
        e._evaluate_custom_monitors(snap)
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 0)

    def test_no_alert_when_days_left_null(self):
        """days_left 为 NULL（从未成功探测）→ 不触发告警"""
        self._set_global_config(warn_days=30)
        now = int(time.time())
        dbm.create_record(self.db, "custom_monitors", dict(
            type="ssl", name="never-checked", target="new.com",
            port=443, days_left=None, warn_days=0, severity="",
            channels="[]", enabled=1, created_at=now, updated_at=now))
        e = self._engine()
        e._seed_rules()
        snap = e._load_snapshot()
        e._evaluate_custom_monitors(snap)
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 0)

    def test_seed_rule_exists(self):
        """种子规则包含「自定义监控到期」"""
        e = self._engine()
        e._seed_rules()
        rules = dbm.list_records(self.db, "alert_rules")
        names = {r["name"] for r in rules}
        self.assertIn("自定义监控到期", names)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_alert_engine.py::TestCustomMonitorAlerts -v`
Expected: FAIL — `_evaluate_custom_monitors` 方法不存在

- [ ] **Step 3: 实现告警引擎集成**

在 `webapp/alert_engine.py` 的 `DEFAULT_RULES` 列表末尾追加种子规则：

```python
    dict(name="自定义监控到期", metric_type="custom_monitor", metric="expiry",
         compare="<", threshold=30, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P1",
         channels=["feishu"], escalation_interval=30,
         remark="内置默认规则（到期监控）"),
```

在 `_load_snapshot()` 方法的返回字典中新增两个键：

```python
    def _load_snapshot(self) -> dict:
        rules = dbm.list_records(self._db_path, "alert_rules")
        return {
            "rules": rules,
            "metric_rules": [r for r in rules
                             if r.get("enabled") and r.get("metric_type") == "metric"],
            "status_rules": [r for r in rules
                             if r.get("enabled") and r.get("metric_type") == "status"],
            "task_fail_rule": next((r for r in rules
                                    if r.get("enabled") and r.get("metric") == "task_failed"),
                                   None),
            "projects": dbm.list_records(self._db_path, "projects"),
            "envs": dbm.list_records(self._db_path, "environments"),
            "servers": dbm.list_records(self._db_path, "servers"),
            "services": dbm.list_records(self._db_path, "services", "enabled=1"),
            "windows": dbm.list_records(self._db_path, "silence_windows"),
            "custom_monitors": dbm.list_records(
                self._db_path, "custom_monitors",
                "enabled=1 AND days_left IS NOT NULL"),
            "custom_monitor_config": self._load_custom_monitor_config(),
        }
```

在 `AlertEngine` 类中新增两个方法（放在 `_evaluate_status_rules` 之后、`_check_tasks` 之前）：

```python
    def _load_custom_monitor_config(self) -> dict:
        """加载全局到期监控配置"""
        import json
        rows = dbm.list_records(self._db_path, "platform_settings",
                                "key=?", ("custom_monitor_config",))
        if rows and rows[0].get("value"):
            try:
                return json.loads(rows[0]["value"])
            except (ValueError, TypeError):
                pass
        return {"default_warn_days": 30, "default_severity": "P1",
                "default_channels": ["feishu"], "ssl_check_interval": 3600}

    def _evaluate_custom_monitors(self, snap=None):
        """评估到期监控项：days_left <= 阈值 → 触发告警"""
        if snap is None:
            snap = self._load_snapshot()
        monitors = snap.get("custom_monitors") or []
        if not monitors:
            return
        cfg = snap.get("custom_monitor_config") or {}
        default_warn = cfg.get("default_warn_days", 30)
        default_sev = cfg.get("default_severity", "P1")
        default_channels = cfg.get("default_channels", ["feishu"])
        # 查找 custom_monitor 类型的种子规则
        cm_rule = None
        for r in snap.get("rules", []):
            if r.get("metric_type") == "custom_monitor" and r.get("enabled"):
                cm_rule = r
                break
        if not cm_rule:
            return
        for m in monitors:
            days_left = m.get("days_left")
            if days_left is None:
                continue
            # 计算有效配置：域名级覆盖 > 全局默认
            warn_days = m.get("warn_days") or 0
            threshold = warn_days if warn_days > 0 else default_warn
            severity = m.get("severity") or default_sev
            channels_raw = m.get("channels") or []
            channels = channels_raw if channels_raw else default_channels
            # 判断是否触发
            if days_left <= threshold:
                subject = f"到期监控: {m['name']} ({m['target']})"
                desc = f"剩余 {days_left} 天，阈值 {threshold} 天"
                target_info = {"monitor_id": m["id"], "type": m["type"],
                               "target": m["target"], "name": m["name"]}
                # 构造一个临时 rule 用于 _fire 调用
                fire_rule = dict(cm_rule)
                fire_rule["severity"] = severity
                fire_rule["channels"] = channels
                self._fire(fire_rule, f"custom_monitor:{m['id']}",
                           subject=subject, description=desc,
                           target_info=target_info,
                           windows=snap.get("windows"))
```

在 `_cycle()` 方法中新增调用（在 `_evaluate_status_rules(snap)` 之后）：

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

- [ ] **Step 4: 扩展 _auto_resolve 支持 SSL 自动恢复**

在 `webapp/alert_engine.py` 的 `_auto_resolve()` 方法中新增 SSL 类型自动恢复逻辑：

```python
    def _auto_resolve(self, snap=None):
        """指标类告警：连续 RECOVER_ROUNDS 轮恢复 → resolved(auto)；
        服务下线告警：服务重新在线 → resolved(auto)；任务失败不自动恢复；
        SSL 到期监控：证书续期后 days_left > threshold → resolved(auto)"""
        if snap is None:
            snap = self._load_snapshot()
        rules_by_id = {r["id"]: r for r in snap["rules"]}
        for a in dbm.list_records(self._db_path, "alerts",
                                  "status IN ('firing','acked')", (), "id"):
            rule = rules_by_id.get(a["rule_id"]) or {}
            if rule.get("metric_type") == "metric":
                if self._metric_recovered(rule, a):
                    self._resolve(a, "auto")
            elif rule.get("metric") == "service_down":
                if self._service_back(rule, a):
                    self._resolve(a, "auto")
            elif rule.get("metric_type") == "custom_monitor":
                if self._custom_monitor_recovered(a):
                    self._resolve(a, "auto")
```

新增 `_custom_monitor_recovered()` 方法：

```python
    def _custom_monitor_recovered(self, alert: dict) -> bool:
        """SSL 类型到期监控自动恢复：days_left > threshold 连续 RECOVER_ROUNDS 轮"""
        # 仅 SSL 类型适用自动恢复（非 SSL 类型 days_left 手动填写，不会自动变化）
        ti = alert.get("target_info") or {}
        if ti.get("type") != "ssl":
            return False
        monitor_id = ti.get("monitor_id")
        if not monitor_id:
            return False
        m = dbm.get_record(self._db_path, "custom_monitors", monitor_id)
        if not m or m.get("days_left") is None:
            return False
        # 使用滑动窗口判定
        key = (0, f"cm:{monitor_id}", "custom_monitor")
        self._push_history(key, m["days_left"])
        dq = self._history.get(key)
        if not dq:
            return False
        # 取该监控项的有效阈值
        cfg = self._load_custom_monitor_config()
        warn = m.get("warn_days") or 0
        threshold = warn if warn > 0 else cfg.get("default_warn_days", 30)
        n = 0
        for ts, value in reversed(dq):
            if value <= threshold:
                return False  # 仍超阈值
            n += 1
            if n >= RECOVER_ROUNDS:
                return True
        return False
```

- [ ] **Step 5: 运行测试验证通过**

Run: `pytest tests/test_alert_engine.py::TestCustomMonitorAlerts -v`
Expected: 4 tests PASS

- [ ] **Step 6: 提交**

```bash
git add webapp/alert_engine.py tests/test_alert_engine.py
git commit -m "feat: integrate custom monitors into alert engine"
```

---

### Task 5: API 路由 — 到期监控 CRUD

**Files:**
- Modify: `webapp/routes/alert_routes.py`
- Test: `tests/test_custom_monitors.py`

- [ ] **Step 1: 编写失败测试**

创建 `tests/test_custom_monitors.py`：

```python
"""到期监控 CRUD API + 全局配置测试"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webapp as webapp_pkg

_TMP = tempfile.mkdtemp(prefix="custom_monitors_test_")
webapp_pkg.DATA_DIR = Path(_TMP)

from webapp import create_app


class TestCustomMonitorRoutes(unittest.TestCase):
    """到期监控 API 冒烟测试"""

    @classmethod
    def setUpClass(cls):
        webapp_pkg.DATA_DIR = Path(_TMP)
        cls.app = create_app()
        cls.c = cls.app.test_client()
        token = cls._login(cls.c, "admin", "admin")
        r = cls.c.post("/api/auth/password", json={
            "old_password": "admin", "new_password": "test1234"},
            headers=cls._h(token))
        assert r.status_code == 200
        cls.token = cls._login(cls.c, "admin", "test1234")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    @staticmethod
    def _login(c, user, pwd):
        r = c.post("/api/auth/login", json={"username": user, "password": pwd})
        return r.get_json()["token"]

    @staticmethod
    def _h(token):
        return {"Authorization": f"Bearer {token}"}

    def _post(self, body):
        return self.c.post("/api/alerts/monitors", json=body,
                           headers=self._h(self.token))

    def test_create_ssl_monitor(self):
        r = self._post({"type": "ssl", "name": "测试证书", "target": "api.example.com"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertGreater(r.get_json()["id"], 0)

    def test_create_cloud_renewal(self):
        import time
        r = self._post({
            "type": "cloud_renewal", "name": "阿里云 ECS",
            "target": "ecs-bp1xxx",
            "expiry_at": int(time.time()) + 86400 * 90,
            "days_left": 90,
            "detail": {"provider": "阿里云", "product": "ECS"}
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_create_invalid_type(self):
        r = self._post({"type": "invalid", "name": "X", "target": "x"})
        self.assertEqual(r.status_code, 400)

    def test_create_ssl_missing_target(self):
        r = self._post({"type": "ssl", "name": "X"})
        self.assertEqual(r.status_code, 400)

    def test_list_monitors(self):
        r = self.c.get("/api/alerts/monitors", headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIsInstance(data, list)

    def test_toggle_monitor(self):
        r = self._post({"type": "ssl", "name": "Toggle", "target": "toggle.com"})
        mid = r.get_json()["id"]
        r2 = self.c.post(f"/api/alerts/monitors/{mid}/toggle",
                         headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)
        self.assertIn(r2.get_json()["enabled"], [0, 1])

    def test_delete_monitor(self):
        r = self._post({"type": "ssl", "name": "Del", "target": "del.com"})
        mid = r.get_json()["id"]
        r2 = self.c.delete(f"/api/alerts/monitors/{mid}",
                           headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)

    def test_global_config_get_put(self):
        # 获取默认配置
        r = self.c.get("/api/alerts/monitors/config",
                       headers=self._h(self.token))
        self.assertEqual(r.status_code, 200)
        # 更新配置
        r2 = self.c.put("/api/alerts/monitors/config",
                        json={"default_warn_days": 15, "default_severity": "P2",
                              "default_channels": ["feishu", "email"]},
                        headers=self._h(self.token))
        self.assertEqual(r2.status_code, 200)
        # 验证更新生效
        r3 = self.c.get("/api/alerts/monitors/config",
                        headers=self._h(self.token))
        cfg = r3.get_json()
        self.assertEqual(cfg["default_warn_days"], 15)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试验证失败**

Run: `pytest tests/test_custom_monitors.py -v`
Expected: FAIL — 404 (路由不存在)

- [ ] **Step 3: 扩展 _validate_rule 支持 custom_monitor 类型**

在 `webapp/routes/alert_routes.py` 的 `_validate_rule()` 函数中，`metric_type` 校验部分新增 `"custom_monitor"`：

```python
    if has("metric_type") and body.get("metric_type") not in (
            "metric", "status", "log_keyword", "custom_monitor"):
        return "metric_type 非法"
```

- [ ] **Step 4: 实现 API 路由**

在 `webapp/routes/alert_routes.py` 末尾追加：

```python
# ---------- 到期监控 CRUD ----------

MONITOR_TYPES = ("ssl", "cloud_renewal", "license", "custom")


@alert_bp.route("/alerts/monitors")
@require_auth
def list_monitors():
    """到期监控列表"""
    db = current_app.config["DB_PATH"]
    rows = dbm.list_records(db, "custom_monitors", "", (), "id")
    return jsonify(rows)


@alert_bp.route("/alerts/monitors", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_monitor():
    """添加监控项"""
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    err = _validate_monitor(body)
    if err:
        return jsonify({"error": err}), 400
    now = int(time.time())
    mid = dbm.create_record(db, "custom_monitors", dict(
        type=body["type"], name=body["name"].strip(),
        target=body["target"].strip(),
        port=body.get("port", 443),
        expiry_at=body.get("expiry_at"),
        days_left=body.get("days_left"),
        warn_days=body.get("warn_days", 0),
        severity=body.get("severity", ""),
        channels=body.get("channels", []),
        enabled=1,
        detail=body.get("detail", {}),
        error="",
        created_at=now, updated_at=now))
    audit("create_monitor", "custom_monitor", mid, {"name": body["name"]})
    return jsonify({"id": mid})


@alert_bp.route("/alerts/monitors/<int:monitor_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_monitor(monitor_id):
    """编辑监控项"""
    db = current_app.config["DB_PATH"]
    old = dbm.get_record(db, "custom_monitors", monitor_id)
    if not old:
        return jsonify({"error": "监控项不存在"}), 404
    body = request.get_json(silent=True) or {}
    err = _validate_monitor(body, partial=True)
    if err:
        return jsonify({"error": err}), 400
    dbm.update_record(db, "custom_monitors", monitor_id, dict(
        type=body.get("type", old["type"]),
        name=(body.get("name") or old["name"]).strip(),
        target=(body.get("target") or old["target"]).strip(),
        port=body.get("port", old["port"]),
        expiry_at=body.get("expiry_at", old["expiry_at"]),
        days_left=body.get("days_left", old["days_left"]),
        warn_days=body.get("warn_days", old["warn_days"]),
        severity=body.get("severity", old.get("severity", "")),
        channels=body.get("channels", old.get("channels", [])),
        detail=body.get("detail", old.get("detail", {})),
        updated_at=int(time.time())))
    audit("update_monitor", "custom_monitor", monitor_id,
          {"name": body.get("name", old["name"])})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/monitors/<int:monitor_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_monitor(monitor_id):
    """删除监控项"""
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "custom_monitors", monitor_id):
        return jsonify({"error": "监控项不存在"}), 404
    dbm.delete_record(db, "custom_monitors", monitor_id)
    audit("delete_monitor", "custom_monitor", monitor_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/monitors/<int:monitor_id>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_monitor(monitor_id):
    """启用/禁用监控项"""
    db = current_app.config["DB_PATH"]
    m = dbm.get_record(db, "custom_monitors", monitor_id)
    if not m:
        return jsonify({"error": "监控项不存在"}), 404
    new_enabled = 0 if m["enabled"] else 1
    dbm.update_record(db, "custom_monitors", monitor_id,
                      {"enabled": new_enabled})
    audit("toggle_monitor", "custom_monitor", monitor_id,
          {"enabled": new_enabled})
    return jsonify({"ok": True, "enabled": new_enabled})


@alert_bp.route("/alerts/monitors/<int:monitor_id>/check", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def check_monitor(monitor_id):
    """手动触发 SSL 探测"""
    from ..ssl_monitor import get_ssl_monitor
    mon = get_ssl_monitor()
    if not mon:
        return jsonify({"error": "SSL 监控未启动"}), 503
    result = mon.probe_one_now(monitor_id)
    if "error" in result and len(result) == 1:
        return jsonify(result), 400
    return jsonify(result)


@alert_bp.route("/alerts/monitors/config")
@require_auth
def get_monitor_config():
    """获取全局到期监控配置"""
    db = current_app.config["DB_PATH"]
    rows = dbm.list_records(db, "platform_settings",
                            "key=?", ("custom_monitor_config",))
    if rows and rows[0]["value"]:
        try:
            return jsonify(json.loads(rows[0]["value"]))
        except ValueError:
            pass
    return jsonify({"default_warn_days": 30, "default_severity": "P1",
                    "default_channels": ["feishu"], "ssl_check_interval": 3600})


@alert_bp.route("/alerts/monitors/config", methods=["PUT"])
@require_auth
@require_role("admin")
def put_monitor_config():
    """更新全局到期监控配置"""
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    cfg = {
        "default_warn_days": int(body.get("default_warn_days", 30)),
        "default_severity": body.get("default_severity", "P1"),
        "default_channels": body.get("default_channels", ["feishu"]),
        "ssl_check_interval": int(body.get("ssl_check_interval", 3600)),
    }
    if cfg["default_severity"] not in SEVERITIES:
        return jsonify({"error": "severity 非法"}), 400
    now = int(time.time())
    value = json.dumps(cfg, ensure_ascii=False)
    rows = dbm.list_records(db, "platform_settings",
                            "key=?", ("custom_monitor_config",))
    if rows:
        dbm.update_record(db, "platform_settings", rows[0]["id"],
                          {"value": value, "updated_at": now})
    else:
        dbm.create_record(db, "platform_settings",
                          {"key": "custom_monitor_config", "value": value,
                           "updated_at": now})
    audit("update_monitor_config", "config", 0, {"config": cfg})
    return jsonify({"ok": True})


def _validate_monitor(body, partial=False) -> str:
    """监控项校验；返回错误信息，空串=通过"""
    if not partial:
        if body.get("type") not in MONITOR_TYPES:
            return "type 非法"
        if not (body.get("name") or "").strip():
            return "名称必填"
        if not (body.get("target") or "").strip():
            return "目标必填"
    if "type" in body and body["type"] not in MONITOR_TYPES:
        return "type 非法"
    if "port" in body:
        try:
            p = int(body["port"])
            if p < 1 or p > 65535:
                return "port 需在 1-65535 之间"
        except (TypeError, ValueError):
            return "port 必须为数字"
    if "warn_days" in body:
        try:
            wd = int(body["warn_days"])
            if wd < 0:
                return "warn_days 需为非负整数"
        except (TypeError, ValueError):
            return "warn_days 必须为数字"
    if "severity" in body and body["severity"] and body["severity"] not in SEVERITIES:
        return "severity 非法"
    if "channels" in body:
        ch = body.get("channels") or []
        if not isinstance(ch, list):
            return "channels 需为列表"
        if ch and not set(ch) <= set(CHANNELS):
            return "channels 包含非法渠道"
    # 非 SSL 类型需要到期时间
    if not partial and body.get("type") != "ssl":
        if not body.get("expiry_at") and body.get("days_left") is None:
            return "非 SSL 类型需提供 expiry_at 或 days_left"
    return ""
```

- [ ] **Step 4: 运行测试验证通过**

Run: `pytest tests/test_custom_monitors.py -v`
Expected: 8 tests PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/routes/alert_routes.py tests/test_custom_monitors.py
git commit -m "feat: add custom monitor CRUD API routes"
```

---

### Task 6: 应用初始化 — 启动 SSLMonitor

**Files:**
- Modify: `webapp/app.py`

- [ ] **Step 1: 在 app.py 中添加 SSLMonitor 初始化**

在 `webapp/app.py` 的 `main()` 函数中，在 `init_engine(app)` 之后添加：

```python
        from webapp.ssl_monitor import init_ssl_monitor
        init_ssl_monitor(app)
        print("[ssl-monitor] SSL 证书探测已启动")
```

完整上下文（在 `init_engine(app)` 和 `print(f"[alert]...")` 之后）：

```python
        from webapp.alert_engine import init_engine
        init_engine(app)
        print(f"[alert] 告警引擎已启动，评估周期 {args.monitor_interval}s")
        from webapp.ssl_monitor import init_ssl_monitor
        init_ssl_monitor(app)
        print("[ssl-monitor] SSL 证书探测已启动")
        from webapp.log_collector import init_collector
        init_collector(app)
```

- [ ] **Step 2: 验证应用启动无异常**

Run: `python -c "from webapp import create_app; app = create_app(); print('OK')"`
Expected: `OK`

- [ ] **Step 3: 提交**

```bash
git add webapp/app.py
git commit -m "feat: initialize SSL monitor on app startup"
```

---

### Task 7: 前端 — 到期监控 Tab

**Files:**
- Modify: `webapp/static/js/pages/alerts.js`
- Modify: `webapp/static/css/pages/alerts.css`
- Modify: `webapp/templates/screen.html`

- [ ] **Step 1: 在 alerts.js 中添加到期监控 Tab**

在 `alerts.js` 中修改 `currentTab` 默认值注释和 `renderPage` 函数，添加 `monitors` Tab：

修改 `currentTab` 注释行：
```javascript
let currentTab = 'alerts';  // Tab 切换：alerts | rules | silences | stats | monitors
```

修改 `renderPage` 函数中的 Tab 栏 HTML：
```javascript
  function renderPage(){
    const root = document.getElementById('pageRoot');
    root.innerHTML = `
      <div class="alert-tabs-bar">
        <button class="tab-btn ${currentTab==='alerts'?'active':''}" data-tab="alerts">🔔 告警列表</button>
        <button class="tab-btn ${currentTab==='rules'?'active':''}" data-tab="rules">📏 规则管理</button>
        <button class="tab-btn ${currentTab==='silences'?'active':''}" data-tab="silences">🔕 静默窗口</button>
        <button class="tab-btn ${currentTab==='stats'?'active':''}" data-tab="stats">📊 历史统计</button>
        <button class="tab-btn ${currentTab==='monitors'?'active':''}" data-tab="monitors">📜 到期监控</button>
      </div>
      <div id="tabContent"></div>`;
    root.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
      btn.onclick = () => switchTab(btn.dataset.tab);
    });
    if (currentTab === 'alerts') renderAlertsTab();
    else if (currentTab === 'rules') renderRulesTab();
    else if (currentTab === 'silences') renderSilencesTab();
    else if (currentTab === 'stats') renderStatsTab();
    else if (currentTab === 'monitors') renderMonitorsTab();
  }
```

修改 `switchTab` 函数：
```javascript
  function switchTab(tab){
    if (currentTab === tab) return;
    currentTab = tab;
    renderPage();
    if (tab === 'alerts') { loadAlertCounts(); loadAlerts(); }
    else if (tab === 'rules') { loadRules(); }
    else if (tab === 'silences') { loadSilences(); }
    else if (tab === 'stats') { loadStats(); }
    else if (tab === 'monitors') { loadMonitors(); }
  }
```

在 `alerts.js` 的 `// ---- 模块导出 ----` 之前添加到期监控 Tab 的完整逻辑（约 300 行 JS 代码，包含状态管理、渲染、CRUD 操作）。

- [ ] **Step 2: 在 alerts.css 中追加到期监控样式**

在 `webapp/static/css/pages/alerts.css` 末尾追加到期监控 Tab 的样式。

- [ ] **Step 3: 在 screen.html 中引入 monitors.css（如需要）**

检查 screen.html 是否已引入 alerts.css，到期监控样式追加在 alerts.css 中则无需额外引入。

- [ ] **Step 4: 浏览器验证**

启动应用后在浏览器中验证：
1. 告警中心出现「到期监控」Tab
2. 点击 Tab 显示监控列表（空列表或已有数据）
3. 点击「添加监控」按钮弹出表单
4. 填写 SSL 类型域名并提交
5. 列表显示新增项
6. 全局配置编辑功能正常

- [ ] **Step 5: 提交**

```bash
git add webapp/static/js/pages/alerts.js webapp/static/css/pages/alerts.css
git commit -m "feat: add custom monitor tab in alert center frontend"
```

---

### Task 8: 运行全部测试

**Files:**
- 无新文件

- [ ] **Step 1: 运行完整测试套件**

Run: `pytest tests/ -v`
Expected: 所有测试通过（包括新增的 test_ssl_probe, test_ssl_monitor, test_custom_monitors, test_alert_engine 扩展）

- [ ] **Step 2: 修复任何失败**

如有测试失败，逐个修复。

- [ ] **Step 3: 最终提交**

```bash
git add -A
git commit -m "feat: custom monitor system - all tests passing"
```
