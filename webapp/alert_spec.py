"""告警规则、作用域与静默窗口校验的唯一口径：Web 路由与 Agent 工具共用（P2-6 质量门禁）

历史上同一套校验存在两份镜像（`routes/alert_routes` 与 `agent/tools` 各写了一遍
规则校验和静默窗口校验；窗口 scope 校验也有两份）。镜像实现会漂移：
一侧调整枚举或阈值，另一侧静默保持旧口径，同一份数据经不同入口结论不同。
本模块是唯一的口径来源；入口只保留各自专属的检查
（如 Agent 侧的 scope id 存在性 —— 防对话幻觉引用不存在的对象）。

新增告警渠道请改 `webapp/alert_channels.py` 的 CHANNELS，本模块直接复用。
"""
import re

from .alert_channels import CHANNELS

SEVERITIES = ("P0", "P1", "P2", "P3")
COMPARATORS = ("<", "<=", ">", ">=")
METRIC_TYPES = ("metric", "status", "log_keyword", "custom_monitor")
SCOPE_TYPES = ("all", "project", "env", "server", "service")
SILENCE_TYPES = ("one_time", "recurring")
SILENCE_SCOPE_KEYS = ("rules", "envs", "servers")

# 周期静默的时间格式（HH:MM，0-23 时 / 0-59 分）
HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

# metric_type → 允许的 metric 取值（log_keyword 例外：metric 存逗号分隔关键词）
RULE_METRICS = {
    "metric": ("cpu", "mem", "disk", "load"),
    "status": ("service_down", "task_failed"),
}

# 关键词单条长度上限：拦住"把整行堆栈当关键词"的误配。采集端是纯子串匹配、
# 本身不设长度，这里只是校验侧的自设约束（长关键词只会拖慢每轮扫描）
KEYWORD_MAX_LEN = 100
# 指标型规则的持续时长上限：不得超过告警引擎的滑动窗口。
# 窗口 = 引擎缓存的 40 轮 × 探测周期（`DASHBOARD_MONITOR_INTERVAL`，默认 30s ≈ 20 分钟），
# 不是恒定 20 分钟；此处按默认周期取保守值，超过即永不触发。
METRIC_DURATION_MAX = 1200


def validate_scope(scope) -> str:
    """校验 scope 结构，返回错误信息或空字符串（发布窗口与告警规则共用）"""
    if not isinstance(scope, dict):
        return "scope 格式错误"
    if scope.get("type") not in SCOPE_TYPES:
        return "scope type 非法（仅支持 %s）" % "/".join(SCOPE_TYPES)
    if not isinstance(scope.get("ids"), list):
        return "scope.ids 需为列表"
    if "env_ids" in scope and not isinstance(scope["env_ids"], list):
        return "scope.env_ids 需为列表"
    return ""


def validate_silence(body, partial=False) -> str:
    """静默窗口校验；返回错误信息，空串=通过。

    one_time 用 epoch 秒、recurring 用 HH:MM（引擎按当日墙钟匹配），两种类型的
    时间格式不能混用——这是历史上两份镜像实现各自表述、最容易漂移的地方。
    partial=True 只校验请求体中出现的字段（局部更新）。
    """
    def has(k):
        return True if not partial else k in body

    if has("name") and not (body.get("name") or "").strip():
        return "窗口名称必填"
    if has("type") and body.get("type") not in SILENCE_TYPES:
        return "type 必须为 %s" % "/".join(SILENCE_TYPES)
    wtype = body.get("type")
    if has("start_at") or has("end_at") or not partial:
        start_at = str(body.get("start_at", ""))
        end_at = str(body.get("end_at", ""))
        if wtype == "one_time":
            try:
                float(start_at)
                float(end_at)
            except (TypeError, ValueError):
                return "一次性窗口需 epoch 秒 start_at/end_at"
        elif wtype == "recurring":
            if not (HHMM.match(start_at) and HHMM.match(end_at)):
                return "周期窗口需 HH:MM 格式 start_at/end_at"
    if has("scope"):
        scope = body.get("scope")
        if not isinstance(scope, dict) or not all(
                isinstance(scope.get(k), list) for k in SILENCE_SCOPE_KEYS):
            return "scope 需为 {%s} 列表结构" % ",".join(SILENCE_SCOPE_KEYS)
    weekdays = body.get("weekdays")
    if "weekdays" in body and weekdays is not None and not isinstance(weekdays, list):
        return "weekdays 需为列表"
    return ""


def _validate_keywords(raw: str, what: str) -> str:
    """逗号分隔关键词：非空 + 单条长度上限"""
    keywords = [k.strip() for k in (raw or "").split(",") if k.strip()]
    if what == "日志关键词" and not keywords:
        return "日志关键词不能为空"
    too_long = [k for k in keywords if len(k) > KEYWORD_MAX_LEN]
    if too_long:
        return f"单个{what}长度不能超过 {KEYWORD_MAX_LEN}"
    return ""


def validate_alert_rule(body, partial=False) -> str:
    """告警规则校验；返回错误信息，空串=通过。

    partial=True 仅校验请求体中出现的字段（局部更新前的形态检查）；
    调用方若需防止跨字段组合失配，应在合并 old+body 后不带 partial 再校验一次。
    """
    def has(k):
        return True if not partial else k in body

    if has("name") and not (body.get("name") or "").strip():
        return "规则名称必填"
    if has("metric_type") and body.get("metric_type") not in METRIC_TYPES:
        return "metric_type 非法（仅支持 %s）" % "/".join(METRIC_TYPES)
    mt = body.get("metric_type")
    if has("metric"):
        if mt == "metric" and body.get("metric") not in RULE_METRICS["metric"]:
            return "metric 型规则的 metric 必须为 %s" % "/".join(RULE_METRICS["metric"])
        if mt == "status" and body.get("metric") not in RULE_METRICS["status"]:
            return "status 型规则的 metric 必须为 %s" % "/".join(RULE_METRICS["status"])
        if mt == "log_keyword":
            err = _validate_keywords(body.get("metric") or "", "日志关键词")
            if err:
                return err
    if has("exclude_keyword"):
        err = _validate_keywords(body.get("exclude_keyword") or "", "排除关键词")
        if err:
            return err
    if has("severity") and body.get("severity") not in SEVERITIES:
        return "severity 必须为 %s" % "/".join(SEVERITIES)
    if has("compare") and body.get("compare") not in COMPARATORS:
        return "compare 必须为 %s" % " ".join(COMPARATORS)
    if has("threshold"):
        try:
            float(body.get("threshold", 0))
        except (TypeError, ValueError):
            return "threshold 必须为数字"
    if has("duration_seconds"):
        try:
            duration = int(body.get("duration_seconds", 0))
        except (TypeError, ValueError):
            return "duration_seconds 非法"
        if mt == "metric" and duration > METRIC_DURATION_MAX:
            return (f"duration_seconds 不能超过 {METRIC_DURATION_MAX} 秒"
                    "（不得超出告警滑动窗口：40 轮 × 探测周期，默认周期下约 20 分钟）")
    if has("escalation_interval"):
        try:
            int(body.get("escalation_interval", 30))
        except (TypeError, ValueError):
            return "escalation_interval 必须为数字"
    if has("scope"):
        scope = body.get("scope") or {}
        err = validate_scope(scope)
        if err:
            return err
        # 跨字段约束：指标按主机采集（无服务维度），状态/日志按服务（无单机维度）
        if mt == "metric" and scope.get("type") == "service":
            return "指标规则不能选择服务作用域"
        if mt in ("status", "log_keyword") and scope.get("type") == "server":
            return "状态/日志规则不能选择服务器作用域"
    if has("channels"):
        channels = body.get("channels") or []
        if not isinstance(channels, list):
            return "channels 需为列表"
        if channels and not set(channels) <= set(CHANNELS):
            return "channels 包含非法渠道（仅支持 %s）" % "/".join(CHANNELS)
    return ""
