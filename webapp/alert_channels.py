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
import sys
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


# 渠道 -> 发送函数名（调用时经模块命名空间解析，便于测试 patch 与热更新）
_CHANNEL_FUNCS = {"email": "_send_email", "wecom": "_send_wecom",
                  "webhook": "_send_webhook", "sms_aliyun": "_send_aliyun_sms",
                  "feishu": "_send_feishu", "dingtalk": "_send_dingtalk"}

# 各渠道必需配置键（缺失即快速失败——重试对未配置无意义，避免每次告警空耗 3s）
_REQUIRED = {
    "email": ("host", "to_addrs"),
    "wecom": ("webhook",),
    "webhook": ("url",),
    "sms_aliyun": ("access_key", "template"),
    "feishu": ("webhook",),
    "dingtalk": ("webhook",),
}

# 导入期校验：渠道函数名必须在模块中可解析，重命名时静默失败提前暴露
_MISSING = [n for n in _CHANNEL_FUNCS.values()
            if not callable(getattr(sys.modules[__name__], n, None))]
if _MISSING:
    raise ImportError(f"渠道发送函数未定义: {_MISSING}")


def send_alert(db_path: str, alert: dict, channels: list) -> list:
    """按渠道发送告警通知（失败重试 2 次指数退避），结果写 alert_events。

    返回 [(channel, ok, detail), ...]；任何异常不外抛（告警引擎不被通知阻塞）。
    """
    text = format_alert_text(alert)
    results = []
    now = int(time.time())
    for channel in channels:
        func_name = _CHANNEL_FUNCS.get(channel)
        if not func_name:
            continue
        ok, detail = False, ""
        try:
            cfg = get_channel_config(db_path, channel)
            missing = [k for k in _REQUIRED.get(channel, ()) if not cfg.get(k)]
            if missing:
                # 渠道未配置：快速失败不重试，避免阻塞告警引擎评估线程
                detail = "渠道未配置: " + ",".join(missing)
            else:
                for attempt in range(RETRIES + 1):
                    try:
                        getattr(sys.modules[__name__], func_name)(cfg, text, alert)
                        ok, detail = True, "ok"
                        break
                    except Exception as e:
                        detail = str(e)[:200]
                        if attempt < RETRIES:
                            time.sleep(2 ** attempt)
            dbm.create_record(db_path, "alert_events", dict(
                alert_id=alert["id"], channel=channel, target="", payload="",
                ok=1 if ok else 0, detail=detail, created_at=now))
        except Exception as e:
            # 配置解析/事件落库失败同样不外抛（告警引擎不被通知阻塞）
            detail = str(e)[:200]
        results.append((channel, ok, detail))
    return results
