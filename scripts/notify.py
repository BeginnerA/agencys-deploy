"""发布消息通知：支持飞书 / 钉钉群机器人

配置（deploy.yaml 的 notify 节）：
    notify:
      enabled: true
      channels: ["feishu"]          # feishu / dingtalk，可多个
      feishu_webhook: ""            # 可用环境变量 NOTIFY_FEISHU_WEBHOOK 覆盖
      dingtalk_webhook: ""          # 可用环境变量 NOTIFY_DINGTALK_WEBHOOK 覆盖
      dingtalk_secret: ""           # 钉钉加签密钥（可选），可用环境变量 NOTIFY_DINGTALK_SECRET 覆盖
      on_events: ["start", "success", "failed", "rollback"]

通知失败仅告警，不阻断发布流程（send_notify 内部捕获所有异常）。
"""
import os
import time
import hmac
import hashlib
import base64
import argparse
import urllib.parse

import requests

from common import env_or_config, log

EVENTS = ("start", "success", "failed", "rollback")
EMOJIS = {"start": "🚀", "success": "✅", "failed": "❌", "rollback": "🔙"}


def get_notify_config(config: dict) -> dict:
    cfg = config.get("notify") or {}
    return {
        "enabled": cfg.get("enabled", False),
        "channels": cfg.get("channels", ["feishu"]),
        "feishu_webhook": env_or_config(cfg.get("feishu_webhook", ""), "NOTIFY_FEISHU_WEBHOOK"),
        "dingtalk_webhook": env_or_config(cfg.get("dingtalk_webhook", ""), "NOTIFY_DINGTALK_WEBHOOK"),
        "dingtalk_secret": env_or_config(cfg.get("dingtalk_secret", ""), "NOTIFY_DINGTALK_SECRET"),
        "on_events": cfg.get("on_events", ["start", "success", "failed", "rollback"]),
    }


def format_message(event: str, **kw) -> str:
    """组装通知文本；event ∈ start/success/failed/rollback，kw 含 project/version/env/host/detail/elapsed"""
    emoji = EMOJIS.get(event, "")
    lines = [f"{emoji}【{event.upper()}】{kw.get('project', '')} 发布"]
    if kw.get("version"):
        lines.append(f"版本: {kw['version']}")
    if kw.get("previous_version"):
        lines.append(f"回滚版本: {kw['previous_version']}")
    if kw.get("env"):
        lines.append(f"环境: {kw['env']}")
    if kw.get("host"):
        lines.append(f"服务器: {kw['host']}")
    if kw.get("elapsed"):
        lines.append(f"耗时: {kw['elapsed']}")
    if kw.get("detail"):
        lines.append(f"详情: {kw['detail']}")
    return "\n".join(lines)


def _feishu_text(webhook: str, text: str) -> bool:
    resp = requests.post(webhook, json={"msg_type": "text", "content": {"text": text}}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书返回错误: {data}")
    return True


def _dingtalk_sign(secret: str):
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    return timestamp, sign


def _dingtalk_text(webhook: str, secret: str, text: str) -> bool:
    params = {}
    if secret:
        timestamp, sign = _dingtalk_sign(secret)
        params = {"timestamp": timestamp, "sign": sign}
    resp = requests.post(webhook, params=params, json={"msgtype": "text", "text": {"content": text}}, timeout=10)
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"钉钉返回错误: {data}")
    return True


def send_notify(config: dict, event: str, **kw) -> None:
    """发送通知；任何失败仅记录日志，不抛异常（不阻断发布流程）"""
    try:
        cfg = get_notify_config(config)
        if not cfg["enabled"] or event not in cfg["on_events"]:
            return
        text = format_message(event, **kw)
        for channel in cfg["channels"]:
            if channel == "feishu" and cfg["feishu_webhook"]:
                _feishu_text(cfg["feishu_webhook"], text)
                log(f"通知已发送（飞书）: {event}")
            elif channel == "dingtalk" and cfg["dingtalk_webhook"]:
                _dingtalk_text(cfg["dingtalk_webhook"], cfg["dingtalk_secret"], text)
                log(f"通知已发送（钉钉）: {event}")
            else:
                log(f"跳过通知渠道 {channel}: 未配置 webhook")
    except Exception as e:
        log(f"通知发送失败（不影响发布）: {e}")


def main():
    """CLI 入口：供 Jenkins 等外部调用 python scripts/notify.py --event success --project xxx ..."""
    parser = argparse.ArgumentParser(description="发送发布通知（飞书/钉钉）")
    parser.add_argument("--event", required=True, choices=EVENTS, help="事件: start/success/failed/rollback")
    parser.add_argument("--project", default="", help="项目/流水线名称")
    parser.add_argument("--version", default="", help="版本号")
    parser.add_argument("--previous-version", default="", help="回滚版本号")
    parser.add_argument("--env", default="", help="环境")
    parser.add_argument("--host", default="", help="服务器")
    parser.add_argument("--elapsed", default="", help="耗时")
    parser.add_argument("--detail", default="", help="详情")
    args = parser.parse_args()
    if args.env:
        os.environ["PLATFORM_DEPLOY_ENV"] = args.env
    from common import load_config
    config = load_config(env=args.env)
    send_notify(config, args.event, project=args.project, version=args.version,
                previous_version=args.previous_version, env=args.env,
                host=args.host, elapsed=args.elapsed, detail=args.detail)


if __name__ == "__main__":
    main()
