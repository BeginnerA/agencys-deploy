# 智能告警引擎实施计划（阶段一：告警引擎）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 Flask 单进程内实现告警规则引擎（指标阈值/服务状态/任务失败规则）、P0-P3 分级、firing→acked→resolved 状态机、升级/收敛、静默窗口、6 种通知渠道，并将现有前端计算型告警页迁移为消费后端 API。

**Architecture:** AlertEvaluator 守护线程每 30s 读 monitor 探测缓存评估规则；滑动窗口环形缓冲判定"持续满足阈值 ≥ duration_seconds"；收敛后的活跃告警存 alerts 表（fingerprint 部分唯一索引，同指纹只允许一条非 resolved）；通知经 alert_channels 分发并记录 alert_events；静默窗口在首次触发与每次通知前匹配；任务失败由引擎每轮扫描 tasks 表检测。

**Tech Stack:** Python 3 标准库（threading/sqlite3/smtplib/time/json）+ requests + Flask + 原生 HTML/CSS/JS；测试用标准库 unittest（零新依赖）。

**相关规格:** docs/superpowers/specs/2026-08-18-alert-log-center-design.md（§4 数据模型、§5 告警引擎核心逻辑、§8 alerts 页）

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `webapp/db.py` | 修改 | SCHEMA 追加 5 张表、TABLES 白名单、JSON_FIELDS、services 加列迁移 |
| `webapp/alert_channels.py` | 新建 | 6 种通知渠道（邮件/企微/Webhook/阿里云短信/飞书/钉钉）+ 重试 |
| `webapp/alert_engine.py` | 新建 | 规则评估/状态机/升级收敛/静默/任务失败检测/种子规则/调度线程 |
| `webapp/routes/alert_routes.py` | 新建 | 告警/规则/静默/渠道 API |
| `webapp/__init__.py` | 修改 | 注册 alert_bp |
| `webapp/app.py` | 修改 | 启动 AlertEngine 线程 |
| `webapp/static/js/pages/alerts.js` | 重写 | 消费后端 API 的告警中心页 |
| `tests/__init__.py` | 新建 | 测试包 |
| `tests/test_db_extensions.py` | 新建 | DB 扩展测试 |
| `tests/test_alert_channels.py` | 新建 | 渠道测试（mock 网络） |
| `tests/test_alert_engine.py` | 新建 | 引擎核心测试 |

---

### Task 1: db.py 扩展（5 张新表 + services 加列）

**Files:**
- Modify: `webapp/db.py`
- Create: `tests/__init__.py`
- Create: `tests/test_db_extensions.py`

- [ ] **Step 1: 创建测试包**

创建 `tests/__init__.py`，内容为空文件。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_db_extensions.py`：

```python
"""DB 扩展测试：告警 5 张新表 + services 加列 + JSON 字段往返"""
import os
import tempfile
import unittest

from webapp import db as dbm


class TestDbExtensions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)

    def test_new_tables_exist(self):
        conn = dbm.get_conn(self.db)
        try:
            names = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        for t in ("alert_rules", "alerts", "alert_events",
                  "silence_windows", "platform_settings"):
            self.assertIn(t, names)

    def test_alerts_fingerprint_partial_unique(self):
        """同 fingerprint 只能有一条非 resolved 告警（收敛的 DB 层保证）"""
        conn = dbm.get_conn(self.db)
        try:
            idx = [r["sql"] for r in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'")
                if r["sql"] and "idx_alerts_active" in r["sql"]]
        finally:
            conn.close()
        self.assertEqual(len(idx), 1)
        self.assertIn("WHERE status != 'resolved'", idx[0])

    def test_services_new_columns(self):
        conn = dbm.get_conn(self.db)
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
        finally:
            conn.close()
        self.assertIn("log_retention_days", cols)
        self.assertIn("log_collect_interval", cols)

    def test_alert_rules_json_roundtrip(self):
        rid = dbm.create_record(self.db, "alert_rules", dict(
            name="cpu", metric_type="metric", metric="cpu", compare=">",
            threshold=80, duration_seconds=300,
            scope={"type": "all", "ids": []}, severity="P2",
            channels=["email"], escalation_interval=30, enabled=1,
            remark="", created_at=1, updated_at=1))
        r = dbm.get_record(self.db, "alert_rules", rid)
        self.assertEqual(r["scope"]["type"], "all")
        self.assertEqual(r["channels"], ["email"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m unittest tests.test_db_extensions -v`
Expected: FAIL——`AssertionError: 'alert_rules' not in ...`（表不存在）

- [ ] **Step 4: db.py 扩展 SCHEMA 与新表**

在 `webapp/db.py` 的 `SCHEMA` 末尾（`audit_logs` 表定义之后）追加：

```sql
CREATE TABLE IF NOT EXISTS alert_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    metric_type TEXT NOT NULL DEFAULT 'metric',
    metric TEXT DEFAULT '',
    compare TEXT DEFAULT '>',
    threshold REAL DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,
    scope TEXT DEFAULT '{"type":"all","ids":[]}',
    severity TEXT DEFAULT 'P2',
    channels TEXT DEFAULT '[]',
    escalation_interval INTEGER DEFAULT 30,
    enabled INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id INTEGER DEFAULT 0,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'firing',
    severity TEXT NOT NULL DEFAULT 'P2',
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    target_info TEXT DEFAULT '{}',
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    ack_by TEXT DEFAULT '',
    ack_at INTEGER,
    resolved_by TEXT DEFAULT '',
    resolved_at INTEGER,
    resolved_type TEXT DEFAULT '',
    notify_count INTEGER DEFAULT 0,
    escalation_level INTEGER DEFAULT 0,
    escalation_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_active
    ON alerts(fingerprint) WHERE status != 'resolved';
CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    target TEXT DEFAULT '',
    payload TEXT DEFAULT '',
    ok INTEGER DEFAULT 0,
    detail TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS silence_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'one_time',
    start_at TEXT DEFAULT '0',
    end_at TEXT DEFAULT '0',
    weekdays TEXT DEFAULT '[]',
    scope TEXT DEFAULT '{"rules":[],"envs":[],"servers":[]}',
    enabled INTEGER DEFAULT 1,
    remark TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS platform_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value TEXT DEFAULT '',
    updated_at INTEGER NOT NULL
);
```

说明：`silence_windows.start_at/end_at` 用 TEXT——one_time 存 epoch 秒字符串，recurring 存 `"HH:MM"`（引擎侧按类型解释）。

- [ ] **Step 5: 更新 TABLES 白名单与 JSON_FIELDS**

`webapp/db.py` 中：

```python
TABLES = frozenset(("projects", "services", "environments", "servers",
                    "env_servers", "pipelines", "users", "tasks", "audit_logs",
                    "alert_rules", "alerts", "alert_events",
                    "silence_windows", "platform_settings"))
```

```python
JSON_FIELDS = {
    "services": {"build_order", "nacos_groups", "dubbo_services", "log_error_keywords"},
    "environments": {"registry", "nacos", "mysql_snailjob", "ssh", "build", "deploy"},
    "pipelines": {"stages"},
    "alert_rules": {"scope", "channels"},
    "alerts": {"target_info"},
    "silence_windows": {"weekdays", "scope"},
}
```

- [ ] **Step 6: _migrate 增加 services 加列（幂等）**

将 `webapp/db.py` 的 `_migrate` 整体替换为（services 加列先于 role 迁移执行，避免提前 return 跳过加列）：

```python
def _migrate(conn):
    """存量库结构迁移（幂等）。"""
    srv_cols = {r["name"] for r in conn.execute("PRAGMA table_info(services)")}
    if "log_retention_days" not in srv_cols:
        conn.execute("ALTER TABLE services ADD COLUMN log_retention_days INTEGER")
    if "log_collect_interval" not in srv_cols:
        conn.execute("ALTER TABLE services ADD COLUMN log_collect_interval INTEGER")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(servers)")}
    if "role" in cols:
        return
    conn.execute("ALTER TABLE servers ADD COLUMN role TEXT DEFAULT ''")
    conn.execute("UPDATE servers SET role = remark, remark = '' "
                 "WHERE remark IS NOT NULL AND remark != ''")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `python -m unittest tests.test_db_extensions -v`
Expected: 4 个测试全部 PASS

- [ ] **Step 8: 提交**

```bash
git add webapp/db.py tests/__init__.py tests/test_db_extensions.py
git commit -m "feat: 告警系统数据库扩展（5 张新表 + services 加列）"
```

---

### Task 2: alert_channels.py（6 种通知渠道）

**Files:**
- Create: `webapp/alert_channels.py`
- Create: `tests/test_alert_channels.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_alert_channels.py`：

```python
"""通知渠道测试：配置解析/环境变量覆盖/payload 构造/失败重试（mock 网络）"""
import os
import tempfile
import unittest
from unittest import mock

from webapp import db as dbm
from webapp import alert_channels as ac


class TestChannels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.alert = dict(
            id=1, severity="P1", subject="服务下线: hqb-boot-system",
            description="服务 hqb-boot-system 在环境 test 未部署",
            target_info={"env": "test", "service": "hqb-boot-system"},
            first_seen_at=1700000000)

    def test_channel_config_env_override(self):
        os.environ["ALERT_SMTP_HOST"] = "smtp.example.com"
        try:
            cfg = ac.get_channel_config(self.db, "email")
            self.assertEqual(cfg["host"], "smtp.example.com")
        finally:
            del os.environ["ALERT_SMTP_HOST"]

    def test_format_alert_text(self):
        text = ac.format_alert_text(self.alert)
        self.assertIn("【P1】", text)
        self.assertIn("hqb-boot-system", text)

    def test_wecom_payload(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"errcode": 0}
            ac._send_wecom({"webhook": "https://qyapi.weixin.qq.com/hook"}, "text", {})
            args, kwargs = m.call_args
            self.assertEqual(kwargs["json"]["msgtype"], "text")
            self.assertEqual(kwargs["json"]["text"]["content"], "text")

    def test_wecom_error_raises(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"errcode": 93000}
            with self.assertRaises(RuntimeError):
                ac._send_wecom({"webhook": "https://x"}, "text", {})

    def test_aliyun_sms_payload_and_sign(self):
        with mock.patch("requests.post") as m:
            m.return_value.json.return_value = {"Code": "OK"}
            ac._send_aliyun_sms(
                {"access_key": "ak", "secret": "sk", "sign": "S",
                 "template": "T1", "phones": "13800000000"}, "告警", self.alert)
            args, kwargs = m.call_args
            self.assertEqual(args[0], "https://dysmsapi.aliyuncs.com/")
            self.assertIn("AccessKeyId", kwargs["data"])
            self.assertIn("Signature", kwargs["data"])
            self.assertIn("TemplateParam", kwargs["data"])

    def test_send_alert_retry_then_success(self):
        """第一次失败第二次成功：重试生效且结果记录 ok"""
        with mock.patch.object(ac, "_send_wecom") as sw:
            sw.side_effect = [RuntimeError("boom"), None]
            results = ac.send_alert(self.db, self.alert, ["wecom"])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][1])
        self.assertEqual(sw.call_count, 2)
        evs = dbm.list_records(self.db, "alert_events")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["ok"], 1)

    def test_send_alert_all_fail_records_fail(self):
        with mock.patch.object(ac, "_send_wecom",
                               side_effect=RuntimeError("boom")):
            results = ac.send_alert(self.db, self.alert, ["wecom"])
        self.assertFalse(results[0][1])
        evs = dbm.list_records(self.db, "alert_events")
        self.assertEqual(evs[0]["ok"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_alert_channels -v`
Expected: FAIL——`ModuleNotFoundError: No module named 'webapp.alert_channels'`

- [ ] **Step 3: 实现 alert_channels.py**

创建 `webapp/alert_channels.py`（完整内容）：

```python
"""告警通知渠道：邮件 SMTP / 企业微信机器人 / 通用 Webhook / 阿里云短信 / 飞书 / 钉钉

渠道配置存 platform_settings 表（key=channel.<name>，value=JSON），
环境变量覆盖（与 scripts/common.py env_or_config 语义一致）：
    email:      ALERT_SMTP_HOST / ALERT_SMTP_PORT / ALERT_SMTP_USER /
                ALERT_SMTP_PASSWORD / ALERT_SMTP_FROM / ALERT_SMTP_TO
    wecom:      ALERT_WECOM_WEBHOOK
    webhook:    ALERT_WEBHOOK_URL
    sms_aliyun: ALERT_ALIYUN_AK / ALERT_ALIYUN_SK / ALERT_ALIYUN_SIGN /
                ALERT_ALIYUN_TEMPLATE / ALERT_ALIYUN_PHONES
    feishu:     NOTIFY_FEISHU_WEBHOOK（与 scripts/notify.py 同变量）
    dingtalk:   NOTIFY_DINGTALK_WEBHOOK / NOTIFY_DINGTALK_SECRET
"""
import base64
import hashlib
import hmac
import json
import os
import smtplib
import time
import urllib.parse
import uuid
from email.header import Header
from email.mime.text import MIMEText

import requests

from . import db as dbm

# 发送失败重试次数（指数退避：1s、2s）
RETRIES = 2

CHANNELS = ("email", "wecom", "webhook", "sms_aliyun", "feishu", "dingtalk")


def get_channel_config(db_path: str, channel: str) -> dict:
    """读取渠道配置：platform_settings 为底，环境变量覆盖（保持幂等返回 dict）"""
    rows = dbm.list_records(db_path, "platform_settings", "key=?", (f"channel.{channel}",))
    cfg = {}
    if rows and rows[0]["value"]:
        try:
            cfg = json.loads(rows[0]["value"]) or {}
        except ValueError:
            cfg = {}
    if channel == "email":
        cfg = dict(cfg,
                   host=os.environ.get("ALERT_SMTP_HOST") or cfg.get("host", ""),
                   port=int(os.environ.get("ALERT_SMTP_PORT") or cfg.get("port", 465)),
                   user=os.environ.get("ALERT_SMTP_USER") or cfg.get("user", ""),
                   password=os.environ.get("ALERT_SMTP_PASSWORD") or cfg.get("password", ""),
                   from_addr=os.environ.get("ALERT_SMTP_FROM") or cfg.get("from_addr", ""),
                   to_addrs=os.environ.get("ALERT_SMTP_TO") or cfg.get("to_addrs", ""))
    elif channel == "wecom":
        cfg = dict(cfg, webhook=os.environ.get("ALERT_WECOM_WEBHOOK") or cfg.get("webhook", ""))
    elif channel == "webhook":
        cfg = dict(cfg, url=os.environ.get("ALERT_WEBHOOK_URL") or cfg.get("url", ""))
    elif channel == "sms_aliyun":
        cfg = dict(cfg,
                   access_key=os.environ.get("ALERT_ALIYUN_AK") or cfg.get("access_key", ""),
                   secret=os.environ.get("ALERT_ALIYUN_SK") or cfg.get("secret", ""),
                   sign=os.environ.get("ALERT_ALIYUN_SIGN") or cfg.get("sign", ""),
                   template=os.environ.get("ALERT_ALIYUN_TEMPLATE") or cfg.get("template", ""),
                   phones=os.environ.get("ALERT_ALIYUN_PHONES") or cfg.get("phones", ""),
                   region=os.environ.get("ALERT_ALIYUN_REGION") or cfg.get("region", "cn-hangzhou"))
    elif channel == "feishu":
        cfg = dict(cfg, webhook=os.environ.get("NOTIFY_FEISHU_WEBHOOK") or cfg.get("webhook", ""))
    elif channel == "dingtalk":
        cfg = dict(cfg,
                   webhook=os.environ.get("NOTIFY_DINGTALK_WEBHOOK") or cfg.get("webhook", ""),
                   secret=os.environ.get("NOTIFY_DINGTALK_SECRET") or cfg.get("secret", ""))
    return cfg


def format_alert_text(alert: dict) -> str:
    """组装告警通知文本"""
    lines = [f"🚨【{alert['severity']}】{alert['subject']}"]
    if alert.get("description"):
        lines.append(alert["description"])
    ti = alert.get("target_info") or {}
    if ti.get("env"):
        lines.append(f"环境: {ti['env']}")
    if ti.get("host"):
        lines.append(f"服务器: {ti['host']}")
    if ti.get("service"):
        lines.append(f"服务: {ti['service']}")
    lines.append(f"首次触发: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert['first_seen_at']))}")
    return "\n".join(lines)


def _send_email(cfg: dict, text: str, alert: dict):
    if not (cfg.get("host") and cfg.get("to_addrs")):
        raise RuntimeError("邮件渠道未配置 host/to_addrs")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(text.splitlines()[0][:60], "utf-8")
    msg["From"] = cfg.get("from_addr") or cfg.get("user") or ""
    msg["To"] = cfg["to_addrs"]
    port = int(cfg.get("port") or 465)
    to_list = [x.strip() for x in cfg["to_addrs"].split(",") if x.strip()]
    if port == 465:
        with smtplib.SMTP_SSL(cfg["host"], port, timeout=10) as s:
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
            s.sendmail(msg["From"], to_list, msg.as_string())
    else:
        with smtplib.SMTP(cfg["host"], port, timeout=10) as s:
            s.starttls()
            if cfg.get("user"):
                s.login(cfg["user"], cfg.get("password", ""))
            s.sendmail(msg["From"], to_list, msg.as_string())


def _send_wecom(cfg: dict, text: str, alert: dict):
    if not cfg.get("webhook"):
        raise RuntimeError("企业微信渠道未配置 webhook")
    resp = requests.post(cfg["webhook"],
                         json={"msgtype": "text", "text": {"content": text}}, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误: {data}")


def _send_webhook(cfg: dict, text: str, alert: dict):
    if not cfg.get("url"):
        raise RuntimeError("Webhook 渠道未配置 url")
    payload = {"alert": alert, "text": text,
               "severity": alert["severity"], "ts": int(time.time())}
    requests.post(cfg["url"], json=payload, timeout=10).raise_for_status()


def _aliyun_sign(params: dict, secret: str) -> str:
    """阿里云 RPC 签名（HMAC-SHA1），与官方 SDK 等价实现"""
    items = sorted(params.items())
    qs = "&".join(f"{urllib.parse.quote(k, safe='')}="
                  f"{urllib.parse.quote(str(v), safe='')}" for k, v in items)
    string_to_sign = "POST&%2F&" + urllib.parse.quote(qs, safe="")
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def _send_aliyun_sms(cfg: dict, text: str, alert: dict):
    if not (cfg.get("access_key") and cfg.get("template")):
        raise RuntimeError("短信渠道未配置 access_key/template")
    params = {
        "AccessKeyId": cfg["access_key"], "Action": "SendSms",
        "Format": "JSON", "PhoneNumbers": cfg.get("phones", ""),
        "RegionId": cfg.get("region", "cn-hangzhou"),
        "SignName": cfg.get("sign", ""),
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": str(uuid.uuid4()),
        "SignatureVersion": "1.0",
        "TemplateCode": cfg["template"],
        "TemplateParam": json.dumps({"content": text[:200]}, ensure_ascii=False),
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "Version": "2017-05-25",
    }
    params["Signature"] = _aliyun_sign(params, cfg.get("secret", ""))
    resp = requests.post("https://dysmsapi.aliyuncs.com/", data=params, timeout=10)
    data = resp.json()
    if data.get("Code") != "OK":
        raise RuntimeError(f"短信返回错误: {data}")


def _send_feishu(cfg: dict, text: str, alert: dict):
    if not cfg.get("webhook"):
        raise RuntimeError("飞书渠道未配置 webhook")
    resp = requests.post(cfg["webhook"],
                         json={"msg_type": "text", "content": {"text": text}}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书返回错误: {data}")


def _dingtalk_sign(secret: str):
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return ts, urllib.parse.quote_plus(base64.b64encode(digest))


def _send_dingtalk(cfg: dict, text: str, alert: dict):
    if not cfg.get("webhook"):
        raise RuntimeError("钉钉渠道未配置 webhook")
    params = {}
    if cfg.get("secret"):
        ts, sign = _dingtalk_sign(cfg["secret"])
        params = {"timestamp": ts, "sign": sign}
    resp = requests.post(cfg["webhook"], params=params,
                         json={"msgtype": "text", "text": {"content": text}}, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"钉钉返回错误: {data}")


_CHANNEL_FUNCS = {"email": _send_email, "wecom": _send_wecom,
                  "webhook": _send_webhook, "sms_aliyun": _send_aliyun_sms,
                  "feishu": _send_feishu, "dingtalk": _send_dingtalk}


def send_alert(db_path: str, alert: dict, channels: list) -> list:
    """按渠道发送告警通知（失败重试 2 次指数退避），结果写 alert_events。

    返回 [(channel, ok, detail), ...]；任何异常不外抛（告警引擎不被通知阻塞）。
    """
    text = format_alert_text(alert)
    results = []
    now = int(time.time())
    for channel in channels:
        if channel not in _CHANNEL_FUNCS:
            continue
        cfg = get_channel_config(db_path, channel)
        ok, detail = False, ""
        for attempt in range(RETRIES + 1):
            try:
                _CHANNEL_FUNCS[channel](cfg, text, alert)
                ok, detail = True, "ok"
                break
            except Exception as e:
                detail = str(e)[:200]
                if attempt < RETRIES:
                    time.sleep(2 ** attempt)
        results.append((channel, ok, detail))
        dbm.create_record(db_path, "alert_events", dict(
            alert_id=alert["id"], channel=channel, target="", payload="",
            ok=1 if ok else 0, detail=detail, created_at=now))
    return results
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_alert_channels -v`
Expected: 7 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
 git add webapp/alert_channels.py tests/test_alert_channels.py
 git commit -m "feat: 告警通知渠道（邮件/企微/Webhook/阿里云短信/飞书/钉钉）"
```

---

### Task 3: alert_engine.py（规则引擎核心）

**Files:**
- Create: `webapp/alert_engine.py`
- Create: `tests/test_alert_engine.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_alert_engine.py`：

```python
"""告警引擎测试：滑动窗口/收敛/状态机/升级/静默/种子规则（用 FakeMonitor 替代真实探测）"""
import os
import tempfile
import time
import unittest

from webapp import db as dbm
from webapp.alert_engine import AlertEngine, DEFAULT_RULES


class FakeMonitor:
    """模拟 monitor 探测缓存：get_overview(project_id, env) → 固定数据"""
    def __init__(self, overviews):
        self._data = overviews

    def get_overview(self, project_id, env):
        return self._data.get((project_id, env), {"services": {}, "host_metrics": {}})


def make_env(db, project_id=1):
    return dbm.create_record(db, "environments", dict(
        project_id=project_id, name="test", is_default=1,
        registry="{}", nacos="{}", mysql_snailjob="{}", ssh="{}",
        build="{}", deploy="{}", monitor_enabled=1))


def make_project(db):
    return dbm.create_record(db, "projects", dict(
        name="p1", code="p1", description="", git_org="", created_at=1))


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "test.db")
        dbm.init_db(self.db)
        self.proj_id = make_project(self.db)
        self.env_id = make_env(self.db)

    def _engine(self, interval=30, overviews=None):
        return AlertEngine(self.db, interval=interval,
                           monitor=FakeMonitor(overviews or {}))

    def _metric_rule(self, metric="cpu", threshold=80, duration=300, **kw):
        return dbm.create_record(self.db, "alert_rules", dict(
            name=f"{metric} test", metric_type="metric", metric=metric,
            compare=">", threshold=threshold, duration_seconds=duration,
            scope={"type": "all", "ids": []}, severity="P2", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1, **kw))

    # ---- 种子规则 ----
    def test_seed_rules_idempotent(self):
        e = self._engine()
        e._seed_rules()
        e._seed_rules()  # 再次执行不重复
        rules = dbm.list_records(self.db, "alert_rules")
        self.assertEqual(len(rules), len(DEFAULT_RULES))
        names = {r["name"] for r in rules}
        self.assertIn("主机 CPU 过高", names)
        self.assertIn("服务下线", names)
        self.assertIn("任务失败", names)

    # ---- 滑动窗口持续判定 ----
    def test_metric_fires_after_duration(self):
        self._metric_rule(metric="cpu", threshold=80, duration=300)  # 30s×10 轮
        e = self._engine(interval=30)
        # 推送 9 轮满足 → 不触发；第 10 轮满足 → 触发
        for i in range(9):
            e._push_history((self.env_id, "h1", "cpu"), 95.0)
            e._evaluate_metric_rules()
        self.assertEqual(len(dbm.list_records(self.db, "alerts")), 0)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "firing")
        self.assertEqual(alerts[0]["severity"], "P2")

    def test_metric_recovers_no_fire(self):
        self._metric_rule(metric="cpu", threshold=80, duration=300)
        e = self._engine(interval=30)
        values = [95.0] * 5 + [10.0] * 6  # 中途恢复 → 连续满足重置
        for v in values:
            e._push_history((self.env_id, "h1", "cpu"), v)
            e._evaluate_metric_rules()
        self.assertEqual(len(dbm.list_records(self.db, "alerts")), 0)

    # ---- 收敛 ----
    def test_convergence_single_alert(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        for _ in range(3):
            e._push_history((self.env_id, "h1", "cpu"), 95.0)
            e._evaluate_metric_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["last_seen_at"], alerts[0]["first_seen_at"])

    # ---- 状态机 ----
    def test_ack_and_resolve(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"],
                          dict(status="acked", ack_by="admin", ack_at=int(time.time())))
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["status"], "acked")
        self.assertEqual(a["ack_by"], "admin")
        dbm.update_record(self.db, "alerts", alert["id"],
                          dict(status="resolved", resolved_by="admin",
                               resolved_at=int(time.time()), resolved_type="manual"))
        self.assertEqual(dbm.get_record(self.db, "alerts", alert["id"])["status"], "resolved")

    # ---- 自动恢复 ----
    def test_auto_resolve_on_recovery(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        self.assertEqual(alert["status"], "firing")
        # 连续 2 轮恢复 → auto resolved
        e._push_history((self.env_id, "h1", "cpu"), 10.0)
        e._evaluate_metric_rules()
        e._push_history((self.env_id, "h1", "cpu"), 10.0)
        e._auto_resolve()
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["status"], "resolved")
        self.assertEqual(a["resolved_type"], "auto")

    # ---- 升级 ----
    def test_escalation(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0, escalation_interval=30)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"], dict(
            status="acked", escalation_at=int(time.time()) - 1))
        e._escalate()
        a = dbm.get_record(self.db, "alerts", alert["id"])
        self.assertEqual(a["severity"], "P1")  # P2 → P1
        self.assertEqual(a["escalation_level"], 1)
        self.assertGreater(a["escalation_at"], int(time.time()))

    def test_no_escalation_at_p0(self):
        self._metric_rule(metric="cpu", threshold=80, duration=0, severity="P0",
                          escalation_interval=30)
        e = self._engine(interval=30)
        e._push_history((self.env_id, "h1", "cpu"), 95.0)
        e._evaluate_metric_rules()
        alert = dbm.list_records(self.db, "alerts")[0]
        dbm.update_record(self.db, "alerts", alert["id"], dict(
            status="acked", escalation_at=int(time.time()) - 1))
        e._escalate()
        self.assertEqual(dbm.get_record(self.db, "alerts", alert["id"])["severity"], "P0")

    # ---- 静默窗口 ----
    def _silence(self, **kw):
        now = int(time.time())
        return dbm.create_record(self.db, "silence_windows", dict(
            name="win", type="one_time",
            start_at=str(now - 60), end_at=str(now + 60),
            weekdays=[], scope={"rules": [], "envs": [], "servers": []},
            enabled=1, remark="", created_at=now, **kw))

    def test_silence_one_time_global(self):
        self._silence()
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules",
                              self._metric_rule(metric="cpu", duration=0))
        self.assertTrue(e._silenced(rule, {"env": "test", "host": "h1"}))

    def test_silence_by_rule_scope(self):
        rule_id = self._metric_rule(metric="cpu", duration=0)
        self._silence(scope={"rules": [rule_id], "envs": [], "servers": []})
        e = self._engine()
        self.assertTrue(e._silenced(dbm.get_record(self.db, "alert_rules", rule_id), {}))
        other = dbm.get_record(self.db, "alert_rules",
                               self._metric_rule(metric="disk", duration=0))
        self.assertFalse(e._silenced(other, {}))

    def test_silence_recurring_weekday(self):
        e = self._engine()
        lt = time.localtime()
        w = dict(name="weekend", type="recurring", start_at="00:00",
                 end_at="23:59", weekdays=[lt.tm_wday],
                 scope={"rules": [], "envs": [], "servers": []},
                 enabled=1, remark="", created_at=1)
        w["id"] = dbm.create_record(self.db, "silence_windows", w)
        self.assertTrue(e._window_active(w, time.time()))
        w["weekdays"] = [(lt.tm_wday + 1) % 7]
        self.assertFalse(e._window_active(w, time.time()))

    def test_silence_expired(self):
        self._silence(start_at=str(int(time.time()) - 120),
                      end_at=str(int(time.time()) - 60))
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules",
                              self._metric_rule(metric="cpu", duration=0))
        self.assertFalse(e._silenced(rule, {}))

    # ---- 服务下线状态规则 ----
    def test_service_down_fires(self):
        dbm.create_record(self.db, "alert_rules", dict(
            name="down", metric_type="status", metric="service_down",
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P1", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1))
        e = self._engine(overviews={
            (self.proj_id, "test"): {"services": {"svc-a": {"status": "not_deployed"}},
                                     "host_metrics": {}}})
        e._evaluate_status_rules()
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("svc-a", alerts[0]["subject"])
        # 服务恢复在线 → 自动 resolved
        e._monitor._data[(self.proj_id, "test")]["services"]["svc-a"]["status"] = "online"
        e._auto_resolve()
        self.assertEqual(dbm.get_record(self.db, "alerts", alerts[0]["id"])["status"],
                         "resolved")

    # ---- 任务失败检测 ----
    def test_task_failed_fires(self):
        dbm.create_record(self.db, "alert_rules", dict(
            name="taskfail", metric_type="status", metric="task_failed",
            compare=">", threshold=0, duration_seconds=0,
            scope={"type": "all", "ids": []}, severity="P2", channels=["wecom"],
            escalation_interval=30, enabled=1, remark="", created_at=1, updated_at=1))
        dbm.create_task(self.db, self.proj_id, "deploy", 1, "发布", "test",
                        "v1", "", 1)
        dbm.update_task(self.db, 1, status="failed", finished_at=int(time.time()))
        e = self._engine()
        e._check_tasks()
        e._check_tasks()  # 重复扫描不重复告警
        alerts = dbm.list_records(self.db, "alerts")
        self.assertEqual(len(alerts), 1)
        self.assertIn("发布", alerts[0]["subject"])

    # ---- 作用域 ----
    def test_scope_env_filter(self):
        rid = self._metric_rule(metric="cpu", duration=0,
                                scope={"type": "env", "ids": [999]})
        e = self._engine()
        rule = dbm.get_record(self.db, "alert_rules", rid)
        env = {"id": self.env_id, "project_id": self.proj_id, "name": "test"}
        self.assertFalse(e._in_scope(rule, env, host="h1"))
        rule["scope"] = {"type": "env", "ids": [self.env_id]}
        self.assertTrue(e._in_scope(rule, env, host="h1"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m unittest tests.test_alert_engine -v`
Expected: FAIL——`ModuleNotFoundError: No module named 'webapp.alert_engine'`

- [ ] **Step 3: 实现 alert_engine.py**

创建 `webapp/alert_engine.py`（完整内容）：

```python
"""告警规则引擎：规则评估（滑动窗口持续判定）、状态机、升级/收敛、静默窗口、任务失败检测

AlertEvaluator 守护线程每 interval 秒一轮：
    1. 指标规则：读 monitor 探测缓存 host_metrics，滑动窗口判断连续满足阈值 ≥ duration
    2. 状态规则：服务下线（not_deployed）、任务失败（扫描 tasks 表）
    3. 自动恢复：指标/服务恢复后连续 RECOVER_ROUNDS 轮正常 → resolved(auto)
    4. 升级：acked 超时 escalation_interval 未解决 → 级别提升并重新通知

收敛：alerts.fingerprint 唯一索引（部分索引，仅非 resolved），
同指纹重复触发只更新 last_seen_at，不重复建告警。
"""
import sys
import threading
import time
from collections import deque

from . import db as dbm
from .alert_channels import send_alert

SEVERITY_ORDER = ("P0", "P1", "P2", "P3")

# 内置默认规则种子（首次启动导入，可编辑/禁用）
DEFAULT_RULES = [
    dict(name="主机 CPU 过高", metric_type="metric", metric="cpu",
         compare=">", threshold=80, duration_seconds=300,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="磁盘使用率过高", metric_type="metric", metric="disk",
         compare=">", threshold=90, duration_seconds=300,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="服务下线", metric_type="status", metric="service_down",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P1",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="任务失败", metric_type="status", metric="task_failed",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P2",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则"),
    dict(name="日志错误关键词", metric_type="log_keyword", metric="",
         compare=">", threshold=0, duration_seconds=0,
         scope={"type": "all", "ids": []}, severity="P3",
         channels=["feishu"], escalation_interval=30, remark="内置默认规则（阶段三联动启用）"),
]

# 自动恢复判定轮数（连续 N 轮正常才 resolved，避免单轮抖动误恢复）
RECOVER_ROUNDS = 2


def _compare(op: str, value, threshold) -> bool:
    if value is None:
        return False
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
    return ops.get(op, lambda a, b: a > b)(value, threshold)


class AlertEngine:
    """告警引擎（DB 每轮独立连接，与 monitor 相同模式）"""

    def __init__(self, db_path: str, interval: int = 30, monitor=None):
        self._db_path = db_path
        self._interval = max(10, int(interval))
        self._monitor = monitor
        self._stop = threading.Event()
        self._thread = None
        # 滑动窗口：{(env_id, host, metric): deque[(ts, value)]}
        self._history = {}
        self._max_history = 40  # 40 轮 = 20 分钟 @30s
        self._last_task_scan = 0.0
        self._seed_rules()

    # ---------- 调度 ----------

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="alert-engine")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                # 引擎自愈：单周期异常不退出（与 monitor 调度器同策略）
                print(f"[alert] 评估周期异常: {e!r}", file=sys.stderr)
            self._stop.wait(self._interval)

    def _cycle(self):
        self._evaluate_metric_rules()
        self._evaluate_status_rules()
        self._check_tasks()
        self._auto_resolve()
        self._escalate()

    # ---------- 种子规则 ----------

    def _seed_rules(self):
        """首次启动导入内置默认规则（幂等：表非空即跳过）"""
        if dbm.list_records(self._db_path, "alert_rules"):
            return
        now = int(time.time())
        for r in DEFAULT_RULES:
            dbm.create_record(self._db_path, "alert_rules", dict(
                name=r["name"], metric_type=r["metric_type"],
                metric=r["metric"], compare=r["compare"],
                threshold=r["threshold"], duration_seconds=r["duration_seconds"],
                scope=r["scope"], severity=r["severity"], channels=r["channels"],
                escalation_interval=r["escalation_interval"], enabled=1,
                remark=r["remark"], created_at=now, updated_at=now))

    # ---------- 滑动窗口 ----------

    def _push_history(self, key, value):
        """记录一轮探测值；None 视为探测失败（不参与连续计数但保留位置）"""
        dq = self._history.setdefault(key, deque(maxlen=self._max_history))
        dq.append((time.time(), value))

    def _consecutive_hits(self, rule, key) -> int:
        """从最新一轮向前数连续满足阈值的轮数"""
        dq = self._history.get(key)
        if not dq:
            return 0
        n = 0
        for ts, value in reversed(dq):
            if _compare(rule.get("compare", ">"), value, rule.get("threshold", 0)):
                n += 1
            else:
                break
        return n

    def _duration_hit(self, rule, key) -> bool:
        """连续满足轮数 × 周期 ≥ duration_seconds 才触发（duration=0 时一轮即触发）"""
        duration = int(rule.get("duration_seconds", 0) or 0)
        if duration <= 0:
            return self._consecutive_hits(rule, key) >= 1
        return self._consecutive_hits(rule, key) * self._interval >= duration

    # ---------- 指标规则 ----------

    def _evaluate_metric_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "metric"]
        if not rules or not self._monitor:
            return
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for host, m in (data.get("host_metrics") or {}).items():
                for rule in rules:
                    metric = rule.get("metric") or "cpu"
                    if metric not in ("cpu", "mem", "disk", "load"):
                        continue
                    value = m.get(metric)
                    key = (env["id"], host, metric)
                    self._push_history(key, value)
                    if not self._duration_hit(rule, key):
                        continue
                    if not self._in_scope(rule, env, host=host):
                        continue
                    self._fire(rule, f"host:{env['id']}:{host}",
                               subject=f"{host} {metric.upper()} 过高",
                               description=f"当前 {value}%，阈值 {rule['compare']}{rule['threshold']}%，"
                                           f"持续 {rule['duration_seconds']}s",
                               target_info={"env": env["name"], "host": host})

    # ---------- 状态规则 ----------

    def _evaluate_status_rules(self):
        rules = [r for r in dbm.list_records(self._db_path, "alert_rules")
                 if r.get("enabled") and r.get("metric_type") == "status"]
        if not rules or not self._monitor:
            return
        for env in dbm.list_records(self._db_path, "environments"):
            data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
            for name, st in (data.get("services") or {}).items():
                for rule in rules:
                    metric = rule.get("metric", "service_down")
                    if metric == "service_down" and st.get("status") == "not_deployed":
                        if not self._in_scope(rule, env, service=name):
                            continue
                        self._fire(rule, f"service:{env['id']}:{name}",
                                   subject=f"服务下线: {name}",
                                   description=f"服务 {name} 在环境 {env['name']} 未部署或已停止",
                                   target_info={"env": env["name"], "service": name})

    def _check_tasks(self):
        """扫描最近失败任务（增量：finished_at > 上次扫描点）触发任务失败告警"""
        now = time.time()
        rows = dbm.list_records(self._db_path, "tasks", "status='failed'",
                                "finished_at DESC")[:20]
        for t in rows:
            finished = t.get("finished_at") or 0
            if finished <= self._last_task_scan:
                continue
            fp = f"task:{t['id']}"
            if dbm.list_records(self._db_path, "alerts", "fingerprint=?", (fp,)):
                continue
            rule = self._task_fail_rule()
            if not rule:
                continue
            self._fire(rule, fp,
                       subject=f"任务失败: {t['name']}",
                       description=f"任务 #{t['id']} {t['name']} [{t.get('env','')}] 执行失败",
                       target_info={"env": t.get("env", ""), "task_id": t["id"]})
        self._last_task_scan = now

    def _task_fail_rule(self):
        rows = [r for r in dbm.list_records(self._db_path, "alert_rules")
                if r.get("enabled") and r.get("metric") == "task_failed"]
        return rows[0] if rows else None

    # ---------- 触发与收敛 ----------

    def _fire(self, rule: dict, target: str, subject: str,
              description: str, target_info: dict) -> int:
        """创建或更新告警（收敛）；新告警首次触发时通知（静默窗口内不通知）"""
        fp = f"{rule['id']}:{target}"
        rows = dbm.list_records(self._db_path, "alerts",
                                "fingerprint=? AND status != 'resolved'", (fp,))
        now = int(time.time())
        if rows:
            dbm.update_record(self._db_path, "alerts", rows[0]["id"],
                              {"last_seen_at": now})
            return rows[0]["id"]
        alert_id = dbm.create_record(self._db_path, "alerts", dict(
            rule_id=rule["id"], fingerprint=fp, status="firing",
            severity=rule.get("severity", "P2"), subject=subject,
            description=description, target_info=target_info,
            first_seen_at=now, last_seen_at=now, notify_count=0,
            escalation_level=0,
            escalation_at=now + int(rule.get("escalation_interval", 30)) * 60))
        alert = dbm.get_record(self._db_path, "alerts", alert_id)
        if self._silenced(rule, target_info):
            dbm.create_record(self._db_path, "alert_events", dict(
                alert_id=alert_id, channel="silenced", target="", payload="",
                ok=1, detail="静默窗口内，未发送通知", created_at=now))
        else:
            send_alert(self._db_path, alert, rule.get("channels") or [])
            dbm.update_record(self._db_path, "alerts", alert_id,
                              {"notify_count": 1})
        return alert_id

    # ---------- 静默窗口 ----------

    def _silenced(self, rule: dict, target_info: dict) -> bool:
        """命中任一启用窗口（作用域：规则/环境/服务器，空作用域=全局）"""
        now = time.time()
        for w in dbm.list_records(self._db_path, "silence_windows"):
            if not w.get("enabled") or not self._window_active(w, now):
                continue
            scope = w.get("scope") or {}
            rules = scope.get("rules") or []
            envs = scope.get("envs") or []
            servers = scope.get("servers") or []
            if not (rules or envs or servers):
                return True
            if rule["id"] in rules:
                return True
            if target_info.get("env") in envs:
                return True
            if target_info.get("host") in servers:
                return True
        return False

    def _window_active(self, w: dict, now: float) -> bool:
        """窗口时间匹配：one_time 用 epoch 秒；recurring 用 weekdays + HH:MM（支持跨午夜）"""
        if w.get("type") == "one_time":
            try:
                return float(w["start_at"]) <= now <= float(w["end_at"])
            except (TypeError, ValueError):
                return False
        lt = time.localtime(now)
        weekdays = w.get("weekdays") or []
        if weekdays and lt.tm_wday not in weekdays:
            return False

        def to_min(s):
            h, _, m = str(s).partition(":")
            return int(h) * 60 + int(m)

        cur = lt.tm_hour * 60 + lt.tm_min
        s_min = to_min(w.get("start_at", "00:00"))
        e_min = to_min(w.get("end_at", "23:59"))
        if s_min <= e_min:
            return s_min <= cur <= e_min
        return cur >= s_min or cur <= e_min  # 跨午夜

    # ---------- 作用域 ----------

    def _in_scope(self, rule: dict, env: dict, host: str = None,
                  service: str = None) -> bool:
        """规则作用域：all/project/env/server/service；ids 为对应 id/名称列表"""
        scope = rule.get("scope") or {"type": "all", "ids": []}
        t, ids = scope.get("type", "all"), scope.get("ids") or []
        if t == "all":
            return True
        if t == "project":
            return env["project_id"] in ids
        if t == "env":
            return env["id"] in ids
        if t == "server":
            return host in ids
        if t == "service":
            return service in ids
        return True

    # ---------- 自动恢复 ----------

    def _auto_resolve(self):
        """指标类告警：连续 RECOVER_ROUNDS 轮恢复 → resolved(auto)；
        服务下线告警：服务重新在线 → resolved(auto)；任务失败不自动恢复"""
        for a in dbm.list_records(self._db_path, "alerts",
                                  "status IN ('firing','acked')", "id"):
            rule = dbm.get_record(self._db_path, "alert_rules", a["rule_id"]) or {}
            if rule.get("metric_type") == "metric":
                if self._metric_recovered(rule, a):
                    self._resolve(a, "auto")
            elif rule.get("metric") == "service_down":
                if self._service_back(rule, a):
                    self._resolve(a, "auto")

    def _metric_recovered(self, rule: dict, alert: dict) -> bool:
        if not self._monitor:
            return False
        parts = alert["fingerprint"].split(":", 2)
        if len(parts) != 3 or parts[0] != "host":
            return False
        try:
            env_id = int(parts[1])
        except ValueError:
            return False
        host = parts[2]
        key = (env_id, host, rule.get("metric", "cpu"))
        dq = self._history.get(key)
        if not dq:
            return False
        n = 0
        for ts, value in reversed(dq):
            if _compare(rule.get("compare", ">"), value,
                        rule.get("threshold", 0)):
                return False  # 最新一轮仍超阈值
            n += 1
            if n >= RECOVER_ROUNDS:
                return True
        return False

    def _service_back(self, rule: dict, alert: dict) -> bool:
        if not self._monitor:
            return False
        parts = alert["fingerprint"].split(":")
        if len(parts) != 4 or parts[2] != "service":
            return False
        try:
            env_id = int(parts[2])
        except ValueError:
            return False
        name = parts[3]
        envs = [e for e in dbm.list_records(self._db_path, "environments")
                if e["id"] == env_id]
        if not envs:
            return False
        env = envs[0]
        data = self._monitor.get_overview(env["project_id"], env["name"]) or {}
        st = (data.get("services") or {}).get(name) or {}
        return st.get("status") == "online"

    def _resolve(self, alert: dict, rtype: str, by: str = ""):
        dbm.update_record(self._db_path, "alerts", alert["id"], dict(
            status="resolved", resolved_by=by, resolved_at=int(time.time()),
            resolved_type=rtype))

    # ---------- 升级 ----------

    def _escalate(self):
        """acked 超时未解决 → 级别提升一级（最多 P0），按新级别重新通知"""
        now = int(time.time())
        for a in dbm.list_records(self._db_path, "alerts",
                                  "status='acked' AND escalation_at IS NOT NULL "
                                  "AND escalation_at <= ?", (now,), "id"):
            rule = dbm.get_record(self._db_path, "alert_rules", a["rule_id"]) or {}
            sev = a.get("severity", "P2")
            if sev not in SEVERITY_ORDER:
                continue
            idx = SEVERITY_ORDER.index(sev)
            if idx <= 0:
                continue  # 已是 P0，不再升级
            new_sev = SEVERITY_ORDER[idx - 1]
            interval = int(rule.get("escalation_interval", 30)) * 60
            dbm.update_record(self._db_path, "alerts", a["id"], dict(
                severity=new_sev,
                escalation_level=a.get("escalation_level", 0) + 1,
                escalation_at=now + interval))
            a["severity"] = new_sev
            if not self._silenced(rule, a.get("target_info") or {}):
                send_alert(self._db_path, a, rule.get("channels") or [])

    # ---------- 供日志联动调用（阶段三） ----------

    def trigger_log_alert(self, rule: dict, service: str, env_name: str,
                          line: str, context: list, env_id: int):
        """日志关键词命中：创建/更新日志告警（指纹含关键词，收敛由部分唯一索引保证）"""
        keyword = rule.get("log_keyword") or "log"
        target = f"log:{env_id}:{service}:{keyword}"
        self._fire(rule, target,
                   subject=f"日志告警 [{keyword}]: {service}",
                   description=f"{env_name}/{service} 日志命中关键词 {keyword}:\n{line}",
                   target_info={"env": env_name, "service": service,
                                "keyword": keyword, "context": context[:5]})


_engine = None


def get_engine() -> "AlertEngine":
    global _engine
    return _engine


def init_engine(app) -> None:
    """启动告警引擎（依赖 monitor 实例；monitor 未启动时引擎不启动）"""
    global _engine
    from .monitor import get_monitor
    mon = get_monitor()
    if mon is None:
        return
    if _engine is None:
        _engine = AlertEngine(app.config["DB_PATH"],
                              app.config.get("MONITOR_INTERVAL", 30),
                              monitor=mon)
    _engine.start()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m unittest tests.test_alert_engine -v`
Expected: 15 个测试全部 PASS

- [ ] **Step 5: 提交**

```bash
git add webapp/alert_engine.py tests/test_alert_engine.py
git commit -m "feat: 告警规则引擎（滑动窗口/状态机/升级收敛/静默/任务失败检测）"
```

---

### Task 4: alert_routes.py（告警 API + 蓝图注册）

**Files:**
- Create: `webapp/routes/alert_routes.py`
- Modify: `webapp/__init__.py`

- [ ] **Step 1: 实现 alert_routes.py**

创建 `webapp/routes/alert_routes.py`（完整内容）：

```python
"""告警 API：列表/ack/resolve/统计 + 规则 CRUD + 静默窗口 CRUD + 渠道配置"""
import json
import time
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, request

from .. import db as dbm
from ..auth import audit, require_auth, require_role

alert_bp = Blueprint("alert", __name__)

SEVERITIES = ("P0", "P1", "P2", "P3")
STATUSES = ("firing", "acked", "resolved")
CHANNELS = ("email", "wecom", "webhook", "sms_aliyun", "feishu", "dingtalk")


def _paged():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(200, max(1, request.args.get("page_size", 20, type=int)))
    return page, page_size


# ---------- 告警实例 ----------

@alert_bp.route("/alerts")
@require_auth
def list_alerts():
    db = current_app.config["DB_PATH"]
    where, params = [], []
    st = request.args.get("status")
    if st in STATUSES:
        where.append("status=?"); params.append(st)
    sev = request.args.get("severity")
    if sev in SEVERITIES:
        where.append("severity=?"); params.append(sev)
    kw = (request.args.get("keyword") or "").strip()
    if kw:
        where.append("(subject LIKE ? OR description LIKE ?)")
        params += [f"%{kw}%"] * 2
    page, page_size = _paged()
    conn = dbm.get_conn(db)
    try:
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM alerts{wsql}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM alerts{wsql} ORDER BY id DESC LIMIT ? OFFSET ?",
                            params + [page_size, (page - 1) * page_size]).fetchall()
        items = [dbm._deserialize("alerts", dict(r)) for r in rows]
    finally:
        conn.close()
    return jsonify({"rows": items, "total": total, "page": page, "page_size": page_size})


@alert_bp.route("/alerts/<int:alert_id>/ack", methods=["POST"])
@require_auth
def ack_alert(alert_id):
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] == "resolved":
        return jsonify({"error": "已解决告警不能确认"}), 400
    rule = dbm.get_record(db, "alert_rules", a["rule_id"]) or {}
    dbm.update_record(db, "alerts", alert_id, dict(
        status="acked", ack_by=g.user["username"], ack_at=int(time.time()),
        escalation_at=int(time.time()) + int(rule.get("escalation_interval", 30)) * 60))
    audit("ack_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/<int:alert_id>/resolve", methods=["POST"])
@require_auth
def resolve_alert(alert_id):
    db = current_app.config["DB_PATH"]
    a = dbm.get_record(db, "alerts", alert_id)
    if not a:
        return jsonify({"error": "告警不存在"}), 404
    if a["status"] == "resolved":
        return jsonify({"error": "已解决"}), 400
    dbm.update_record(db, "alerts", alert_id, dict(
        status="resolved", resolved_by=g.user["username"],
        resolved_at=int(time.time()), resolved_type="manual"))
    audit("resolve_alert", "alert", alert_id, {"subject": a["subject"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/stats")
@require_auth
def alert_stats():
    """近 N 天按天/级别聚合（ECharts 趋势图数据源）"""
    db = current_app.config["DB_PATH"]
    days = min(30, max(1, request.args.get("days", 7, type=int)))
    since = int(time.time()) - days * 86400
    conn = dbm.get_conn(db)
    try:
        rows = conn.execute(
            "SELECT strftime('%m-%d', first_seen_at, 'unixepoch', 'localtime') d, "
            "severity, COUNT(*) n FROM alerts WHERE first_seen_at >= ? "
            "GROUP BY d, severity", (since,)).fetchall()
    finally:
        conn.close()
    by_day = {}
    for r in rows:
        by_day.setdefault(r["d"], {})[r["severity"]] = r["n"]
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%m-%d")
        out.append({"date": d,
                    **{s: by_day.get(d, {}).get(s, 0) for s in SEVERITIES}})
    return jsonify(out)


# ---------- 规则 CRUD ----------

@alert_bp.route("/alerts/rules")
@require_auth
def list_rules():
    return jsonify(dbm.list_records(current_app.config["DB_PATH"], "alert_rules",
                                    "", (), "id"))


def _validate_rule(body) -> str:
    """规则校验；返回错误信息，空串=通过"""
    if not (body.get("name") or "").strip():
        return "规则名称必填"
    if body.get("metric_type") not in ("metric", "status", "log_keyword"):
        return "metric_type 非法"
    if body.get("metric_type") == "metric" and body.get("metric") not in (
            "cpu", "mem", "disk", "load"):
        return "metric 非法"
    if body.get("metric_type") == "status" and body.get("metric") not in (
            "service_down", "task_failed"):
        return "metric 非法"
    if body.get("severity") not in SEVERITIES:
        return "severity 非法"
    if not body.get("channels"):
        return "至少选择一个通知渠道"
    return ""


@alert_bp.route("/alerts/rules", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_rule():
    body = request.get_json(silent=True) or {}
    err = _validate_rule(body)
    if err:
        return jsonify({"error": err}), 400
    now = int(time.time())
    rid = dbm.create_record(current_app.config["DB_PATH"], "alert_rules", dict(
        name=body["name"].strip(), metric_type=body["metric_type"],
        metric=body.get("metric", ""), compare=body.get("compare", ">"),
        threshold=float(body.get("threshold", 0)),
        duration_seconds=int(body.get("duration_seconds", 0)),
        scope=body.get("scope") or {"type": "all", "ids": []},
        severity=body["severity"], channels=body.get("channels") or [],
        escalation_interval=int(body.get("escalation_interval", 30)),
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), created_at=now, updated_at=now))
    audit("create_alert_rule", "alert_rule", rid, {"name": body["name"]})
    return jsonify({"id": rid})


@alert_bp.route("/alerts/rules/<int:rule_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_rule(rule_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "alert_rules", rule_id):
        return jsonify({"error": "规则不存在"}), 404
    body = request.get_json(silent=True) or {}
    err = _validate_rule(body)
    if err:
        return jsonify({"error": err}), 400
    dbm.update_record(db, "alert_rules", rule_id, dict(
        name=body["name"].strip(), metric_type=body["metric_type"],
        metric=body.get("metric", ""), compare=body.get("compare", ">"),
        threshold=float(body.get("threshold", 0)),
        duration_seconds=int(body.get("duration_seconds", 0)),
        scope=body.get("scope") or {"type": "all", "ids": []},
        severity=body["severity"], channels=body.get("channels") or [],
        escalation_interval=int(body.get("escalation_interval", 30)),
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), updated_at=int(time.time())))
    audit("update_alert_rule", "alert_rule", rule_id, {"name": body["name"]})
    return jsonify({"ok": True})


@alert_bp.route("/alerts/rules/<int:rule_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_rule(rule_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "alert_rules", rule_id):
        return jsonify({"error": "规则不存在"}), 404
    dbm.delete_record(db, "alert_rules", rule_id)
    audit("delete_alert_rule", "alert_rule", rule_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/rules/<int:rule_id>/toggle", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def toggle_rule(rule_id):
    db = current_app.config["DB_PATH"]
    r = dbm.get_record(db, "alert_rules", rule_id)
    if not r:
        return jsonify({"error": "规则不存在"}), 404
    dbm.update_record(db, "alert_rules", rule_id,
                      {"enabled": 0 if r["enabled"] else 1})
    return jsonify({"ok": True, "enabled": 0 if r["enabled"] else 1})


# ---------- 静默窗口 CRUD ----------

@alert_bp.route("/alerts/silences")
@require_auth
def list_silences():
    return jsonify(dbm.list_records(current_app.config["DB_PATH"], "silence_windows",
                                    "", (), "id"))


@alert_bp.route("/alerts/silences", methods=["POST"])
@require_auth
@require_role("admin", "operator")
def create_silence():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "窗口名称必填"}), 400
    wtype = body.get("type", "one_time")
    if wtype not in ("one_time", "recurring"):
        return jsonify({"error": "type 非法"}), 400
    start_at = body.get("start_at", "")
    end_at = body.get("end_at", "")
    if wtype == "one_time":
        try:
            float(start_at); float(end_at)
        except (TypeError, ValueError):
            return jsonify({"error": "一次性窗口需 epoch 秒 start_at/end_at"}), 400
    now = int(time.time())
    wid = dbm.create_record(current_app.config["DB_PATH"], "silence_windows", dict(
        name=name, type=wtype, start_at=str(start_at), end_at=str(end_at),
        weekdays=body.get("weekdays") or [],
        scope=body.get("scope") or {"rules": [], "envs": [], "servers": []},
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", ""), created_at=now))
    audit("create_silence", "silence", wid, {"name": name})
    return jsonify({"id": wid})


@alert_bp.route("/alerts/silences/<int:silence_id>", methods=["PUT"])
@require_auth
@require_role("admin", "operator")
def update_silence(silence_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "silence_windows", silence_id):
        return jsonify({"error": "窗口不存在"}), 404
    body = request.get_json(silent=True) or {}
    dbm.update_record(db, "silence_windows", silence_id, dict(
        name=(body.get("name") or "").strip() or "窗口",
        type=body.get("type", "one_time"),
        start_at=str(body.get("start_at", "0")),
        end_at=str(body.get("end_at", "0")),
        weekdays=body.get("weekdays") or [],
        scope=body.get("scope") or {"rules": [], "envs": [], "servers": []},
        enabled=1 if body.get("enabled", True) else 0,
        remark=body.get("remark", "")))
    audit("update_silence", "silence", silence_id)
    return jsonify({"ok": True})


@alert_bp.route("/alerts/silences/<int:silence_id>", methods=["DELETE"])
@require_auth
@require_role("admin", "operator")
def delete_silence(silence_id):
    db = current_app.config["DB_PATH"]
    if not dbm.get_record(db, "silence_windows", silence_id):
        return jsonify({"error": "窗口不存在"}), 404
    dbm.delete_record(db, "silence_windows", silence_id)
    audit("delete_silence", "silence", silence_id)
    return jsonify({"ok": True})


# ---------- 渠道配置 ----------

_MASK_KEYS = ("password", "secret", "access_key")


@alert_bp.route("/alerts/channels", methods=["GET"])
@require_auth
@require_role("admin")
def get_channels():
    db = current_app.config["DB_PATH"]
    out = {}
    for c in CHANNELS:
        rows = dbm.list_records(db, "platform_settings", "key=?", (f"channel.{c}",))
        cfg = {}
        if rows and rows[0]["value"]:
            try:
                cfg = json.loads(rows[0]["value"]) or {}
            except ValueError:
                cfg = {}
        masked = dict(cfg)
        for k in _MASK_KEYS:
            if masked.get(k):
                masked[k] = "******"
        out[c] = masked
    return jsonify(out)


@alert_bp.route("/alerts/channels", methods=["PUT"])
@require_auth
@require_role("admin")
def put_channels():
    db = current_app.config["DB_PATH"]
    body = request.get_json(silent=True) or {}
    for channel, cfg in body.items():
        if channel not in CHANNELS or not isinstance(cfg, dict):
            continue
        rows = dbm.list_records(db, "platform_settings", "key=?", (f"channel.{channel}",))
        old = {}
        if rows and rows[0]["value"]:
            try:
                old = json.loads(rows[0]["value"]) or {}
            except ValueError:
                old = {}
        # 打码字段未修改时保留原值
        for k in _MASK_KEYS:
            if cfg.get(k) in (None, "", "******") and old.get(k):
                cfg[k] = old[k]
        now = int(time.time())
        value = json.dumps(cfg, ensure_ascii=False)
        if rows:
            dbm.update_record(db, "platform_settings", rows[0]["id"],
                              {"value": value, "updated_at": now})
        else:
            dbm.create_record(db, "platform_settings",
                              {"key": f"channel.{channel}", "value": value,
                               "updated_at": now})
    audit("update_alert_channels", "config", 0, {"channels": list(body.keys())})
    return jsonify({"ok": True})
```

- [ ] **Step 2: 注册蓝图**

在 `webapp/__init__.py` 的 `create_app()` 中，现有蓝图注册之后追加：

```python
    from .routes.alert_routes import alert_bp
    app.register_blueprint(alert_bp, url_prefix="/api")
```

- [ ] **Step 3: 冒烟验证 API 可加载**

Run: `python -c "from webapp import create_app; app = create_app(); print(len(app.url_map._rules))"`
Expected: 输出路由数量（比改造前多 12+ 条），无 ImportError/500

- [ ] **Step 4: 提交**

```bash
git add webapp/routes/alert_routes.py webapp/__init__.py
git commit -m "feat: 告警 API（列表/ack/resolve/统计/规则/静默/渠道）"
```

---

### Task 5: app.py 启动告警引擎线程

**Files:**
- Modify: `webapp/app.py`

- [ ] **Step 1: 修改 main() 启动引擎**

在 `webapp/app.py` 的 `main()` 中，`init_monitor(app)` 之后追加：

```python
        from webapp.alert_engine import init_engine
        init_engine(app)
        print(f"[alert] 告警引擎已启动，评估周期 {args.monitor_interval}s")
```

改造后 `main()` 相关片段：

```python
    if args.monitor_interval > 0:
        from webapp.monitor import init_monitor
        app.config["MONITOR_INTERVAL"] = args.monitor_interval
        init_monitor(app)
        from webapp.alert_engine import init_engine
        init_engine(app)
        print(f"[monitor] 探测调度已启动，周期 {args.monitor_interval}s")
        print(f"[alert] 告警引擎已启动，评估周期 {args.monitor_interval}s")
```

说明：`init_engine` 内部检查 monitor 实例，monitor 未启动（`--monitor-interval 0`）时引擎自动不启动。

- [ ] **Step 2: 启动冒烟验证**

Run: `python webapp/app.py --monitor-interval 30`（后台运行 15 秒后停止）
Expected: 日志出现 `[monitor] 探测调度已启动` 与 `[alert] 告警引擎已启动`，无异常堆栈

- [ ] **Step 3: 验证种子规则已导入**

Run: `python -c "from webapp import db as dbm; print([r['name'] for r in dbm.list_records('data/platform.db', 'alert_rules')])"`
Expected: 输出 5 条内置规则名（CPU/磁盘/服务下线/任务失败/日志错误关键词）

- [ ] **Step 4: 提交**

```bash
git add webapp/app.py
git commit -m "feat: 启动告警引擎调度线程"
```
