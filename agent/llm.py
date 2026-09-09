"""LLM 网关：OpenAI 兼容 /chat/completions + function calling

零新增依赖：直接用 requests 与 OpenAI 兼容端点通信（DeepSeek/Qwen/Kimi/
Doubao/GLM/OpenAI 等主流厂商均支持该协议）。工具统一走 message.tool_calls
标准格式，兼容面最大。

错误分级：
    LlmConfigError  网关未配置/模型未配置
    LlmHttpError    网络/HTTP 失败（含超时）
    LlmApiError     服务端业务错误（鉴权/限流/上下文超长等，附状态码）
"""
import json

import requests

from . import config as agent_config


class LlmError(Exception):
    """网关错误基类"""


class LlmConfigError(LlmError):
    """网关配置缺失"""


class LlmHttpError(LlmError):
    """网络层失败"""


class LlmApiError(LlmError):
    """服务端返回业务错误"""
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _endpoint(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def chat(db_path: str = None, messages: list = None, tools: list = None,
         tool_choice: str = None, temperature: float = None,
         timeout: int = None, extra: dict = None) -> dict:
    """一次对话补全；返回 choices[0].message（含 content/tool_calls）

    tools: [{"type":"function","function":{name,description,parameters}}]
    调用方负责把返回的 message 追加到会话，再传入 tool 结果重试。
    """
    settings = agent_config.get_llm_settings(db_path)
    if not settings["base_url"] or not settings["api_key"]:
        raise LlmConfigError(
            "LLM 网关未配置：请设置 AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY "
            "环境变量，或在平台设置中配置 agent_llm_base_url / agent_llm_api_key")
    payload = {
        "model": settings["model"] or None,
        "messages": messages or [],
        "temperature": temperature if temperature is not None
        else settings["temperature"],
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if extra:
        payload.update(extra)
    body = {k: v for k, v in payload.items() if v is not None}

    try:
        resp = requests.post(
            _endpoint(settings["base_url"]),
            json=body,
            headers={
                "Authorization": "Bearer " + settings["api_key"],
                "Content-Type": "application/json",
            },
            timeout=timeout or settings["timeout"],
        )
    except requests.RequestException as e:
        raise LlmHttpError(f"LLM 网关请求失败: {e}") from e

    if resp.status_code != 200:
        detail = ""
        try:
            err = resp.json()
            detail = err.get("error", {}).get("message", "") or str(err)[:200]
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        if resp.status_code in (401, 403):
            raise LlmApiError(f"LLM 网关鉴权失败({resp.status_code}): {detail}",
                              resp.status_code)
        if resp.status_code == 429:
            raise LlmApiError(f"LLM 网关限流(429): {detail}", resp.status_code)
        raise LlmApiError(f"LLM 网关错误({resp.status_code}): {detail}",
                          resp.status_code)

    try:
        data = resp.json()
        msg = data["choices"][0]["message"]
        # 统一 message 形态：content 可为 None；tool_calls 缺省为空列表
        return {
            "role": "assistant",
            "content": msg.get("content"),
            "tool_calls": msg.get("tool_calls") or [],
        }
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LlmApiError(f"LLM 网关响应格式非法: {e}") from e


def parse_tool_calls(message: dict) -> list:
    """把 assistant message 的 tool_calls 归一化为 [(tool_name, args_dict), ...]

    兼容部分实现把 arguments 作为 JSON 字符串/对象两种形态；解析失败抛出
    LlmApiError，由调用方转成 tool error 结果反馈模型修正。
    """
    out = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        raw = fn.get("arguments", "{}")
        if isinstance(raw, str):
            try:
                args = json.loads(raw) if raw.strip() else {}
            except ValueError as e:
                raise LlmApiError(
                    f"工具 {name} 参数非法 JSON: {e}（原文: {raw[:200]}）") from e
        elif isinstance(raw, dict):
            args = raw
        else:
            args = {}
        out.append((name, args if isinstance(args, dict) else {}))
    return out


def simple_chat(db_path: str = None, system: str = "",
                user: str = "", max_retry: int = 1) -> str:
    """无工具对话（advisor 润色/摘要用）；失败抛 LlmError 由调用方降级"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    last_err = None
    for _ in range(max_retry):
        try:
            msg = chat(db_path, messages)
            return (msg.get("content") or "").strip()
        except LlmError as e:
            last_err = e
    raise last_err
