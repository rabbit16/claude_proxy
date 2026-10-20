# -*- coding: utf-8 -*-
"""GPT-6 在官方 chat/completions 上不支持 function tools，需桥到 /v1/responses。

仅用于本地 LiteLLM → OpenAI；号商模式不走此桥。
Cursor 仍收发 chat/completions，代理内部转换。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


def needs_gpt6_responses_bridge(parsed_body: Optional[dict]) -> bool:
    """gpt-6* + tools 的 chat/completions 必须改走 responses。"""
    if not isinstance(parsed_body, dict):
        return False
    model = (parsed_body.get("model") or "").strip()
    for prefix in ("openai/", "xai/"):
        if model.startswith(prefix):
            model = model[len(prefix) :]
            break
    if not model.startswith("gpt-6"):
        return False
    tools = parsed_body.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                ptype = p.get("type")
                if ptype in {"text", "input_text", "output_text"}:
                    parts.append(p.get("text") or "")
                elif ptype == "document":
                    from utils.anthropic_bridge import _anthropic_document_payload
                    kind, value = _anthropic_document_payload(p)
                    if kind == "text" and value:
                        parts.append(value)
                    elif kind == "file" and isinstance(value, dict):
                        filename = (value.get("file") or {}).get("filename") or "file"
                        parts.append(f"[file:{filename}]")
                elif ptype in {"image", "image_url", "input_image"}:
                    parts.append("[image]")
                elif ptype in {"file", "input_file"}:
                    name = p.get("filename") or (p.get("file") or {}).get("filename") or "file"
                    parts.append(f"[file:{name}]")
                elif ptype == "input_audio":
                    parts.append("[audio]")
                elif ptype == "thinking":
                    parts.append(p.get("thinking") or "")
                else:
                    blob = json.dumps(p, ensure_ascii=False)
                    if len(blob) > 2000:
                        blob = json.dumps({"type": ptype or "unknown"}, ensure_ascii=False)
                    parts.append(blob)
        return "".join(parts)
    return str(content)


def _responses_input_file(part: dict) -> Optional[dict]:
    """文件块转成 Responses 的 input_file。纯文本文档返回 None，由调用方写成文本。"""
    if not isinstance(part, dict):
        return None
    if part.get("type") == "input_file":
        item = {"type": "input_file"}
        for key in ("file_id", "filename", "file_data", "file_url"):
            if part.get(key):
                item[key] = part[key]
        return item if len(item) > 1 else None
    if part.get("type") == "file" and isinstance(part.get("file"), dict):
        return _responses_input_file({"type": "input_file", **part["file"]})
    if part.get("type") == "document":
        from utils.anthropic_bridge import _anthropic_document_payload
        kind, value = _anthropic_document_payload(part)
        if kind == "file" and isinstance(value, dict):
            return _responses_input_file(value)
    return None


def _content_to_responses(content: Any, role: str = "user") -> Any:
    """Responses 接受 string，或按角色区分的 content parts。

    - user/system/developer：input_text / input_image
    - assistant 历史：output_text（不能用 input_text，否则 OpenAI 400）
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    text_type = "output_text" if role == "assistant" else "input_text"
    parts: List[dict] = []
    for p in content:
        if isinstance(p, str):
            parts.append({"type": text_type, "text": p})
            continue
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype in {"text", "input_text", "output_text"}:
            parts.append({"type": text_type, "text": p.get("text") or ""})
        elif ptype in {"file", "input_file", "document"}:
            file_part = _responses_input_file(p)
            if file_part:
                parts.append(file_part)
            elif ptype == "document":
                from utils.anthropic_bridge import _anthropic_document_payload
                kind, value = _anthropic_document_payload(p)
                if kind == "text" and value:
                    parts.append({"type": text_type, "text": value})
        elif ptype in {"image_url", "input_image"}:
            image_url = p.get("image_url")
            if isinstance(image_url, dict):
                url = image_url.get("url") or ""
            else:
                url = p.get("url") or ""
            parts.append({"type": "input_image", "image_url": url})
        elif ptype == "input_audio":
            audio = p.get("input_audio") if isinstance(p.get("input_audio"), dict) else p
            if audio.get("data"):
                parts.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": audio.get("data") or "",
                        "format": audio.get("format") or "wav",
                    },
                })
        elif ptype == "refusal" and p.get("refusal"):
            parts.append({"type": text_type, "text": p.get("refusal") or ""})
        else:
            blob = json.dumps(p, ensure_ascii=False)
            if len(blob) > 2000:
                blob = json.dumps({"type": ptype or "unknown"}, ensure_ascii=False)
            parts.append({"type": text_type, "text": blob})
    return parts or ""


def messages_to_responses_input(messages: list) -> list:
    items: List[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and isinstance(msg.get("reasoning_content"), str) and msg.get("reasoning_content"):
            items.append({
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": msg.get("reasoning_content")}],
            })
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": _content_to_text(msg.get("content")),
                }
            )
            continue
        if role == "assistant" and msg.get("tool_calls"):
            text = _content_to_text(msg.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "name": fn.get("name") or "tool",
                        "arguments": args or "{}",
                    }
                )
            continue
        if role in {"system", "developer", "user", "assistant"}:
            items.append(
                {
                    "role": role,
                    "content": _content_to_responses(msg.get("content"), role=role),
                }
            )
    return items


def _reasoning_item_text(item: dict) -> str:
    bits = []
    summary = item.get("summary")
    if isinstance(summary, str):
        bits.append(summary)
    elif isinstance(summary, list):
        for part in summary:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                bits.append(part.get("text") or "")
    content = item.get("content")
    if isinstance(content, str):
        bits.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                bits.append(part.get("text") or part.get("thinking") or "")
    return "".join(bits)


def _convert_tools(tools: list) -> list:
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            item = {
                "type": "function",
                "name": fn.get("name"),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if fn.get("description"):
                item["description"] = fn.get("description")
            out.append(item)
            continue
        # 已是 responses 形态
        if tool.get("type") == "function" and tool.get("name"):
            out.append(tool)
    return out


def _convert_tool_choice(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
            name = fn.get("name") or tool_choice.get("name")
            if name:
                return {"type": "function", "name": name}
        return tool_choice
    return tool_choice


def chat_completions_to_responses_request(payload: dict) -> dict:
    """OpenAI chat/completions 请求 → Responses 请求。"""
    out: Dict[str, Any] = {
        "model": payload.get("model"),
        "input": messages_to_responses_input(payload.get("messages") or []),
        "stream": bool(payload.get("stream")),
    }
    tools = _convert_tools(payload.get("tools") or [])
    if tools:
        out["tools"] = tools
    tool_choice = _convert_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    if payload.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = payload.get("parallel_tool_calls")

    max_out = payload.get("max_completion_tokens")
    if max_out is None:
        max_out = payload.get("max_tokens")
    if max_out is not None:
        out["max_output_tokens"] = max_out

    for key in (
        "temperature", "top_p", "user", "metadata", "store", "service_tier",
        "frequency_penalty", "presence_penalty", "seed",
    ):
        if payload.get(key) is not None:
            out[key] = payload.get(key)
    response_format = payload.get("response_format")
    if isinstance(response_format, dict):
        if response_format.get("type") == "json_schema" and isinstance(response_format.get("json_schema"), dict):
            schema = response_format["json_schema"]
            fmt = {"type": "json_schema", "name": schema.get("name") or "response"}
            if schema.get("schema") is not None:
                fmt["schema"] = schema.get("schema")
            if schema.get("strict") is not None:
                fmt["strict"] = schema.get("strict")
            out["text"] = {"format": fmt}
        elif response_format.get("type") == "json_object":
            out["text"] = {"format": {"type": "json_object"}}

    # chat 的 reasoning_effort → responses.reasoning.effort
    effort = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        effort = reasoning.get("effort")
    if effort:
        out["reasoning"] = {"effort": effort}

    return out


def prepare_gpt6_responses_upstream(
    body: bytes, parsed_body: dict, headers: dict
) -> tuple:
    """改写 body/headers/path，转发到 /v1/responses。"""
    wire = chat_completions_to_responses_request(parsed_body)
    new_body = json.dumps(wire, ensure_ascii=False).encode("utf-8")
    new_headers = dict(headers or {})
    new_headers["content-type"] = "application/json"
    # 避免把 chat 专用头带给 responses
    for drop in ("anthropic-version", "anthropic-beta"):
        new_headers.pop(drop, None)
        # header keys may be mixed case
        for k in list(new_headers.keys()):
            if k.lower() == drop:
                new_headers.pop(k, None)
    return new_body, wire, new_headers, "/v1/responses"


def _sse_data(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _chat_chunk(
    *,
    chunk_id: str,
    model: str,
    created: int,
    delta: Optional[dict] = None,
    finish_reason: Optional[str] = None,
    usage: Optional[dict] = None,
) -> dict:
    choice: Dict[str, Any] = {
        "index": 0,
        "delta": delta or {},
        "finish_reason": finish_reason,
    }
    obj: Dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if usage:
        obj["usage"] = usage
    return obj


def _usage_to_chat(usage: Optional[dict]) -> Optional[dict]:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
    completion = usage.get("output_tokens", usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    out = {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
    }
    details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        out["prompt_tokens_details"] = {"cached_tokens": details.get("cached_tokens")}
    return out


def responses_to_chat_completion(resp: dict, client_model: str = "") -> dict:
    """非流式 Responses → chat.completion。"""
    model = client_model or resp.get("model") or ""
    created = int(resp.get("created_at") or time.time())
    msg: Dict[str, Any] = {"role": "assistant", "content": None}
    tool_calls = []
    texts = []
    images = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in {"message", "output_text"}:
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"}:
                    texts.append(part.get("text") or "")
                elif part.get("type") in {"output_image", "input_image", "image_url"}:
                    url = part.get("image_url") or part.get("url") or ""
                    if isinstance(url, dict):
                        url = url.get("url") or ""
                    if url:
                        images.append({"type": "image_url", "image_url": {"url": url}})
            if item.get("text"):
                texts.append(str(item.get("text")))
        elif itype == "image_generation_call" and item.get("result"):
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{item.get('result')}"},
            })
        elif itype in {"web_search_call", "file_search_call", "code_interpreter_call", "computer_call", "mcp_call"}:
            bits = []
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            if action.get("query"):
                bits.append(str(action.get("query")))
            for bucket in ("outputs", "results"):
                values = item.get(bucket)
                if not isinstance(values, list):
                    continue
                for value in values[:8]:
                    if isinstance(value, str) and value:
                        bits.append(value)
                    elif isinstance(value, dict):
                        piece = value.get("text") or value.get("url") or value.get("filename") or value.get("logs") or ""
                        if piece:
                            bits.append(str(piece))
            if bits:
                texts.append("\n".join(bits))
        elif itype == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "tool",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
        elif itype == "reasoning":
            reasoning = _reasoning_item_text(item)
            if reasoning:
                msg["reasoning_content"] = reasoning
    if images:
        content_parts = []
        if texts:
            content_parts.append({"type": "text", "text": "".join(texts)})
        content_parts.extend(images)
        msg["content"] = content_parts
    elif texts:
        msg["content"] = "".join(texts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    usage = _usage_to_chat(resp.get("usage"))
    out = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": finish,
            }
        ],
    }
    if usage:
        out["usage"] = usage
    return out


class ResponsesToChatStream:
    """把 Responses SSE 转成 chat.completion.chunk SSE。"""

    def __init__(self, client_model: str = ""):
        self.client_model = client_model or "gpt-6"
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.tool_index: Dict[str, int] = {}
        self.saw_tool = False
        self._closed = False
        self.role_sent = False
        self.max_block_index = -1

    def _emit(
        self,
        delta: Optional[dict] = None,
        finish_reason: Optional[str] = None,
        usage: Optional[dict] = None,
    ) -> bytes:
        return _sse_data(
            _chat_chunk(
                chunk_id=self.chunk_id,
                model=self.client_model,
                created=self.created,
                delta=delta,
                finish_reason=finish_reason,
                usage=usage,
            )
        )

    def feed_event(self, obj: Optional[dict]) -> Iterable[bytes]:
        if not isinstance(obj, dict) or self._closed:
            return []
        typ = obj.get("type") or ""
        out: List[bytes] = []

        if typ in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}:
            delta = obj.get("delta") or ""
            if delta:
                if not self.role_sent:
                    out.append(self._emit(delta={"role": "assistant"}))
                    self.role_sent = True
                out.append(self._emit(delta={"reasoning_content": delta}))
            return out

        if typ == "response.output_text.delta":
            if not self.role_sent:
                out.append(self._emit(delta={"role": "assistant"}))
                self.role_sent = True
            delta = obj.get("delta") or ""
            if delta:
                out.append(self._emit(delta={"content": delta}))
            return out

        if typ == "response.output_item.added":
            item = obj.get("item") or {}
            if item.get("type") == "function_call":
                if not self.role_sent:
                    out.append(self._emit(delta={"role": "assistant"}))
                    self.role_sent = True
                idx = len(self.tool_index)
                item_id = item.get("id") or f"item_{idx}"
                self.tool_index[item_id] = idx
                self.saw_tool = True
                out.append(
                    self._emit(
                        delta={
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": item.get("call_id") or f"call_{uuid.uuid4().hex[:24]}",
                                    "type": "function",
                                    "function": {
                                        "name": item.get("name") or "tool",
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                    )
                )
            return out

        if typ == "response.function_call_arguments.delta":
            item_id = obj.get("item_id") or ""
            idx = self.tool_index.get(item_id, 0)
            delta = obj.get("delta") or ""
            if delta:
                out.append(
                    self._emit(
                        delta={
                            "tool_calls": [
                                {"index": idx, "function": {"arguments": delta}}
                            ]
                        }
                    )
                )
            return out

        if typ in {"response.completed", "response.incomplete"}:
            usage = _usage_to_chat((obj.get("response") or {}).get("usage"))
            finish = "tool_calls" if self.saw_tool else "stop"
            out.append(self._emit(finish_reason=finish, usage=usage))
            out.append(b"data: [DONE]\n\n")
            self._closed = True
            return out

        if typ == "error" or obj.get("error"):
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {"message": str(obj.get("error") or "error")}
            out.append(
                (
                    "data: "
                    + json.dumps(
                        {"error": {"message": err.get("message") or "upstream error", "type": "upstream_error"}},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
            )
            out.append(b"data: [DONE]\n\n")
            self._closed = True
            return out

        return out

    # 兼容 main 里 anthropic bridge 的方法名
    def feed_openai_event(self, obj: Optional[dict]) -> Iterable[bytes]:
        return self.feed_event(obj)

    def finish(self) -> Iterable[bytes]:
        if self._closed:
            return []
        finish = "tool_calls" if self.saw_tool else "stop"
        self._closed = True
        return [self._emit(finish_reason=finish), b"data: [DONE]\n\n"]
