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


def _payload(settings: dict, messages: list, tools: list,
             tool_choice: str = None, temperature: float = None,
             extra: dict = None, stream: bool = False) -> dict:
    """组装请求体（chat/stream_chat 共用）"""
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
    if stream:
        payload["stream"] = True
    return {k: v for k, v in payload.items() if v is not None}


def _http_error(resp) -> LlmApiError:
    """把非 200 响应转为带状态的业务异常（chat/stream_chat 共用）"""
    detail = ""
    try:
        err = resp.json()
        detail = err.get("error", {}).get("message", "") or str(err)[:200]
    except (ValueError, AttributeError):
        detail = resp.text[:200]
    if resp.status_code in (401, 403):
        return LlmApiError(f"LLM 网关鉴权失败({resp.status_code}): {detail}",
                           resp.status_code)
    if resp.status_code == 429:
        return LlmApiError(f"LLM 网关限流(429): {detail}", resp.status_code)
    return LlmApiError(f"LLM 网关错误({resp.status_code}): {detail}",
                       resp.status_code)


def _settings(db_path: str = None) -> dict:
    s = agent_config.get_llm_settings(db_path)
    if not s["base_url"] or not s["api_key"]:
        raise LlmConfigError(
            "LLM 网关未配置：请设置 AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY "
            "环境变量，或在平台设置中配置 agent_llm_base_url / agent_llm_api_key")
    return s


def _headers(settings: dict) -> dict:
    return {
        "Authorization": "Bearer " + settings["api_key"],
        "Content-Type": "application/json",
    }


def _normalize_message(msg: dict) -> dict:
    """统一 choices[0].message 形态：content 可为 None；tool_calls 缺省空列表"""
    return {
        "role": "assistant",
        "content": msg.get("content"),
        "tool_calls": msg.get("tool_calls") or [],
    }


def chat(db_path: str = None, messages: list = None, tools: list = None,
         tool_choice: str = None, temperature: float = None,
         timeout: int = None, extra: dict = None) -> dict:
    """一次对话补全；返回 choices[0].message（含 content/tool_calls）

    tools: [{"type":"function","function":{name,description,parameters}}]
    调用方负责把返回的 message 追加到会话，再传入 tool 结果重试。
    """
    settings = _settings(db_path)
    body = _payload(settings, messages, tools, tool_choice, temperature, extra)

    try:
        resp = requests.post(
            _endpoint(settings["base_url"]),
            json=body,
            headers=_headers(settings),
            timeout=timeout or settings["timeout"],
        )
    except requests.RequestException as e:
        raise LlmHttpError(f"LLM 网关请求失败: {e}") from e

    if resp.status_code != 200:
        raise _http_error(resp)

    try:
        data = resp.json()
        return _normalize_message(data["choices"][0]["message"])
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LlmApiError(f"LLM 网关响应格式非法: {e}") from e


def stream_chat(db_path: str = None, messages: list = None, tools: list = None,
                tool_choice: str = None, temperature: float = None,
                timeout: int = None, extra: dict = None):
    """流式对话补全：yield 事件 dict

    事件（delta 可任意多次，message 恒为最后一个）：
        {"type": "delta",   "text": "..."}               正文增量（逐段输出）
        {"type": "message", "message": {...}}           完整 assistant message

    兼容性：网关忽略 stream 参数直接回完整 JSON 时自动降级——不产生 delta，
    仅 yield 一个 message 事件；异常语义与 chat() 完全一致（Llm*Error）。
    工具调用的流式分片在内部拼装，无需调用方处理。
    """
    settings = _settings(db_path)
    body = _payload(settings, messages, tools, tool_choice, temperature, extra,
                    stream=True)
    try:
        resp = requests.post(
            _endpoint(settings["base_url"]),
            json=body,
            headers=_headers(settings),
            stream=True,
            timeout=timeout or settings["timeout"],
        )
    except requests.RequestException as e:
        raise LlmHttpError(f"LLM 网关请求失败: {e}") from e

    if resp.status_code != 200:
        raise _http_error(resp)

    content_parts = []
    slots = {}     # tool_call index -> 分片拼装缓冲
    order = []     # 首见顺序，保证最终 tool_calls 稳定有序
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("text/event-stream"):
            # 网关未按流式响应：按普通 JSON 一次性解析（自动降级）
            data = resp.json()
            yield {"type": "message",
                   "message": _normalize_message(
                       data["choices"][0]["message"])}
            return
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue  # 心跳等脏行忽略
            try:
                choice = (obj.get("choices") or [])[0]
            except IndexError:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            text = delta.get("content")
            if text:
                content_parts.append(text)
                yield {"type": "delta", "text": text}
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                if idx not in slots:
                    slots[idx] = {"id": "", "name": "", "arguments": ""}
                    order.append(idx)
                fn = tc.get("function") or {}
                slot = slots[idx]
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if fn.get("name"):  # 个别实现把 name 也分片，用累加保险
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
    except requests.RequestException as e:
        # 流中途断连：已发出的 delta 无法撤回，向上抛错由编排层中断
        raise LlmHttpError(f"LLM 网关流式响应中断: {e}") from e
    finally:
        resp.close()

    tool_calls = []
    for idx in order:
        slot = slots[idx]
        tool_calls.append({
            "id": slot["id"] or f"call_{idx}",
            "type": "function",
            "function": {"name": slot["name"],
                         "arguments": slot["arguments"] or "{}"},
        })
    yield {"type": "message", "message": {
        "role": "assistant",
        "content": "".join(content_parts) or None,
        "tool_calls": tool_calls}}



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
