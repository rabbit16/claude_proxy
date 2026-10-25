import json
import logging
import re
import time
from typing import Any, Dict, Optional

from urllib.parse import parse_qs

from settings.macro import (
    CACHE_INPUT_INCLUDES_CACHE,
    IMAGE_MODEL_MONEY_DICT,
    TEAMO_FAST_MODEL_MONEY_DICT,
    TEAMO_MODEL_ALIASES,
    TEAMO_MODEL_MONEY_DICT,
    TEAMO_UPSTREAM,
    VENDOR_UPSTREAM,
    YUNXIN_VENDOR_MODELS,
)

common_util_log = logging.getLogger("common_util")


def is_client_disconnect(exc: BaseException) -> bool:
    """下游页面停止/断连。CancelledError 也算，因为 StreamingResponse 会取消生成器。"""
    if isinstance(exc, GeneratorExit):
        return True
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    return (
        name in {"cancellederror", "clientdisconnect", "clientdisconnected"}
        or "disconnect" in name
        or "connectionreset" in name
        or "broken pipe" in text
        or "client disconnected" in text
    )

_YUNXIN_VENDOR_SET = frozenset(YUNXIN_VENDOR_MODELS)


def _runtime_flags() -> tuple[bool, bool]:
    """(teamo, yunxin)；优先读 DB 热切换后的运行时供应商。"""
    try:
        from utils.vendor_catalog import get_runtime_upstream
        rt = get_runtime_upstream()
        return bool(rt.get("teamo")), bool(rt.get("yunxin"))
    except Exception:
        return bool(TEAMO_UPSTREAM), bool(VENDOR_UPSTREAM)


# 标准名 → 号商名；无 -hc 的型号原样透传。仅 yunxin 时启用。
UPSTREAM_MODEL_MAP = {
    (name[: -len("-hc")] if name.endswith("-hc") else name): name
    for name in YUNXIN_VENDOR_MODELS
}
# 已是号商名的也允许直传
UPSTREAM_MODEL_MAP.update({name: name for name in YUNXIN_VENDOR_MODELS})

ANTHROPIC_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
}

PATH_ALIASES = {
    "/responses": "/v1/responses",
    "/chat/completions": "/v1/chat/completions",
    "/embeddings": "/v1/embeddings",
    "/models": "/v1/models",
    "/messages": "/v1/messages",
    "/images/generations": "/v1/images/generations",
    "/images/edits": "/v1/images/edits",
    "/images/variations": "/v1/images/variations",
}

IMAGE_PATHS = {
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/images/variations",
}

AUDIO_PATH_PREFIX = "/v1/audio/"
GEMINI_NATIVE_PATH_RE = re.compile(
    r"^/v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$"
)

HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "content-encoding",
}

AUTH_FAIL_REASONS = {"not exist", "no money"}


def detect_source(path: str, headers: Optional[dict] = None) -> str:
    """识别客户端协议：anthropic / responses / openai / images / gemini。"""
    headers = headers or {}
    if is_image_path(path):
        return "images"
    if is_gemini_native_path(path):
        return "gemini"
    if path.startswith("/v1/messages") or path == "/messages":
        return "anthropic"
    if path.startswith("/v1/responses") or path == "/responses":
        return "responses"
    if path.startswith("/v1/chat/completions") or path == "/chat/completions":
        return "openai"

    ua = (headers.get("user-agent") or "").lower()
    if "claude" in ua or headers.get("anthropic-version"):
        return "anthropic"
    if "codex" in ua:
        return "responses"
    return "openai"


def normalize_upstream_path(path: str) -> str:
    return PATH_ALIASES.get(path, path)


def is_image_path(path: str) -> bool:
    normalized = normalize_upstream_path(path)
    return normalized in IMAGE_PATHS or normalized.startswith("/v1/images/")


def is_gemini_native_path(path: str) -> bool:
    return bool(GEMINI_NATIVE_PATH_RE.match(path or ""))


def extract_gemini_model_from_path(path: str) -> str:
    match = GEMINI_NATIVE_PATH_RE.match(path or "")
    return match.group(1) if match else ""


def replace_gemini_model_in_path(path: str, model: str) -> str:
    """Gemini 原生路径里的模型名改成最终要调用的模型。"""
    match = GEMINI_NATIVE_PATH_RE.match(path or "")
    name = (model or "").strip()
    if not match or not name or name == "-" or "/" in name or ":" in name:
        return path
    return f"/v1beta/models/{name}:{match.group(2)}"


def is_models_path(path: str) -> bool:
    normalized = normalize_upstream_path(path)
    return normalized == "/v1/models" or normalized.startswith("/v1/models/")


def is_embedding_path(path: str) -> bool:
    return normalize_upstream_path(path) == "/v1/embeddings"


def is_audio_path(path: str) -> bool:
    normalized = normalize_upstream_path(path)
    return normalized == "/v1/audio" or normalized.startswith(AUDIO_PATH_PREFIX)


def is_audio_chat_completion(path: str, model: str) -> bool:
    normalized_path = normalize_upstream_path(path)
    model_name = (model or "").strip().lower().rsplit("/", 1)[-1]
    if model_name.startswith("gpt-audio"):
        return True
    if normalized_path != "/v1/chat/completions":
        return False
    return model_name == "gpt-audio" or model_name.startswith("gpt-audio-")


def strip_openai_model_prefix(model: str) -> str:
    """清洗 provider 前缀，并把 Cursor 的 gr-* 别名映射为 grok-*。"""
    model = (model or "").strip()
    for prefix in ("openai/", "xai/"):
        if model.startswith(prefix):
            model = model[len(prefix) :]
            break
    if model.startswith("gr-"):
        suffix = model[3:]
        model = suffix if suffix.startswith("grok-") else f"grok-{suffix}"
    return model


def model_prefers_max_completion_tokens(model: str) -> bool:
    """官方 OpenAI 对 gpt-5/6、o 系列拒绝 max_tokens，需用 max_completion_tokens。"""
    model = strip_openai_model_prefix(model).lower()
    if not model:
        return False
    if model.startswith(("gpt-5", "gpt-6")):
        return True
    if model.startswith(("o1", "o3", "o4")):
        return True
    return False


def normalize_max_tokens_for_upstream(body: bytes, parsed_body: dict) -> tuple:
    """按上游能力规范化 max_tokens / max_completion_tokens。

    - Teamo / 号商：统一成 max_tokens（带 max_completion_tokens 易 422）
    - 本地 LiteLLM → 官方 OpenAI：gpt-5/6、o 系列改用 max_completion_tokens
    """
    if not isinstance(parsed_body, dict):
        return body, parsed_body
    model = parsed_body.get("model") or ""
    teamo, yunxin = _runtime_flags()
    updated = dict(parsed_body)
    changed = False
    has_max = updated.get("max_tokens") is not None
    has_mct = updated.get("max_completion_tokens") is not None

    if teamo or yunxin:
        if has_max and has_mct:
            updated.pop("max_completion_tokens", None)
            changed = True
        elif has_mct and not has_max:
            updated["max_tokens"] = updated.pop("max_completion_tokens")
            changed = True
    elif model_prefers_max_completion_tokens(model):
        if has_max and has_mct:
            updated.pop("max_tokens", None)
            changed = True
        elif has_max and not has_mct:
            updated["max_completion_tokens"] = updated.pop("max_tokens")
            changed = True
    if not changed:
        return body, parsed_body
    return _replace_json_request_body(body, updated)


def ensure_chat_tools_reasoning_none(body: bytes, parsed_body: dict) -> tuple:
    """官方 OpenAI：chat/completions + function tools 时，gpt-6 等必须 reasoning_effort=none。

    否则上游直接 400。Cursor 走 chat/completions 带 tools，需自动降为 none。
    号商模式不改。
    """
    if not isinstance(parsed_body, dict) or _runtime_flags()[1]:
        return body, parsed_body
    model = parsed_body.get("model") or ""
    if not model_prefers_max_completion_tokens(model):
        return body, parsed_body
    if not parsed_body.get("tools"):
        return body, parsed_body

    updated = dict(parsed_body)
    changed = False
    if updated.get("reasoning_effort") != "none":
        updated["reasoning_effort"] = "none"
        changed = True
    reasoning = updated.get("reasoning")
    if isinstance(reasoning, dict):
        if reasoning.get("effort") != "none":
            reasoning = dict(reasoning)
            reasoning["effort"] = "none"
            updated["reasoning"] = reasoning
            changed = True
    if not changed:
        return body, parsed_body
    return _replace_json_request_body(body, updated)


def is_claude_adaptive_thinking_model(model: str) -> bool:
    """Claude 5.x 起上游要求 adaptive thinking，旧的 disabled/enabled 一律 400。

    只认 claude-*-5-5 / claude-*-5.5 及以后的 5.x 段位；4.x 及更早仍是旧语义，
    必须保持原样，否则会把本来能用的请求改坏。
    """
    model = strip_openai_model_prefix(model).lower()
    if not model.startswith("claude-"):
        return False
    # 取模型名里第一个「版本段」数字；4-5 / 4.5 是 4.x（旧语义），5-5 才是新规则。
    # 只从第一个数字开始判断，避免把 claude-sonnet-4-5 里的 5 当成主版本。
    match = re.search(r"(?<![a-z0-9])(\d+)(?:[-.]\d+)?", model)
    if not match:
        return False
    try:
        major = int(match.group(1))
    except (TypeError, ValueError):
        return False
    return major >= 5


def _claude_thinking_mode(model: str) -> str:
    """返回该模型支持的最宽 thinking 模式：adaptive / between_tools。"""
    model = strip_openai_model_prefix(model).lower()
    if "sonnet" in model:
        return "between_tools"
    return "adaptive"


def normalize_anthropic_thinking_for_upstream(
    body: bytes, parsed_body: dict, *, model: str = ""
) -> tuple:
    """把老式 thinking 参数规范成上游 Claude 5.x 要求的形态。

    上游对 claude-*-5-5 只接受 thinking.type=adaptive（sonnet 还接受
    between_tools）+ output_config.effort，以下写法都会 400：
      - thinking.type=disabled
      - thinking.type=enabled / budget_tokens

    直通（bridge=0）路径不做协议转换，客户端发什么就发什么，所以必须在这里拦。
    """
    if not isinstance(parsed_body, dict) or parsed_body.get("messages") is None:
        return body, parsed_body
    target = model or parsed_body.get("model") or ""
    if not is_claude_adaptive_thinking_model(target):
        return body, parsed_body
    thinking = parsed_body.get("thinking")
    if not isinstance(thinking, dict):
        return body, parsed_body
    thinking_type = str(thinking.get("type") or "").strip().lower()
    if thinking_type in {"adaptive", "between_tools"}:
        return body, parsed_body
    updated = dict(parsed_body)
    if thinking_type == "disabled" or not thinking_type:
        # 旧 clients 用 disabled 显式关思考；新模型没有关闭开关，直接省略该字段。
        updated.pop("thinking", None)
    else:
        mode = _claude_thinking_mode(target)
        effort = str(thinking.get("effort") or "").strip().lower()
        if effort not in {"low", "medium", "high", "max"}:
            budget = thinking.get("budget_tokens")
            effort = "high" if isinstance(budget, int) and budget >= 16000 else "medium"
        updated["thinking"] = {"type": mode}
        output_config = updated.get("output_config")
        output_config = dict(output_config) if isinstance(output_config, dict) else {}
        output_config.setdefault("effort", effort)
        updated["output_config"] = output_config
    return _replace_json_request_body(body, updated)


def to_client_model_id(model: str) -> str:
    """号商模式下把 -hc 后缀对外还原成标准名；本地 LiteLLM 模式只剥 provider 前缀。"""
    model = strip_openai_model_prefix(model)
    if _runtime_flags()[1] and model.endswith("-hc") and model in _YUNXIN_VENDOR_SET:
        return model[: -len("-hc")]
    return model


def map_model_to_upstream(model: str, *, teamo: Optional[bool] = None, yunxin: Optional[bool] = None) -> str:
    """标准模型名 → 上游命名。

    号商模式：gpt-5.6-sol → gpt-5.6-sol-hc。
    本地 LiteLLM（号商 URL 未填）：不覆盖模型名，只剥 openai/ 等前缀。
    """
    model = strip_openai_model_prefix(model)
    if not model:
        return model
    if teamo is None or yunxin is None:
        runtime_teamo, runtime_yunxin = _runtime_flags()
        teamo = runtime_teamo if teamo is None else teamo
        yunxin = runtime_yunxin if yunxin is None else yunxin
    if teamo:
        return TEAMO_MODEL_ALIASES.get(model, model)
    if not yunxin:
        return model
    mapped = UPSTREAM_MODEL_MAP.get(model)
    if mapped:
        return mapped
    candidate = f"{model}-hc"
    if candidate in _YUNXIN_VENDOR_SET:
        return candidate
    return model


def rewrite_models_payload(payload: dict) -> dict:
    """上游模型列表清洗：去 openai/ 前缀；号商模式再把 -hc 还原成客户端标准名。"""
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, list):
        return payload
    teamo, _yunxin = _runtime_flags()
    seen = set()
    rewritten = []
    for item in data:
        if not isinstance(item, dict):
            rewritten.append(item)
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            rewritten.append(item)
            continue
        if model_id.strip() == "*":
            continue
        new_id = to_client_model_id(model_id)
        if teamo:
            from utils.vendor_catalog import lookup_vendor_price
            normalized_id = TEAMO_MODEL_ALIASES.get(new_id, new_id)
            if not lookup_vendor_price(normalized_id, vendor="teamo"):
                continue
        if not new_id or new_id in seen:
            continue
        seen.add(new_id)
        if new_id != model_id:
            item = dict(item)
            item["id"] = new_id
        rewritten.append(item)
    updated = dict(payload)
    updated["data"] = rewritten
    return updated


def _replace_json_request_body(body: bytes, updated: dict) -> tuple:
    stripped_body = body.lstrip() if body else b""
    if stripped_body.startswith(b"{") or stripped_body.startswith(b"["):
        try:
            return json.dumps(updated, ensure_ascii=False).encode("utf-8"), updated
        except Exception:
            return body, updated
    return body, updated


def _schema_without_pages(schema: dict) -> tuple:
    """去掉 Read 工具 schema 里的 pages。Kiro read_file 没有这个参数。"""
    if not isinstance(schema, dict):
        return schema, False
    updated = dict(schema)
    changed = False
    props = updated.get("properties")
    if isinstance(props, dict) and "pages" in props:
        props = dict(props)
        props.pop("pages", None)
        updated["properties"] = props
        changed = True
    required = updated.get("required")
    if isinstance(required, list) and "pages" in required:
        updated["required"] = [item for item in required if item != "pages"]
        changed = True
    return (updated if changed else schema), changed


def _value_without_pages(value: Any) -> tuple:
    if isinstance(value, dict):
        if "pages" not in value:
            return value, False
        updated = dict(value)
        updated.pop("pages", None)
        return updated, True
    if isinstance(value, str) and "pages" in value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value, False
        cleaned, changed = _value_without_pages(parsed)
        if not changed:
            return value, False
        return json.dumps(cleaned, ensure_ascii=False), True
    return value, False


def _strip_read_tool(tool: dict) -> tuple:
    if not isinstance(tool, dict) or tool.get("name") != "Read":
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or fn.get("name") != "Read":
            return tool, False
        updated = dict(tool)
        fn = dict(fn)
        params, changed = _schema_without_pages(fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {})
        if isinstance(fn.get("parameters"), dict) and changed:
            fn["parameters"] = params
            updated["function"] = fn
            return updated, True
        return tool, False
    updated = dict(tool)
    changed = False
    for key in ("input_schema", "parameters"):
        schema = updated.get(key)
        if not isinstance(schema, dict):
            continue
        cleaned, schema_changed = _schema_without_pages(schema)
        if schema_changed:
            updated[key] = cleaned
            changed = True
    return (updated if changed else tool), changed


def _strip_read_pages_in_messages(messages: list) -> tuple:
    if not isinstance(messages, list):
        return messages, False
    changed = False
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        item = dict(msg)
        msg_changed = False
        if item.get("type") == "function_call" and item.get("name") == "Read":
            cleaned, call_changed = _value_without_pages(item.get("arguments"))
            if call_changed:
                item["arguments"] = cleaned
                msg_changed = True
        content = item.get("content")
        if isinstance(content, list):
            blocks = []
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Read"
                ):
                    cleaned, block_changed = _value_without_pages(block.get("input"))
                    if block_changed:
                        block = dict(block)
                        block["input"] = cleaned
                        msg_changed = True
                blocks.append(block)
            if msg_changed:
                item["content"] = blocks
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            calls = []
            calls_changed = False
            for call in tool_calls:
                if not isinstance(call, dict):
                    calls.append(call)
                    continue
                fn = call.get("function")
                if isinstance(fn, dict) and fn.get("name") == "Read":
                    cleaned, call_changed = _value_without_pages(fn.get("arguments"))
                    if call_changed:
                        call = dict(call)
                        fn = dict(fn)
                        fn["arguments"] = cleaned
                        call["function"] = fn
                        calls_changed = True
                calls.append(call)
            if calls_changed:
                item["tool_calls"] = calls
                msg_changed = True
        out.append(item if msg_changed else msg)
        changed = changed or msg_changed
    return (out if changed else messages), changed


def strip_read_pages(body: bytes, parsed_body: dict) -> tuple:
    """发给会把 Claude Code 映射到 Kiro 的号商前，去掉 Read.pages。

    返回 (body, parsed_body, removed)。removed 为 True 表示请求里确实有 pages。
    """
    if not isinstance(parsed_body, dict):
        return body, parsed_body, False
    updated = dict(parsed_body)
    changed = False
    tools = updated.get("tools")
    if isinstance(tools, list):
        cleaned_tools = []
        tools_changed = False
        for tool in tools:
            cleaned, tool_changed = _strip_read_tool(tool)
            cleaned_tools.append(cleaned)
            tools_changed = tools_changed or tool_changed
        if tools_changed:
            updated["tools"] = cleaned_tools
            changed = True
    for key in ("messages", "input"):
        items = updated.get(key)
        if not isinstance(items, list):
            continue
        cleaned, items_changed = _strip_read_pages_in_messages(items)
        if items_changed:
            updated[key] = cleaned
            changed = True
    if not changed:
        return body, parsed_body, False
    new_body, new_parsed = _replace_json_request_body(body, updated)
    return new_body, new_parsed, True


def strip_openai_prefix_from_request(body: bytes, parsed_body: dict) -> tuple:
    """请求里若仍带着 openai/，剥掉后再转发。

    只改 parsed_body，body 由调用方在最后统一序列化一次；带图请求有几 MB 的
    base64，中间每改一步就重 dump 一次纯属白搬。
    """
    if not isinstance(parsed_body, dict):
        return body, parsed_body
    model = parsed_body.get("model")
    if not isinstance(model, str):
        return body, parsed_body
    stripped = strip_openai_model_prefix(model)
    if stripped == model:
        return body, parsed_body
    updated = dict(parsed_body)
    updated["model"] = stripped
    return body, updated


# 号商 chat/completions 对部分 OpenAI 新字段不兼容（实测）。
# - 仅 max_completion_tokens：上游可能一直不回包
# - 同时带 max_tokens + max_completion_tokens：直接 422
_OPENAI_CHAT_DROP_KEYS = {
    "prompt_cache_key",
    "prompt_cache_retention",
    "safety_identifier",
    "stream_options",  # Cursor 常带；部分号商 tool 流式会 400/422
    "service_tier",  # Cursor 可能带 default/auto；号商侧易限流或拒授
    "store",  # OpenAI 持久化字段；Teamo 等中转常不认
}


def _strip_schema_noise(node):
    """去掉 JSON Schema 里号商不认的字段。"""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in {"$schema", "$id", "$defs", "definitions", "strict", "additionalProperties"}:
                continue
            if key == "const":
                # 部分网关不支持 const，降成 enum
                out["enum"] = [value]
                continue
            out[key] = _strip_schema_noise(value)
        return out
    if isinstance(node, list):
        return [_strip_schema_noise(x) for x in node]
    return node


def _sanitize_tools(tools):
    if not isinstance(tools, list):
        return tools, False
    changed = False
    cleaned = []
    for tool in tools:
        if not isinstance(tool, dict):
            cleaned.append(tool)
            continue
        item = dict(tool)
        if "strict" in item:
            item.pop("strict", None)
            changed = True
        fn = item.get("function")
        if isinstance(fn, dict):
            fn2 = dict(fn)
            if "strict" in fn2:
                fn2.pop("strict", None)
                changed = True
            params = fn2.get("parameters")
            if isinstance(params, dict):
                new_params = _strip_schema_noise(params)
                if new_params != params:
                    fn2["parameters"] = new_params
                    changed = True
            item["function"] = fn2
        cleaned.append(item)
    return cleaned, changed


def _sanitize_message_content(messages):
    """清洗 messages 里号商易拒的字段（reasoning_content 可留；去掉空 tool_calls 等）。"""
    if not isinstance(messages, list):
        return messages, False
    changed = False
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        item = dict(msg)
        # OpenAI Codex / Cursor 会发 role=developer；DeepSeek 等只认 system/user/assistant/tool
        if item.get("role") == "developer":
            item["role"] = "system"
            changed = True
        # content=null 且无 tool_calls 时改成 ""
        if item.get("content") is None and not item.get("tool_calls"):
            item["content"] = ""
            changed = True
        # 去掉空字符串 name 等噪声
        if item.get("name") == "":
            item.pop("name", None)
            changed = True
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            fixed = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    fixed.append(call)
                    continue
                c2 = dict(call)
                fn = c2.get("function")
                if isinstance(fn, dict):
                    fn2 = dict(fn)
                    args = fn2.get("arguments")
                    if args is None:
                        fn2["arguments"] = "{}"
                        changed = True
                    elif isinstance(args, (dict, list)):
                        fn2["arguments"] = json.dumps(args, ensure_ascii=False)
                        changed = True
                    c2["function"] = fn2
                fixed.append(c2)
            item["tool_calls"] = fixed
        out.append(item)
    return out, changed


def sanitize_openai_chat_request(body: bytes, parsed_body: dict) -> tuple:
    """清洗 Cursor/OpenAI 聊天请求，避免号商 422/挂死。"""
    if not isinstance(parsed_body, dict):
        return body, parsed_body

    updated = dict(parsed_body)
    changed = False

    for key in list(_OPENAI_CHAT_DROP_KEYS):
        if key in updated:
            updated.pop(key, None)
            changed = True

    has_max = updated.get("max_tokens") is not None
    has_mct = updated.get("max_completion_tokens") is not None
    if has_max and has_mct:
        updated.pop("max_completion_tokens", None)
        changed = True
    elif has_mct and not has_max:
        updated["max_tokens"] = updated.pop("max_completion_tokens")
        changed = True
    elif not has_max and not has_mct and updated.get("tools"):
        # Cursor tool 调用常不带上限；号商底层 400，补一个保守上限
        updated["max_tokens"] = 16384
        changed = True

    if "tools" in updated:
        tools, tools_changed = _sanitize_tools(updated.get("tools"))
        if tools_changed:
            updated["tools"] = tools
            changed = True

    if "messages" in updated:
        messages, msg_changed = _sanitize_message_content(updated.get("messages"))
        if msg_changed:
            updated["messages"] = messages
            changed = True

    for key in list(updated.keys()):
        if updated[key] is None and key not in {"messages", "input"}:
            updated.pop(key)
            changed = True

    if not changed:
        return body, parsed_body
    return _replace_json_request_body(body, updated)


def sanitize_stateless_responses_input(body: bytes, parsed_body: dict) -> tuple:
    """直连官方 OpenAI 且非持久化（store 非 true）时，清理不能复用的历史 item。

    官方 store=false 不保存 item；客户端从别的供应商切过来时，历史里会带着
    官方侧不存在的 id（如 reasoning 的 rs_…），回传会被 404 拒绝
    （"Item with id '…' not found. Items are not persisted when store is set to false"）。
    input item 的 id 是可选的，去掉不影响推理；reasoning item 若没有
    encrypted_content 则整条丢弃，因为上游无法从纯 id 还原它。
    """
    if not isinstance(parsed_body, dict):
        return body, parsed_body
    items = parsed_body.get("input")
    if not isinstance(items, list) or parsed_body.get("store") is True:
        return body, parsed_body

    kept = []
    changed = False
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        itype = (item.get("type") or "").strip().lower()
        if itype == "item_reference":
            changed = True
            continue
        if itype == "reasoning" and not item.get("encrypted_content"):
            changed = True
            continue
        if item.get("id"):
            item = {k: v for k, v in item.items() if k != "id"}
            changed = True
        kept.append(item)
    if not changed:
        return body, parsed_body

    updated = dict(parsed_body)
    updated["input"] = kept
    return _replace_json_request_body(body, updated)


def dump_openai_request_debug(req_id: str, parsed_body: dict, body: bytes) -> None:
    """把 Cursor 大请求落盘，便于分析号商 400/422。"""
    try:
        import os
        from utils.get_log import process_log

        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"openai_req_{req_id}.json")
        # 只留结构摘要 + 截断后的原文，避免把超大上下文打爆磁盘
        summary = {
            "keys": sorted(parsed_body.keys()) if isinstance(parsed_body, dict) else [],
            "model": (parsed_body or {}).get("model"),
            "stream": (parsed_body or {}).get("stream"),
            "max_tokens": (parsed_body or {}).get("max_tokens"),
            "max_completion_tokens": (parsed_body or {}).get("max_completion_tokens"),
            "tool_names": [],
            "message_roles": [],
            "message_count": 0,
            "body_bytes": len(body or b""),
        }
        tools = (parsed_body or {}).get("tools") or []
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict):
                    fn = t.get("function") if isinstance(t.get("function"), dict) else t
                    summary["tool_names"].append((fn or {}).get("name") or t.get("type") or "?")
                    # 保留完整 tools 方便复现
        messages = (parsed_body or {}).get("messages") or []
        if isinstance(messages, list):
            summary["message_count"] = len(messages)
            for m in messages:
                if isinstance(m, dict):
                    role = m.get("role") or "?"
                    content = m.get("content")
                    clen = len(content) if isinstance(content, str) else (
                        len(json.dumps(content, ensure_ascii=False)) if content is not None else 0
                    )
                    summary["message_roles"].append(
                        {
                            "role": role,
                            "content_len": clen,
                            "has_tool_calls": bool(m.get("tool_calls")),
                            "tool_call_id": m.get("tool_call_id"),
                        }
                    )
        payload = {
            "summary": summary,
            "tools": (parsed_body or {}).get("tools"),
            # 原文可能很大，最多留 2MB
            "body_head": (body or b"")[: 2 * 1024 * 1024].decode("utf-8", "ignore"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        process_log.info("dumped openai req=%s path=%s bytes=%s msgs=%s tools=%s",
                         req_id, path, summary["body_bytes"], summary["message_count"], len(summary["tool_names"]))
    except Exception:
        from utils.get_log import process_log
        process_log.exception("dump openai request failed req=%s", req_id)


def summarize_request_keys(parsed_body: dict) -> str:
    if not isinstance(parsed_body, dict):
        return "-"
    keys = sorted(str(k) for k in parsed_body.keys())
    extras = []
    if parsed_body.get("max_tokens") is not None:
        extras.append(f"max_tokens={parsed_body.get('max_tokens')}")
    if parsed_body.get("max_completion_tokens") is not None:
        extras.append(f"max_completion_tokens={parsed_body.get('max_completion_tokens')}")
    if parsed_body.get("tools"):
        extras.append(f"tools={len(parsed_body.get('tools') or [])}")
    if parsed_body.get("stream"):
        extras.append("stream=1")
    return ",".join(keys[:40]) + (("|" + ",".join(extras)) if extras else "")


def apply_upstream_model_to_request(
    body: bytes,
    parsed_body: dict,
    *,
    teamo: Optional[bool] = None,
    yunxin: Optional[bool] = None,
    serialize: bool = True,
) -> tuple:
    """号商模式把 body.model 换成 -hc 名；本地 LiteLLM 模式不覆盖。

    parsed_body 始终保留客户端标准名，供计费/日志。
    serialize=False 时只返回改过 model 的 dict 和原 body，由调用方最后统一写盘。
    """
    if not isinstance(parsed_body, dict):
        return body, parsed_body
    if teamo is None or yunxin is None:
        runtime_teamo, runtime_yunxin = _runtime_flags()
        teamo = runtime_teamo if teamo is None else teamo
        yunxin = runtime_yunxin if yunxin is None else yunxin
    if not yunxin and not teamo:
        return body, parsed_body
    model = parsed_body.get("model")
    if not isinstance(model, str) or not model:
        return body, parsed_body
    upstream = map_model_to_upstream(model, teamo=teamo, yunxin=yunxin)
    if upstream == model:
        # body 可能仍带 openai/ 前缀，与 parsed 对齐
        return body, parsed_body
    wire = dict(parsed_body)
    wire["model"] = upstream
    if not serialize:
        return body, wire
    new_body, _ = _replace_json_request_body(body, wire)
    return new_body, parsed_body


def is_grok_46_model(model: str) -> bool:
    return strip_openai_model_prefix(model).startswith("grok-4.6")


def apply_grok_46_defaults_to_request(body: bytes, parsed_body: dict) -> tuple:
    """Grok 4.6 / gr-4.6 未指定模式时，默认 high + xAI priority（Fast，2x）。"""
    if not isinstance(parsed_body, dict):
        return body, parsed_body
    model = parsed_body.get("model")
    if not isinstance(model, str) or not is_grok_46_model(model):
        return body, parsed_body

    updated = dict(parsed_body)
    changed = False
    if not updated.get("service_tier") and not updated.get("processing_mode"):
        updated["service_tier"] = "priority"
        changed = True

    reasoning = updated.get("reasoning")
    has_effort = bool(
        (isinstance(reasoning, dict) and reasoning.get("effort"))
        or updated.get("reasoning_effort")
    )
    if not has_effort:
        if isinstance(reasoning, dict):
            reasoning = dict(reasoning)
            reasoning["effort"] = "high"
            updated["reasoning"] = reasoning
        else:
            updated["reasoning"] = {"effort": "high"}
        changed = True

    if not changed:
        return body, parsed_body
    return _replace_json_request_body(body, updated)


def include_usage_for_chat_stream(body: bytes, parsed_body: dict) -> tuple:
    """Ensure OpenAI chat streaming requests ask the upstream for final token usage."""
    if not isinstance(parsed_body, dict) or not parsed_body.get("stream"):
        return body, parsed_body
    options = parsed_body.get("stream_options")
    if isinstance(options, dict) and options.get("include_usage") is True:
        return body, parsed_body
    updated = dict(parsed_body)
    options = dict(options) if isinstance(options, dict) else {}
    options["include_usage"] = True
    updated["stream_options"] = options
    return _replace_json_request_body(body, updated)


def is_count_tokens_path(path: str) -> bool:
    """Anthropic 的 token 计数接口。只估算，不生成回复。"""
    raw = (path or "").split("?", 1)[0].rstrip("/")
    if raw == "/messages/count_tokens":
        raw = "/v1/messages/count_tokens"
    return normalize_upstream_path(raw) == "/v1/messages/count_tokens"


def is_billable_path(path: str, model: str = "") -> bool:
    if is_count_tokens_path(path):
        return False
    normalized = normalize_upstream_path(path)
    return normalized in {
        "/v1/messages",
        "/v1/responses",
        "/v1/chat/completions",
        "/v1/embeddings",
    } or normalized.startswith("/v1/messages/") or is_image_path(path) or is_audio_path(path) or is_audio_chat_completion(path, model) or is_gemini_native_path(path)


def sanitize_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}


def parse_request_payload(headers: Optional[dict], body: bytes) -> dict:
    """解析 JSON / form / multipart 文本字段，供计费和日志取 model、n、size。"""
    if not body:
        return {}
    headers = headers or {}
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value or ""
            break
    lowered = content_type.lower()
    if "multipart/form-data" in lowered:
        from utils.image_billing import parse_multipart_fields
        return parse_multipart_fields(body, content_type)
    if "application/x-www-form-urlencoded" in lowered:
        parsed = {}
        for key, values in parse_qs(body.decode(errors="ignore"), keep_blank_values=True).items():
            parsed[key] = values[-1] if values else ""
        if parsed.get("n") not in (None, ""):
            try:
                parsed["n"] = int(parsed["n"])
            except (TypeError, ValueError):
                pass
        return parsed
    try:
        loaded = json.loads(body.decode(errors="ignore"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _to_token_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _nested_cached_tokens(usage: dict) -> Optional[int]:
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            return _to_token_int(details.get("cached_tokens"))
    return None


def _deepseek_cached_tokens(usage: dict) -> Optional[int]:
    """DeepSeek 的 prompt_tokens 已含缓存命中，prompt_cache_hit_tokens 是其中子集。

    部分转发只给 prompt_cache_miss_tokens，此时用 prompt_tokens - miss 反推命中量。
    返回 None 表示响应里没有 DeepSeek 缓存字段，不能据此改变 input 口径。
    """
    hit = _to_token_int(usage.get("prompt_cache_hit_tokens"))
    if hit is not None:
        return hit
    miss = _to_token_int(usage.get("prompt_cache_miss_tokens"))
    if miss is None:
        return None
    prompt = _coalesce_token_count(usage.get("prompt_tokens"), usage.get("input_tokens"))
    if prompt is None:
        return None
    return max(0, prompt - miss)


def _gemini_cached_content_tokens(usage: dict) -> Optional[int]:
    """号商 Gemini 把缓存命中放在 billing_usage.gemini_usage_metadata。"""
    billing = usage.get("billing_usage")
    if not isinstance(billing, dict):
        return None
    meta = billing.get("gemini_usage_metadata")
    if not isinstance(meta, dict):
        return None
    if meta.get("cachedContentTokenCount") is None:
        return None
    return _to_token_int(meta.get("cachedContentTokenCount"))


def _claude_cache_creation_tokens(usage: dict) -> int:
    """号商偶发把 Anthropic cache write 拆成 5m/1h 字段。"""
    five_m = _to_token_int(usage.get("claude_cache_creation_5_m_tokens")) or 0
    one_h = _to_token_int(usage.get("claude_cache_creation_1_h_tokens")) or 0
    return five_m + one_h


def _coalesce_token_count(*values: Any) -> Optional[int]:
    """合并 Anthropic / OpenAI usage 字段。

    号商 Gemini 会同时返回 input_tokens=0、output_tokens=0 与真实的
    prompt_tokens/completion_tokens；dict.get 默认值在键存在时不会回退，
    因此不能写成 get("input_tokens", get("prompt_tokens"))。
    取所有可解析值中的最大值，避免把占位 0 当成真实用量。
    """
    parsed = [v for v in (_to_token_int(value) for value in values) if v is not None]
    return max(parsed) if parsed else None


def _normalize_usage(usage: Any) -> Dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    input_tokens = _coalesce_token_count(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _coalesce_token_count(usage.get("output_tokens"), usage.get("completion_tokens"))
    if input_tokens is None or output_tokens is None:
        return {}

    nested_cached = _nested_cached_tokens(usage)
    gemini_cached = _gemini_cached_content_tokens(usage)
    deepseek_cached = _deepseek_cached_tokens(usage)
    direct_cached = _coalesce_token_count(
        usage.get("cached_read_tokens"),
        usage.get("cache_read_tokens"),
        usage.get("cached_tokens"),
    )
    cache_read = max(
        _to_token_int(usage.get("cache_read_input_tokens")) or 0,
        direct_cached or 0,
    )
    cache_write = _coalesce_token_count(
        usage.get("cache_creation_input_tokens"),
        usage.get("cached_write_tokens"),
        usage.get("cache_write_tokens"),
    ) or 0
    cache_write = max(cache_write, _claude_cache_creation_tokens(usage))
    for candidate in (nested_cached, gemini_cached, deepseek_cached):
        if candidate is not None:
            cache_read = max(cache_read, candidate)

    cache_total = cache_read + cache_write
    # OpenAI / Gemini / DeepSeek 的 prompt_tokens 已含 cached；Anthropic 的 input 通常不含。
    # 自动口径仅在有 cache_read 时启用：不能单凭 cache_write 就把 prompt 当成含缓存。
    openai_style_cache = (
        nested_cached is not None or gemini_cached is not None or deepseek_cached is not None
    )
    if CACHE_INPUT_INCLUDES_CACHE is True:
        inclusive = cache_total > 0
    elif CACHE_INPUT_INCLUDES_CACHE is False:
        inclusive = openai_style_cache
    else:
        inclusive = openai_style_cache or (
            cache_read > 0 and cache_total > 0 and input_tokens >= cache_total
        )

    if inclusive and cache_total:
        deduct = cache_read if openai_style_cache else cache_total
        input_tokens = max(0, input_tokens - deduct)

    input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    audio_input_tokens = 0
    audio_output_tokens = 0
    if isinstance(input_details, dict):
        audio_input_tokens = _to_token_int(input_details.get("audio_tokens")) or 0
    if isinstance(output_details, dict):
        audio_output_tokens = _to_token_int(output_details.get("audio_tokens")) or 0

    normalized = {
        "input_tokens": max(0, input_tokens - audio_input_tokens),
        "output_tokens": max(0, output_tokens - audio_output_tokens),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
    if audio_input_tokens:
        normalized["audio_input_tokens"] = audio_input_tokens
    if audio_output_tokens:
        normalized["audio_output_tokens"] = audio_output_tokens
    return normalized


def extract_embedding_usage(obj: Any) -> Dict[str, int]:
    """从 OpenAI Embeddings 响应提取仅输入 token 的 usage。"""
    if not isinstance(obj, dict):
        return {}
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return {}
    input_tokens = _to_token_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    if input_tokens is None:
        return {}
    return {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def extract_usage(obj: Any) -> Dict[str, int]:
    """从 Anthropic / Responses / Chat Completions 的 JSON 中提取 usage。"""
    if not isinstance(obj, dict):
        return {}

    event_type = obj.get("type")
    if event_type in ("response.completed", "response.incomplete"):
        return _normalize_usage((obj.get("response") or {}).get("usage"))

    if obj.get("object") == "response" or (event_type is None and "output" in obj and "usage" in obj):
        tokens = _normalize_usage(obj.get("usage"))
        if tokens:
            return tokens

    if obj.get("object") in ("chat.completion", "chat.completion.chunk"):
        return _normalize_usage(obj.get("usage"))

    delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
    stop_reason = delta.get("stop_reason") or obj.get("stop_reason")
    usage = obj.get("usage")
    if usage is None:
        usage = delta.get("usage")

    if event_type in ("message_delta", "message") or stop_reason in ANTHROPIC_STOP_REASONS:
        tokens = _normalize_usage(usage)
        if tokens:
            return tokens

    # gpt-audio chat usage 可能将音频 token 按 modality 单列。
    if isinstance(usage, dict):
        tokens = _normalize_usage(usage)
        if tokens and (tokens.get("audio_input_tokens") or tokens.get("audio_output_tokens")):
            return tokens

    return {}


def parse_sse_block(block: str) -> Optional[dict]:
    data_lines = []
    for line in block.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _find_sse_event_end(buf: str) -> Optional[int]:
    found = -1
    sep_len = 0
    for sep in ("\r\n\r\n", "\n\n"):
        idx = buf.find(sep)
        if idx != -1 and (found == -1 or idx < found):
            found = idx
            sep_len = len(sep)
    if found == -1:
        return None
    return found + sep_len


STREAM_TERMINATOR_TYPES = {
    "message_stop",
    "response.completed",
    "response.incomplete",
    "image_generation.completed",
    "image.generation.completed",
}
STREAM_TERMINATOR_OBJECTS = {
    "image.generation.result",
}


def split_sse_events(buf: str) -> tuple:
    """拆出完整 SSE 事件，剩余半包留在 buffer 里。"""
    events = []
    while True:
        end = _find_sse_event_end(buf)
        if end is None:
            break
        events.append(buf[:end])
        buf = buf[end:]
    return events, buf


def _find_sse_event_end_bytes(buf: bytes) -> Optional[int]:
    found = -1
    sep_len = 0
    for sep in (b"\r\n\r\n", b"\n\n"):
        idx = buf.find(sep)
        if idx != -1 and (found == -1 or idx < found):
            found = idx
            sep_len = len(sep)
    if found == -1:
        return None
    return found + sep_len


def split_sse_events_bytes(buf: bytes) -> tuple:
    """按字节拆完整 SSE 事件，避免 UTF-8 多字节字符跨 TCP 分片时被提前 decode 弄坏。"""
    events: list[bytes] = []
    while True:
        end = _find_sse_event_end_bytes(buf)
        if end is None:
            break
        events.append(buf[:end])
        buf = buf[end:]
    return events, buf


def is_stream_terminator(event_text: str, obj: Optional[dict] = None) -> bool:
    if obj is None:
        obj = parse_sse_block(event_text)
    if isinstance(obj, dict):
        if obj.get("type") in STREAM_TERMINATOR_TYPES:
            return True
        if obj.get("object") in STREAM_TERMINATOR_OBJECTS:
            return True
        candidates = obj.get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            finish = candidates[0].get("finishReason") or candidates[0].get("finish_reason")
            if finish:
                return True
    for line in event_text.splitlines():
        if line.strip() in {"data: [DONE]", "data:[DONE]"}:
            return True
    return False


def ensure_sse_event_name(event_text: str, obj: Optional[dict] = None) -> str:
    """Responses 客户端常依赖 event: 行；上游若只给 data.type，这里补齐。"""
    if obj is None:
        obj = parse_sse_block(event_text)
    if not obj or not obj.get("type"):
        return event_text
    typ = str(obj.get("type"))
    lines = event_text.splitlines()
    if any(line.startswith("event:") for line in lines):
        return event_text if event_text.endswith("\n\n") or event_text.endswith("\r\n\r\n") else event_text.rstrip("\n") + "\n\n"
    body = "\n".join(lines).rstrip("\n")
    return f"event: {typ}\n{body}\n\n"


# 这些要客户端执行后再发下一轮。web_search 这类由服务端做完，不算任务未结束。
_CLIENT_TOOL_TYPES = {"function_call", "computer_call", "custom_tool_call", "tool_call", "mcp_call"}


def _response_output_items(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    output = payload.get("output")
    if isinstance(output, list):
        return output
    nested = payload.get("response")
    if isinstance(nested, dict) and isinstance(nested.get("output"), list):
        return nested["output"]
    return []


def response_requests_client_tool(payload: Any) -> bool:
    """Responses 的 status=completed 仍可能带着待客户端执行的工具调用。"""
    for item in _response_output_items(payload):
        if isinstance(item, dict) and item.get("type") in _CLIENT_TOOL_TYPES:
            return True
    if not isinstance(payload, dict):
        return False
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or choices[0].get("delta") or {}
        if isinstance(message, dict):
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                return True
    return False


def responses_completion_status(obj: Optional[dict]) -> Optional[str]:
    if not isinstance(obj, dict) or obj.get("type") not in {
        "response.completed",
        "response.incomplete",
        "response.failed",
    }:
        return None
    resp = obj.get("response")
    if isinstance(resp, dict) and resp.get("status") is not None:
        return str(resp.get("status"))
    if obj.get("type") == "response.incomplete":
        return "incomplete"
    if obj.get("type") == "response.failed":
        return "failed"
    return None


def _result_from_finish(finish: str) -> str:
    text = (finish or "").lower()
    if text in {"stop", "end_turn", "completed", "tool_calls", "tool_use", "function_call"}:
        return "completed"
    if text in {"length", "max_tokens", "max_output_tokens"}:
        return "truncated"
    if text in {"content_filter", "refusal"}:
        return "filtered"
    if text in {"cancelled", "canceled"}:
        return "cancelled"
    if text in {"incomplete"}:
        return "incomplete"
    if text in {"failed", "error"}:
        return "error"
    return text or "unknown"


def extract_completion_meta(payload: Any = None) -> Dict[str, str]:
    """从聊天/Responses/Anthropic 响应里抽出 finish 和任务是否完成。"""
    if not isinstance(payload, dict):
        return {}
    typ = payload.get("type")
    if typ in {"response.completed", "response.incomplete", "response.failed"}:
        nested = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        status = str(nested.get("status") or responses_completion_status(payload) or "")
        details = nested.get("incomplete_details") or payload.get("incomplete_details") or {}
        reason = ""
        if isinstance(details, dict):
            reason = str(details.get("reason") or "")
        error = nested.get("error") or payload.get("error") or {}
        if not reason and isinstance(error, dict):
            reason = str(error.get("code") or error.get("type") or "")
        finish = f"{status}:{reason}" if status and reason else (status or typ)
        if (status or "").lower() == "completed" and response_requests_client_tool(payload):
            return {"finish": "function_call", "result": "completed"}
        return {"finish": finish, "result": _result_from_finish(status or typ.rsplit(".", 1)[-1])}

    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason") or choices[0].get("finishReason")
        if finish:
            if str(finish).lower() in {"stop", "completed"} and response_requests_client_tool(payload):
                return {"finish": "tool_calls", "result": "completed"}
            return {"finish": str(finish), "result": _result_from_finish(str(finish))}

    stop = payload.get("stop_reason")
    if not stop:
        delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
        stop = delta.get("stop_reason")
    if stop:
        return {"finish": str(stop), "result": _result_from_finish(str(stop))}

    if payload.get("object") == "response" or ("output" in payload and payload.get("status")):
        status = str(payload.get("status") or "")
        details = payload.get("incomplete_details") if isinstance(payload.get("incomplete_details"), dict) else {}
        reason = str(details.get("reason") or "")
        finish = f"{status}:{reason}" if status and reason else status
        if finish:
            if status.lower() == "completed" and response_requests_client_tool(payload):
                return {"finish": "function_call", "result": "completed"}
            return {"finish": finish, "result": _result_from_finish(status)}

    nested = payload.get("response")
    if isinstance(nested, dict) and nested is not payload:
        return extract_completion_meta(nested)
    return {}


def format_access_usage(usage: Optional[dict] = None, billing: Optional[dict] = None) -> str:
    """access 日志用的 token 字段：输入/输出/缓存读写。"""
    data: Dict[str, Any] = {}
    for src in (usage, billing):
        if not isinstance(src, dict):
            continue
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "audio_input_tokens",
            "audio_output_tokens",
        ):
            if src.get(key) is not None:
                data[key] = src.get(key)
    if not data:
        return ""
    parts = [
        f"in={int(data.get('input_tokens') or 0)}",
        f"out={int(data.get('output_tokens') or 0)}",
        f"cache_r={int(data.get('cache_read_tokens') or 0)}",
        f"cache_w={int(data.get('cache_write_tokens') or 0)}",
    ]
    audio_in = int(data.get("audio_input_tokens") or 0)
    audio_out = int(data.get("audio_output_tokens") or 0)
    if audio_in or audio_out:
        parts.append(f"audio_in={audio_in}")
        parts.append(f"audio_out={audio_out}")
    return " ".join(parts)


def _fmt_money(amount: float) -> str:
    if abs(amount) >= 0.01:
        return f"{amount:.4f}"
    return f"{amount:.6f}"


def _fmt_usd_per_m(amount) -> str:
    if amount is None:
        return "-"
    try:
        n = float(amount)
    except (TypeError, ValueError):
        return "-"
    if abs(n - round(n)) < 1e-9:
        return f"${int(round(n))}"
    if abs(n * 100 - round(n * 100)) < 1e-9:
        return f"${n:.2f}"
    if abs(n * 1000 - round(n * 1000)) < 1e-9:
        return f"${n:.3f}"
    return f"${n:.4f}"


def format_billing_notice(info: dict) -> str:
    if (info.get("kind") or "") == "image":
        return _format_image_billing_notice(info)
    if (info.get("kind") or "") == "embedding":
        model = (info.get("model") or "").strip() or "-"
        return (
            f"\n\n【计费】模型 {model}，输入 {info.get('input_tokens', 0)} tokens，"
            f"官方输入价格 {_fmt_usd_per_m(info.get('input_price'))}/1M tokens。"
            f"实际花费 {_fmt_money(float(info.get('charged') or 0))} 元，"
            f"利润 {_fmt_money(float(info.get('profit') or 0))} 元，"
            f"账户余额 {_fmt_money(float(info.get('remain') or 0))} 元。"
        )
    model = (info.get("model") or "").strip() or "-"
    service_tier = (info.get("service_tier") or "standard").strip().lower()
    reasoning_effort = (info.get("reasoning_effort") or "default").strip().lower()
    context_tier = (info.get("context_tier") or "").strip().lower()
    if info.get("long_context") or context_tier == "long":
        long_context_note = "，按长上下文计费"
    elif context_tier == "short":
        long_context_note = "，按短上下文计费"
    else:
        long_context_note = ""
    cache_read = int(info.get("cache_read_tokens") or 0)
    cache_write = int(info.get("cache_write_tokens") or 0)
    official = (
        f"官方定价 input {_fmt_usd_per_m(info.get('input_price'))}/1M tokens、"
        f"output {_fmt_usd_per_m(info.get('output_price'))}/1M tokens、"
        f"cache read {_fmt_usd_per_m(info.get('cache_read_price'))}/1M tokens、"
        f"cache write {_fmt_usd_per_m(info.get('cache_write_price'))}/1M tokens"
    )
    if cache_read > 0:
        cache_note = "已命中服务端缓存"
    elif cache_write > 0:
        cache_note = "未命中服务端缓存，已写入缓存"
    else:
        cache_note = "未命中服务端缓存"
    headroom_note = _format_headroom_notice(info)
    return (
        f"\n\n【计费】模型 {model}，模式 {service_tier}（官方价格倍率 "
        f"{float(info.get('service_tier_multiplier') or 1):g}x），"
        f"推理强度 {reasoning_effort}{long_context_note}，{official}。"
        f"输入 {info.get('input_tokens', 0)} tokens，"
        f"缓存命中 {cache_read} tokens，"
        f"缓存写入 {cache_write} tokens，"
        f"输出 {info.get('output_tokens', 0)} tokens。"
        f"{cache_note}。"
        f"{headroom_note}"
        f"实际花费 {_fmt_money(float(info.get('charged') or 0))} 元，"
        f"利润 {_fmt_money(float(info.get('profit') or 0))} 元，"
        f"账户余额 {_fmt_money(float(info.get('remain') or 0))} 元。"
    )


def _format_headroom_notice(info: dict) -> str:
    saved = int(info.get("headroom_tokens_saved") or 0)
    before = int(info.get("headroom_tokens_before") or 0)
    after = int(info.get("headroom_tokens_after") or 0)
    if saved <= 0 or before <= 0:
        return ""
    ratio = info.get("headroom_ratio")
    if ratio is None:
        ratio = saved / before
    return (
        f"Headroom 压缩前 {before} tokens，压缩后 {after} tokens，"
        f"节省 {saved} tokens（{float(ratio):.0%}）。"
    )


def _format_image_billing_notice(info: dict) -> str:
    model = (info.get("model") or "").strip() or "-"
    n = int(info.get("n") or 1)
    if (info.get("billing") or "") == "per_image":
        tier = (info.get("size_tier") or "1k").upper()
        official = (
            f"Teamo 按张定价 {tier} ${_fmt_money_usd(float(info.get('per_image_price') or 0))}/张"
        )
        return (
            f"\n\n【计费】模型 {model}，{official}。"
            f"生成 {n} 张。"
            f"实际花费 {_fmt_money(float(info.get('charged') or 0))} 元，"
            f"利润 {_fmt_money(float(info.get('profit') or 0))} 元，"
            f"账户余额 {_fmt_money(float(info.get('remain') or 0))} 元。"
        )
    official = (
        f"官方定价 text input {_fmt_usd_per_m(info.get('text_price'))}/1M tokens、"
        f"image input {_fmt_usd_per_m(info.get('image_input_price'))}/1M tokens、"
        f"image output {_fmt_usd_per_m(info.get('output_price'))}/1M tokens"
    )
    return (
        f"\n\n【计费】模型 {model}，{official}。"
        f"文本输入 {info.get('text_tokens', 0)} tokens，"
        f"图像输入 {info.get('image_input_tokens', 0)} tokens，"
        f"图像输出 {info.get('output_tokens', 0)} tokens。"
        f"生成 {n} 张。"
        f"实际花费 {_fmt_money(float(info.get('charged') or 0))} 元，"
        f"利润 {_fmt_money(float(info.get('profit') or 0))} 元，"
        f"账户余额 {_fmt_money(float(info.get('remain') or 0))} 元。"
    )


def _fmt_money_usd(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _sse_event(payload: dict, event: Optional[str] = None) -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {data}\n\n".encode("utf-8")
    return f"data: {data}\n\n".encode("utf-8")


def billing_sse_bytes(source: str, info: dict) -> list:
    if info.get("kind") == "embedding":
        return []
    text = format_billing_notice(info)
    if source == "images" or (info.get("kind") == "image"):
        now = int(time.time())
        return [
            _sse_event(
                {
                    "created": now,
                    "data": [],
                    "billing": {
                        "notice": text,
                        "charged": info.get("charged"),
                        "profit": info.get("profit"),
                        "remain": info.get("remain"),
                    },
                }
            )
        ]
    if source == "anthropic":
        idx = int(info.get("next_block_index") or 1)
        return [
            _sse_event(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
                "content_block_start",
            ),
            _sse_event(
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                },
                "content_block_delta",
            ),
            _sse_event({"type": "content_block_stop", "index": idx}, "content_block_stop"),
        ]
    if source == "responses":
        return [
            _sse_event({"type": "response.output_text.delta", "delta": text}, "response.output_text.delta")
        ]
    now = int(time.time())
    return [
        _sse_event(
            {
                "id": "billing",
                "object": "chat.completion.chunk",
                "created": now,
                "model": info.get("model") or "",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
        )
    ]


def attach_billing_to_payload(payload: dict, info: dict, source: str) -> dict:
    if not isinstance(payload, dict) or not info:
        return payload
    text = format_billing_notice(info)
    if info.get("kind") == "embedding":
        payload["billing"] = {
            "notice": text,
            "charged": info.get("charged"),
            "profit": info.get("profit"),
            "remain": info.get("remain"),
            "usd_cost": info.get("usd_cost"),
            "model": info.get("model"),
            "input_tokens": info.get("input_tokens"),
        }
        return payload
    if source == "images" or info.get("kind") == "image":
        payload["billing"] = {
            "notice": text,
            "charged": info.get("charged"),
            "profit": info.get("profit"),
            "remain": info.get("remain"),
            "usd_cost": info.get("usd_cost"),
            "model": info.get("model"),
            "n": info.get("n"),
        }
        return payload
    if source == "anthropic":
        content = payload.get("content")
        if isinstance(content, list):
            content.append({"type": "text", "text": text})
        return payload
    if source == "responses":
        output = payload.get("output")
        if isinstance(output, list):
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            )
        return payload
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message")
        if isinstance(message, dict):
            message["content"] = (message.get("content") or "") + text
    return payload


class StreamUsageCollector:
    """跨 TCP chunk 拼接 SSE，避免 usage 事件被拆包后漏计费。"""

    def __init__(self):
        self._buf = ""
        self.usage: Dict[str, int] = {}
        self.max_block_index = -1
        self.request_id = ""

    def note_request_id(self, obj: Optional[dict] = None, headers: Optional[dict] = None):
        from utils.teamo_billing import better_teamo_request_id, extract_teamo_request_id
        found = extract_teamo_request_id(obj, headers)
        if not found:
            return
        self.request_id = better_teamo_request_id(self.request_id, found)

    def note_block_index(self, obj: Optional[dict]):
        if not obj:
            return
        if obj.get("type") in {"content_block_start", "content_block_delta", "content_block_stop"}:
            try:
                self.max_block_index = max(self.max_block_index, int(obj.get("index", 0)))
            except (TypeError, ValueError):
                return

    def feed(self, text: str) -> Dict[str, int]:
        if not text:
            return {}
        self._buf += text
        found = {}
        while True:
            end = _find_sse_event_end(self._buf)
            if end is None:
                break
            block = self._buf[:end]
            self._buf = self._buf[end:]
            obj = parse_sse_block(block)
            if not obj:
                continue
            self.note_block_index(obj)
            self.note_request_id(obj)
            usage = extract_usage(obj) or extract_embedding_usage(obj) or _extract_image_usage_safe(obj)
            if usage:
                self.usage = usage
                found = usage
        return found

    def flush(self) -> Dict[str, int]:
        leftover = self._buf.strip()
        self._buf = ""
        if not leftover:
            return {}
        obj = parse_sse_block(leftover)
        if not obj:
            return {}
        self.note_block_index(obj)
        self.note_request_id(obj)
        usage = extract_usage(obj) or extract_embedding_usage(obj) or _extract_image_usage_safe(obj)
        if usage:
            self.usage = usage
        return usage


def _extract_image_usage_safe(obj: Optional[dict]) -> Dict[str, Any]:
    if not obj:
        return {}
    from utils.image_billing import extract_image_usage
    return extract_image_usage(obj) or {}


def chunk_data(data: str) -> Dict[str, int]:
    """兼容旧接口：从一段流式文本里尽量抽出 usage。"""
    collector = StreamUsageCollector()
    usage = collector.feed(data if data.endswith("\n\n") else data + "\n\n")
    if usage:
        return usage
    try:
        obj = json.loads(data)
    except Exception:
        start = data.find("{")
        if start == -1:
            return {}
        try:
            obj = json.loads(data[start:])
        except Exception:
            return {}
    return extract_usage(obj)


def auth_fail_message(reason: str) -> str:
    if reason == "not exist":
        return "该账户不存在。"
    if reason == "no money":
        return "该账户余额不足。"
    return f"该账户{reason}。"


def build_error_payload(source: str, message: str, reason: str) -> dict:
    error_type = "authentication_error" if reason == "not exist" else "permission_error"
    if source == "anthropic":
        return {
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        }
    openai_type = "invalid_request_error" if reason == "not exist" else "insufficient_quota"
    return {
        "error": {
            "message": message,
            "type": openai_type,
            "code": "invalid_api_key" if reason == "not exist" else "insufficient_quota",
        }
    }


def auth_fail_status(reason: str) -> int:
    return 401 if reason == "not exist" else 403


def common_message_yield(message: str, source: str = "openai"):
    """按客户端协议返回一段可读的流式错误。"""
    text = message if message.endswith("。") else f"该账户{message}。"
    now = int(time.time())
    if source == "anthropic":
        payload = {
            "type": "error",
            "error": {"type": "authentication_error", "message": text},
        }
        yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    if source == "responses":
        payload = {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": text},
        }
        yield f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    chunk_obj = {
        "id": str(now),
        "object": "chat.completion.chunk",
        "created": now,
        "model": "gpt-4o-mini",
        "choices": [
            {
                "delta": {"content": text},
                "index": 0,
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


if __name__ == "__main__":
    error_str = """event: content_block_delta
data: {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "收到"}}

"""
    test_str = """event: message_delta
data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": null}, "usage": {"input_tokens": 33082, "output_tokens": 107}}

"""
    print("anthropic empty:", chunk_data(error_str))
    print("anthropic usage:", chunk_data(test_str))
