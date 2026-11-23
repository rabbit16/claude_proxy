# -*- coding: utf-8 -*-
"""Gemini 原生协议桥接。

部分号商（如 xcmapi）只在 ``/v1beta/models/{model}:generateContent`` 上提供
Gemini 模型，OpenAI / Responses / Anthropic 入口一律 404 / 503。命中供应商
``gemini_native`` 开关时，这里把三种客户端请求统一改写成 Gemini 原生请求体，
再把响应转回客户端协议；生图模型额外映射成 ``/v1/images/*`` 的返回形状。

本模块只做数据转换，不涉及转发与计费；调用方负责选供应商、拼 URL 和扣费。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, List, Optional

from utils.openai_responses_bridge import _content_to_text

# Gemini finishReason → OpenAI finish_reason
_GEMINI_FINISH_REASONS = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "MALFORMED_FUNCTION_CALL": "stop",
    "LANGUAGE": "content_filter",
    "OTHER": "stop",
}

# OpenAI 图片尺寸 → Gemini imageConfig.aspectRatio
_IMAGE_ASPECT_RATIOS = {
    "1024x1024": "1:1",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "2048x2048": "1:1",
    "2048x1152": "16:9",
    "1152x2048": "9:16",
}
# JSON Schema 里 Gemini 不认的键（传过去会 400）
_SCHEMA_DROP_KEYS = {
    "$schema", "$id", "additionalProperties", "strict", "default",
    "examples", "example", "title", "definitions", "$defs",
}


def is_gemini_model(model: str) -> bool:
    return (model or "").strip().lower().startswith("gemini")


def is_gemini_image_model(model: str) -> bool:
    mid = (model or "").strip().lower()
    return "image" in mid or "banana" in mid


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def gemini_usage_to_openai(usage_meta: Any) -> Optional[dict]:
    """Gemini usageMetadata → OpenAI chat usage（prompt_tokens 含缓存）。"""
    if not isinstance(usage_meta, dict):
        return None
    prompt = _to_int(usage_meta.get("promptTokenCount"))
    completion = _to_int(usage_meta.get("candidatesTokenCount"))
    thoughts = _to_int(usage_meta.get("thoughtsTokenCount")) or 0
    if prompt is None and completion is None:
        return None
    prompt = prompt or 0
    completion = (completion or 0) + thoughts
    usage = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    cached = _to_int(usage_meta.get("cachedContentTokenCount"))
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return usage


def gemini_usage_tokens(resp: Any) -> dict:
    """Gemini 响应 → 计费用的 normalized usage（input/output/cache_read）。"""
    from utils.common_utils import extract_usage

    usage = gemini_usage_to_openai((resp or {}).get("usageMetadata"))
    if not usage:
        return {}
    return extract_usage({"object": "chat.completion", "usage": usage})


def _sanitize_schema(node: Any, depth: int = 0) -> Any:
    if depth > 12:
        return {}
    if isinstance(node, list):
        return [_sanitize_schema(item, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for key, value in node.items():
        if key in _SCHEMA_DROP_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _sanitize_schema(v, depth + 1) for k, v in value.items()}
        elif key in {"items", "not", "anyOf", "oneOf", "allOf"}:
            out[key] = _sanitize_schema(value, depth + 1)
        else:
            out[key] = value
    return out


def _data_url_part(url: Any) -> Optional[dict]:
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, sep, data = url.partition(",")
    if not sep or not data:
        return None
    meta = header[5:]
    mime = meta.split(";")[0] or "image/png"
    if ";base64" not in meta:
        return None
    return {"inlineData": {"mimeType": mime, "data": data}}


def _gemini_parts(content: Any) -> List[dict]:
    """OpenAI content（str / parts 数组）→ Gemini parts。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if not isinstance(content, list):
        return [{"text": str(content)}]
    parts: List[dict] = []
    for item in content:
        if isinstance(item, str):
            if item:
                parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue
        ptype = item.get("type")
        if ptype in {"text", "input_text", "output_text"}:
            text = item.get("text") or ""
            if text:
                parts.append({"text": text})
        elif ptype in {"image_url", "input_image"}:
            raw = item.get("image_url")
            url = raw.get("url") if isinstance(raw, dict) else (item.get("url") or raw)
            inline = _data_url_part(url)
            if inline:
                parts.append(inline)
            elif isinstance(url, str) and url:
                # 非 data URL 只能交给上游按 URI 解析，失败由上游报错
                parts.append({"fileData": {"fileUri": url}})
        elif ptype == "input_audio":
            parts.append({"text": "[audio]"})
        elif ptype in {"file", "input_file", "document"}:
            name = item.get("filename") or (item.get("file") or {}).get("filename") or "file"
            parts.append({"text": f"[file:{name}]"})
        elif ptype == "refusal" and item.get("refusal"):
            parts.append({"text": item.get("refusal")})
        else:
            blob = json.dumps(item, ensure_ascii=False)
            parts.append({"text": blob[:2000]})
    return parts


def _tool_declarations(tools: Any) -> List[dict]:
    decls: List[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if fn is None and tool.get("name"):
            fn = tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        decl = {"name": fn.get("name")}
        if fn.get("description"):
            decl["description"] = str(fn.get("description"))
        params = fn.get("parameters")
        decl["parameters"] = (
            _sanitize_schema(params) if isinstance(params, dict)
            else {"type": "object", "properties": {}}
        )
        decls.append(decl)
    return decls


def _tool_config(choice: Any) -> Optional[dict]:
    if isinstance(choice, str):
        mode = {"auto": "AUTO", "required": "ANY", "none": "NONE"}.get(choice.strip().lower())
        return {"mode": mode} if mode else None
    if isinstance(choice, dict):
        fn = choice.get("function") if isinstance(choice.get("function"), dict) else {}
        name = fn.get("name") or choice.get("name")
        if name:
            return {"mode": "ANY", "allowedFunctionNames": [str(name)]}
    return None


def openai_chat_to_gemini(payload: dict) -> dict:
    """OpenAI Chat Completions 请求 → Gemini generateContent 请求体。"""
    payload = payload if isinstance(payload, dict) else {}
    system_parts: List[str] = []
    contents: List[dict] = []
    tool_names: dict[str, str] = {}

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "user").strip().lower()
        if role in {"system", "developer"}:
            text = _content_to_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            name = tool_names.get(msg.get("tool_call_id") or "") or "tool"
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": name,
                        "response": {"content": _content_to_text(msg.get("content"))},
                    },
                }],
            })
            continue
        if role == "assistant":
            parts = _gemini_parts(msg.get("content"))
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = fn.get("name") or "tool"
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except Exception:
                    args = {"raw": raw}
                if not isinstance(args, dict):
                    args = {"value": args}
                tool_names[call.get("id") or ""] = name
                parts.append({"functionCall": {"name": name, "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})
            continue
        parts = _gemini_parts(msg.get("content"))
        if parts:
            contents.append({"role": "user", "parts": parts})

    out: dict = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}

    decls = _tool_declarations(payload.get("tools"))
    if decls:
        out["tools"] = [{"functionDeclarations": decls}]
        cfg = _tool_config(payload.get("tool_choice"))
        if cfg:
            out["toolConfig"] = {"functionCallingConfig": cfg}

    gen: dict = {}
    max_tokens = payload.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, (int, float)) and not isinstance(max_tokens, bool) and max_tokens > 0:
        gen["maxOutputTokens"] = int(max_tokens)
    if payload.get("temperature") is not None:
        gen["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        gen["topP"] = payload.get("top_p")
    if payload.get("top_k") is not None:
        gen["topK"] = payload.get("top_k")
    stop = payload.get("stop")
    if isinstance(stop, str):
        gen["stopSequences"] = [stop]
    elif isinstance(stop, list):
        stops = [str(item) for item in stop if item]
        if stops:
            gen["stopSequences"] = stops
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        fmt = response_format.get("type")
        if fmt == "json_object":
            gen["responseMimeType"] = "application/json"
        elif fmt == "json_schema":
            gen["responseMimeType"] = "application/json"
            schema = (response_format.get("json_schema") or {}).get("schema")
            if isinstance(schema, dict):
                gen["responseSchema"] = _sanitize_schema(schema)
    n = payload.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        gen["candidateCount"] = n
    if gen:
        out["generationConfig"] = gen
    return out


def _aspect_ratio(size: Any) -> tuple[Optional[str], Optional[str]]:
    raw = (size or "").strip().lower() if isinstance(size, str) else ""
    if raw in _IMAGE_ASPECT_RATIOS:
        return _IMAGE_ASPECT_RATIOS[raw], None
    if raw in {"1k", "2k", "4k"}:
        return None, raw.upper()
    return None, None


def openai_images_to_gemini(payload: dict, model: str = "") -> dict:
    """OpenAI Images 请求 → Gemini generateContent 请求体（生图）。"""
    payload = payload if isinstance(payload, dict) else {}
    prompt = payload.get("prompt")
    if isinstance(prompt, list):
        prompt = " ".join(str(item) for item in prompt if item)
    body: dict = {"contents": [{"role": "user", "parts": [{"text": str(prompt or "")}]}]}
    gen: dict = {"responseModalities": ["TEXT", "IMAGE"]}
    ratio, image_size = _aspect_ratio(payload.get("size"))
    image_config: dict = {}
    if ratio:
        image_config["aspectRatio"] = ratio
    if image_size:
        image_config["imageSize"] = image_size
    if image_config:
        gen["imageConfig"] = image_config
    n = payload.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        gen["candidateCount"] = n
    body["generationConfig"] = gen
    return body


def enable_image_output(body: dict) -> dict:
    """Gemini 生图模型必须声明 IMAGE 模态，否则只回文字。"""
    if not isinstance(body, dict):
        return body
    gen = body.setdefault("generationConfig", {})
    if isinstance(gen, dict) and not gen.get("responseModalities"):
        gen["responseModalities"] = ["TEXT", "IMAGE"]
    return body


def _message_from_parts(parts: Any) -> tuple[str, list, list, str]:
    """Gemini parts → (文本, 图片 data URL 列表, tool_calls, 思考文本)。"""
    texts: List[str] = []
    images: List[str] = []
    tool_calls: List[dict] = []
    thoughts: List[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            if part.get("thought"):
                thoughts.append(text)
            else:
                texts.append(text)
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            images.append(f"data:{mime};base64,{inline.get('data')}")
            continue
        fc = part.get("functionCall") or part.get("function_call")
        if isinstance(fc, dict):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": fc.get("name") or "tool",
                    "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                },
            })
    return "".join(texts), images, tool_calls, "".join(thoughts)


def _content_from_images(text: str, images: List[str]) -> Any:
    if not images:
        return text
    parts: List[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def gemini_response_to_chat(resp: dict, client_model: str = "") -> dict:
    """Gemini generateContent 响应 → OpenAI chat.completion。"""
    resp = resp if isinstance(resp, dict) else {}
    candidates = resp.get("candidates")
    cand = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand, dict) else []
    text, images, tool_calls, thoughts = _message_from_parts(parts)
    message: dict = {"role": "assistant", "content": _content_from_images(text, images) or None}
    if thoughts:
        message["reasoning_content"] = thoughts
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = _GEMINI_FINISH_REASONS.get(cand.get("finishReason") or "STOP", "stop")
    if tool_calls:
        finish = "tool_calls"
    out: dict = {
        "id": resp.get("responseId") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": client_model or resp.get("modelVersion") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    usage = gemini_usage_to_openai(resp.get("usageMetadata"))
    if usage:
        out["usage"] = usage
    return out


def gemini_response_to_images(resp: dict) -> dict:
    """Gemini 生图响应 → OpenAI Images 响应（b64_json）。"""
    resp = resp if isinstance(resp, dict) else {}
    data: List[dict] = []
    revised = ""
    for cand in resp.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        for part in ((cand.get("content") or {}).get("parts") or []):
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                data.append({"b64_json": inline.get("data")})
            elif isinstance(part.get("text"), str) and part.get("text"):
                revised = part.get("text")
    for item in data:
        if revised:
            item["revised_prompt"] = revised
    return {"created": int(time.time()), "data": data}


def gemini_response_to_responses(resp: dict, client_model: str = "") -> dict:
    """Gemini generateContent 响应 → Responses 对象（含图片时用 output_image）。"""
    resp = resp if isinstance(resp, dict) else {}
    candidates = resp.get("candidates")
    cand = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
    parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand, dict) else []
    text, images, tool_calls, thoughts = _message_from_parts(parts)
    output: List[dict] = []
    if thoughts:
        output.append({
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": thoughts}],
        })
    content: List[dict] = []
    if text:
        content.append({"type": "output_text", "text": text})
    for url in images:
        content.append({"type": "output_image", "image_url": url})
    if content:
        output.append({"type": "message", "role": "assistant", "content": content})
    for call in tool_calls:
        output.append({
            "type": "function_call",
            "call_id": call["id"],
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
        })
    out: dict = {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "model": client_model or resp.get("modelVersion") or "",
        "status": "completed",
        "output": output,
    }
    usage = gemini_usage_to_openai(resp.get("usageMetadata"))
    if usage:
        out["usage"] = {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
        }
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached:
            out["usage"]["input_tokens_details"] = {"cached_tokens": cached}
    return out


def gemini_error_to_client(payload: Any, source: str) -> dict:
    """Gemini 错误体 → 客户端协议的错误体。"""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        err = {"message": "upstream error"}
    message = err.get("message") or "upstream error"
    code = err.get("status") or err.get("code") or "api_error"
    if source == "anthropic":
        return {"type": "error", "error": {"type": str(code), "message": message}}
    return {"error": {"type": str(code), "message": message}}


class GeminiToOpenAIStream:
    """Gemini 流式分片 → OpenAI chat.completion.chunk 对象。

    调用方拿到 dict 后既可以直接序列化成 ``data:`` 事件（source=openai），
    也可以继续喂给 ProtocolStreamBridge 转成 Anthropic / Responses。
    """

    def __init__(self, client_model: str = ""):
        self.client_model = client_model
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.role_sent = False
        self.closed = False
        self.usage: Optional[dict] = None
        self.finish_reason: Optional[str] = None
        self._tool_seq = 0

    def _chunk(self, delta: Optional[dict] = None, finish_reason: Optional[str] = None,
               usage: Optional[dict] = None) -> dict:
        obj: dict = {
            "id": self.chunk_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.client_model,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
        }
        if usage:
            obj["usage"] = usage
        return obj

    def _open_role(self, out: List[dict]) -> None:
        if not self.role_sent:
            self.role_sent = True
            out.append(self._chunk({"role": "assistant"}))

    def feed(self, obj: Any) -> List[dict]:
        if self.closed or not isinstance(obj, dict):
            return []
        if obj.get("error"):
            self.closed = True
            return []
        out: List[dict] = []
        usage = gemini_usage_to_openai(obj.get("usageMetadata"))
        if usage:
            self.usage = usage
        for cand in obj.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            for part in ((cand.get("content") or {}).get("parts") or []):
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    self._open_role(out)
                    if part.get("thought"):
                        out.append(self._chunk({"reasoning_content": text}))
                    else:
                        out.append(self._chunk({"content": text}))
                    continue
                fc = part.get("functionCall") or part.get("function_call")
                if isinstance(fc, dict):
                    self._open_role(out)
                    index = self._tool_seq
                    self._tool_seq += 1
                    out.append(self._chunk({"tool_calls": [{
                        "index": index,
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "tool",
                            "arguments": json.dumps(fc.get("args") or {}, ensure_ascii=False),
                        },
                    }]}))
                    continue
                inline = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline, dict) and inline.get("data"):
                    mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    self._open_role(out)
                    out.append(self._chunk({"content": [{
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{inline.get('data')}"},
                    }]}))
            finish = cand.get("finishReason")
            if finish:
                self.finish_reason = _GEMINI_FINISH_REASONS.get(finish, "stop")
                if self._tool_seq:
                    self.finish_reason = "tool_calls"
        if self.finish_reason:
            out.append(self._chunk(finish_reason=self.finish_reason, usage=self.usage))
            self.closed = True
        return out

    def finish(self) -> List[dict]:
        if self.closed:
            return []
        self.closed = True
        out: List[dict] = []
        self._open_role(out)
        out.append(self._chunk(finish_reason=self.finish_reason or "stop", usage=self.usage))
        return out
