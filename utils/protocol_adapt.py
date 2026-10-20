"""按模型协议规则改写请求和响应。

客户端协议和规则不一致时，先把请求转成规则指定的协议再转发，
响应再转回客户端原来的协议。图片、音频、Gemini 原生接口不在这里转换。
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable, List, Optional

from utils.anthropic_bridge import (
    OpenAIToAnthropicStream,
    _content_to_openai_parts,
    _text_with_citations,
    _web_result_text,
    anthropic_messages_to_openai,
    openai_completion_to_anthropic,
    openai_file_to_anthropic_document,
    pack_thinking_blocks,
    reasoning_meta_suffix,
    thinking_blocks_from_reasoning,
)
from utils.openai_responses_bridge import (
    ResponsesToChatStream,
    _chat_chunk,
    _content_to_text,
    _sse_data,
    chat_completions_to_responses_request,
    responses_to_chat_completion,
    _reasoning_item_text,
)

CHAT_PROTOCOLS = ("openai", "anthropic", "responses")
UPSTREAM_PATHS = {
    "openai": "/v1/chat/completions",
    "anthropic": "/v1/messages",
    "responses": "/v1/responses",
}


def _openai_content_to_anthropic(content: Any) -> Any:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    blocks = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        document = openai_file_to_anthropic_document(part)
        if document:
            blocks.append(document)
            continue
        if ptype in {"text", "input_text", "output_text"}:
            blocks.append({"type": "text", "text": _text_with_citations(part) if ptype == "text" else (part.get("text") or "")})
        elif ptype in {"image_url", "input_image"}:
            image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            url = image.get("url") or part.get("url") or ""
            if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
                header, data = url.split(";base64,", 1)
                media = header[5:] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data},
                })
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif ptype == "input_audio":
            audio = part.get("input_audio") if isinstance(part.get("input_audio"), dict) else {}
            fmt = audio.get("format") or "audio"
            blocks.append({"type": "text", "text": f"[audio:{fmt}]"})
        elif ptype == "refusal" and part.get("refusal"):
            blocks.append({"type": "text", "text": part.get("refusal")})
        elif ptype in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result"}:
            extra = _web_result_text(part)
            if extra:
                blocks.append({"type": "text", "text": extra})
        else:
            blob = json.dumps(part, ensure_ascii=False)
            if len(blob) > 2000:
                blob = json.dumps({"type": ptype or "unknown"}, ensure_ascii=False)
            blocks.append({"type": "text", "text": blob})
    return blocks or ""


def openai_chat_to_anthropic(payload: dict) -> dict:
    """OpenAI Chat Completions 请求 → Anthropic Messages 请求。"""
    max_tokens = payload.get("max_tokens")
    if max_tokens is None:
        max_tokens = payload.get("max_completion_tokens")
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "max_tokens": int(max_tokens or 4096),
        "stream": bool(payload.get("stream")),
    }
    if payload.get("temperature") is not None:
        out["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        out["top_p"] = payload.get("top_p")
    if payload.get("stop") is not None:
        stop = payload.get("stop")
        out["stop_sequences"] = stop if isinstance(stop, list) else [stop]

    messages: List[dict] = []
    system_parts: List[str] = []
    pending_tools: List[dict] = []

    def flush_tools() -> None:
        if not pending_tools:
            return
        messages.append({"role": "user", "content": list(pending_tools)})
        pending_tools.clear()

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        if role in {"system", "developer"}:
            flush_tools()
            text = _content_to_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            raw_content = msg.get("content")
            if isinstance(raw_content, list):
                tool_content = _openai_content_to_anthropic(raw_content)
            else:
                tool_content = _content_to_text(raw_content)
            is_error = False
            if isinstance(tool_content, str) and tool_content.startswith("[tool error]\n"):
                is_error = True
                tool_content = tool_content[len("[tool error]\n"):]
            elif (
                isinstance(tool_content, list)
                and tool_content
                and isinstance(tool_content[0], dict)
                and tool_content[0].get("text") == "[tool error]"
            ):
                is_error = True
                tool_content = tool_content[1:] or ""
            tool_block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                "content": tool_content if tool_content is not None else "",
            }
            if is_error:
                tool_block["is_error"] = True
            pending_tools.append(tool_block)
            continue
        flush_tools()
        thinking_blocks = thinking_blocks_from_reasoning(msg.get("reasoning_content")) if role == "assistant" else []
        if role == "assistant" and (msg.get("tool_calls") or thinking_blocks):
            blocks = list(thinking_blocks)
            content = msg.get("content")
            if isinstance(content, list):
                converted = _openai_content_to_anthropic(content)
                if isinstance(converted, list):
                    blocks.extend(converted)
                elif converted:
                    blocks.append({"type": "text", "text": str(converted)})
            else:
                text = content if isinstance(content, str) else _content_to_text(content)
                if text:
                    blocks.append({"type": "text", "text": text})
            if msg.get("refusal") and not any(isinstance(block, dict) and block.get("type") == "text" for block in blocks):
                blocks.append({"type": "text", "text": msg.get("refusal")})
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {"raw": raw_args}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": fn.get("name") or "",
                    "input": args if isinstance(args, dict) else {"value": args},
                })
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            messages.append({"role": "assistant", "content": blocks})
            continue
        messages.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": _openai_content_to_anthropic(msg.get("content")),
        })
    flush_tools()
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_id"):
        out["metadata"] = {"user_id": str(metadata.get("user_id"))}
    if payload.get("top_k") is not None:
        out["top_k"] = payload.get("top_k")
    out["messages"] = messages

    tools = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if tool.get("type") == "function" and fn:
            tools.append({
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif tool.get("name") and tool.get("input_schema"):
            tools.append(tool)
    if tools:
        out["tools"] = tools
        choice = payload.get("tool_choice")
        if choice == "required":
            out["tool_choice"] = {"type": "any"}
        elif choice in {"auto", "none"}:
            out["tool_choice"] = {"type": choice}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            out["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        if payload.get("parallel_tool_calls") is False:
            current = out.get("tool_choice")
            if not isinstance(current, dict):
                current = {"type": "none"} if current == "none" else {"type": "auto"}
            else:
                current = dict(current)
            current["disable_parallel_tool_use"] = True
            out["tool_choice"] = current
    return out


def _responses_tools_to_chat(tools: list) -> list:
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        if isinstance(tool.get("function"), dict):
            out.append(tool)
            continue
        if tool.get("name"):
            out.append({
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description") or "",
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            })
    return out


def responses_request_to_chat(payload: dict) -> dict:
    """Responses 请求 → OpenAI Chat Completions 请求。"""
    messages: List[dict] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    raw = payload.get("input")
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        pending_calls: List[dict] = []
        pending_reasoning: List[str] = []

        def flush_calls() -> None:
            if not pending_calls and not pending_reasoning:
                return
            if (
                pending_calls
                and messages
                and messages[-1].get("role") == "assistant"
                and not messages[-1].get("tool_calls")
            ):
                messages[-1]["tool_calls"] = list(pending_calls)
                if pending_reasoning and not messages[-1].get("reasoning_content"):
                    messages[-1]["reasoning_content"] = "".join(pending_reasoning)
                pending_calls.clear()
                pending_reasoning.clear()
                return
            if pending_calls:
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": list(pending_calls),
                }
                if pending_reasoning:
                    assistant["reasoning_content"] = "".join(pending_reasoning)
                messages.append(assistant)
            elif pending_reasoning:
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "".join(pending_reasoning),
                })
            pending_calls.clear()
            pending_reasoning.clear()

        for item in raw:
            if isinstance(item, str):
                flush_calls()
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "reasoning":
                text = _reasoning_item_text(item)
                if text:
                    pending_reasoning.append(text)
                continue
            if itype == "function_call":
                pending_calls.append({
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "tool",
                        "arguments": item.get("arguments") or "{}",
                    },
                })
                continue
            role = item.get("role") or "user"
            if role not in {"system", "developer", "user", "assistant"}:
                role = "user"
            if pending_calls or (pending_reasoning and role != "assistant"):
                flush_calls()
            if itype == "function_call_output":
                output = item.get("output")
                content = _content_to_openai_parts(output) if isinstance(output, list) else _content_to_text(output)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
                continue
            message = {"role": role, "content": _content_to_openai_parts(item.get("content"))}
            if role == "assistant" and pending_reasoning:
                message["reasoning_content"] = "".join(pending_reasoning)
                pending_reasoning.clear()
            messages.append(message)
        flush_calls()
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "stream": bool(payload.get("stream")),
    }
    tools = _responses_tools_to_chat(payload.get("tools") or [])
    if tools:
        out["tools"] = tools
    if payload.get("tool_choice") is not None:
        choice = payload.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function" and choice.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        else:
            out["tool_choice"] = choice
    max_out = payload.get("max_output_tokens")
    if max_out is not None:
        out["max_tokens"] = max_out
    for key in (
        "temperature", "top_p", "user", "metadata", "service_tier", "seed",
        "frequency_penalty", "presence_penalty",
    ):
        if payload.get(key) is not None:
            out[key] = payload.get(key)
    if payload.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = payload.get("parallel_tool_calls")
    text_cfg = payload.get("text")
    if isinstance(text_cfg, dict) and isinstance(text_cfg.get("format"), dict):
        fmt = text_cfg["format"]
        if fmt.get("type") == "json_schema":
            out["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": fmt.get("name") or "response",
                    "schema": fmt.get("schema") or {"type": "object"},
                    "strict": bool(fmt.get("strict")),
                },
            }
        elif fmt.get("type") == "json_object":
            out["response_format"] = {"type": "json_object"}
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning.get("effort")
    return out


def anthropic_message_to_openai(payload: dict, *, client_model: str = "") -> dict:
    """Anthropic message 响应 → OpenAI chat.completion。"""
    blocks = payload.get("content") if isinstance(payload.get("content"), list) else []
    texts = []
    tool_calls = []
    thinking = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(_text_with_citations(block))
        elif btype in {"thinking", "redacted_thinking"}:
            thinking.append(block)
        elif btype in {"tool_use", "server_tool_use", "mcp_tool_use"}:
            tool_calls.append({
                "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": block.get("name") or "",
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
        elif btype in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result"}:
            extra = _web_result_text(block)
            if extra:
                texts.append(extra)
    stop = payload.get("stop_reason") or "end_turn"
    finish = "tool_calls" if tool_calls or stop == "tool_use" else "length" if stop == "max_tokens" else "stop"
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    chat_usage = None
    if usage.get("input_tokens") is not None or usage.get("output_tokens") is not None:
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        chat_usage = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
        if usage.get("cache_read_input_tokens") is not None:
            chat_usage["prompt_tokens_details"] = {"cached_tokens": int(usage.get("cache_read_input_tokens") or 0)}
        if usage.get("cache_creation_input_tokens") is not None:
            chat_usage["cache_creation_input_tokens"] = int(usage.get("cache_creation_input_tokens") or 0)
    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
    packed = pack_thinking_blocks(thinking)
    if packed:
        message["reasoning_content"] = packed
    if tool_calls:
        message["tool_calls"] = tool_calls
    result = {
        "id": payload.get("id") or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": client_model or payload.get("model") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if chat_usage:
        result["usage"] = chat_usage
    return result


def chat_completion_to_responses(resp: dict, client_model: str = "") -> dict:
    """非流式 chat.completion → Responses。"""
    choice = ((resp.get("choices") or [{}])[0]) if isinstance(resp.get("choices"), list) else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    output = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append({
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    text = _content_to_text(message.get("content"))
    if text:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        output.append({
            "type": "function_call",
            "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "name": fn.get("name") or "tool",
            "arguments": fn.get("arguments") or "{}",
        })
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    responses_usage = None
    if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
        responses_usage = {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
    out = {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "model": client_model or resp.get("model") or "",
        "status": "completed",
        "output": output,
    }
    if responses_usage:
        out["usage"] = responses_usage
    return out


def _to_openai(payload: dict, upstream: str, client_model: str) -> dict:
    if upstream == "openai":
        return payload
    if upstream == "anthropic":
        return anthropic_message_to_openai(payload, client_model=client_model)
    if upstream == "responses":
        return responses_to_chat_completion(payload, client_model=client_model)
    return payload


def _from_openai(payload: dict, client: str, client_model: str) -> dict:
    if client == "openai":
        return payload
    if client == "anthropic":
        return openai_completion_to_anthropic(payload, client_model=client_model)
    if client == "responses":
        return chat_completion_to_responses(payload, client_model=client_model)
    return payload


def convert_request(payload: dict, src: str, dst: str) -> dict:
    if src == dst:
        return dict(payload)
    current = payload
    proto = src
    if proto == "anthropic":
        current = anthropic_messages_to_openai(current)
        proto = "openai"
    elif proto == "responses":
        current = responses_request_to_chat(current)
        proto = "openai"
    if dst == "openai" or proto == dst:
        return current
    if dst == "anthropic":
        return openai_chat_to_anthropic(current)
    if dst == "responses":
        return chat_completions_to_responses_request(current)
    return current


def convert_upstream_response(payload: dict, upstream: str, client: str, client_model: str = "") -> dict:
    if not isinstance(payload, dict) or upstream == client:
        return payload
    return _from_openai(_to_openai(payload, upstream, client_model), client, client_model)


def convert_upstream_error(payload: dict, client: str) -> dict:
    err = payload.get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {}
    if isinstance(payload, dict) and payload.get("type") == "error" and isinstance(payload.get("error"), dict):
        err = payload["error"]
    message = (err.get("message") if isinstance(err, dict) else None) or "upstream error"
    etype = (err.get("type") or err.get("code") or "api_error") if isinstance(err, dict) else "api_error"
    detail = {}
    if isinstance(err, dict):
        for key in ("param", "code"):
            if err.get(key):
                detail[key] = err.get(key)
    if client == "anthropic":
        body = {"type": etype, "message": message}
        body.update(detail)
        return {"type": "error", "error": body}
    body = {"message": message, "type": etype}
    body.update(detail)
    return {"error": body}


def prepare_protocol_upstream(parsed_body: dict, headers: dict, src: str, dst: str):
    """返回 (body, parsed, headers, upstream_path)。"""
    converted = convert_request(parsed_body if isinstance(parsed_body, dict) else {}, src, dst)
    new_body = json.dumps(converted, ensure_ascii=False).encode("utf-8")
    new_headers = dict(headers or {})
    new_headers["content-type"] = "application/json"
    if dst != "anthropic":
        for key in list(new_headers):
            if key.lower().startswith("anthropic-"):
                new_headers.pop(key, None)
    return new_body, converted, new_headers, UPSTREAM_PATHS[dst]


def _json_from_sse(chunks: Iterable[bytes]) -> List[dict]:
    found = []
    for chunk in chunks:
        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data = stripped[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict):
                found.append(obj)
    return found


class AnthropicToOpenAIStream:
    """Anthropic Messages SSE → chat.completion.chunk SSE。"""

    def __init__(self, client_model: str = ""):
        self.client_model = client_model
        self.chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.role_sent = False
        self.tool_ids: dict[int, int] = {}
        self.next_tool = 0
        self.saw_tool = False
        self._closed = False
        self.usage: dict = {}
        self.max_block_index = -1
        self._think_text = ""
        self._think_specs: list = []
        self._think_open = None
        self._think_sig = ""

    def _emit(self, delta=None, finish_reason=None, usage=None) -> bytes:
        return _sse_data(_chat_chunk(
            chunk_id=self.chunk_id,
            model=self.client_model,
            created=self.created,
            delta=delta,
            finish_reason=finish_reason,
            usage=usage,
        ))

    def feed_event(self, obj: Optional[dict]) -> List[bytes]:
        if not isinstance(obj, dict) or self._closed:
            return []
        typ = obj.get("type") or ""
        out: List[bytes] = []
        if typ == "message_start" and not self.role_sent:
            self.role_sent = True
            out.append(self._emit(delta={"role": "assistant"}))
            return out
        if typ == "content_block_start":
            block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
            if block.get("type") == "thinking":
                self._think_open = len(self._think_text)
                self._think_sig = ""
                return out
            if block.get("type") == "redacted_thinking" and block.get("data"):
                self._think_specs.append({"type": "redacted_thinking", "data": block.get("data")})
                return out
            if block.get("type") in {"tool_use", "server_tool_use", "mcp_tool_use"}:
                if not self.role_sent:
                    self.role_sent = True
                    out.append(self._emit(delta={"role": "assistant"}))
                idx = self.next_tool
                self.next_tool += 1
                self.tool_ids[int(obj.get("index") or idx)] = idx
                self.saw_tool = True
                self.max_block_index = max(self.max_block_index, idx)
                out.append(self._emit(delta={"tool_calls": [{
                    "index": idx,
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {"name": block.get("name") or "", "arguments": ""},
                }]}))
            return out
        if typ == "content_block_delta":
            delta = obj.get("delta") if isinstance(obj.get("delta"), dict) else {}
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                piece = delta.get("thinking") or ""
                self._think_text += piece
                if not self.role_sent:
                    self.role_sent = True
                    out.append(self._emit(delta={"role": "assistant"}))
                out.append(self._emit(delta={"reasoning_content": piece}))
                return out
            if delta.get("type") == "signature_delta" and delta.get("signature"):
                self._think_sig += delta.get("signature") or ""
                return out
            if delta.get("type") == "text_delta" and delta.get("text"):
                if not self.role_sent:
                    self.role_sent = True
                    out.append(self._emit(delta={"role": "assistant"}))
                out.append(self._emit(delta={"content": delta.get("text") or ""}))
            elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                idx = self.tool_ids.get(int(obj.get("index") or 0), 0)
                out.append(self._emit(delta={"tool_calls": [{
                    "index": idx,
                    "function": {"arguments": delta.get("partial_json") or ""},
                }]}))
            return out
        if typ == "content_block_stop" and self._think_open is not None:
            length = len(self._think_text) - int(self._think_open)
            self._think_specs.append({
                "type": "thinking",
                "signature": self._think_sig,
                "length": length,
            })
            self._think_open = None
            self._think_sig = ""
            return out
        if typ == "message_delta":
            usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
            if usage:
                self.usage = usage
            stop = (obj.get("delta") or {}).get("stop_reason") if isinstance(obj.get("delta"), dict) else None
            if stop:
                self._stop = stop
            return out
        if typ == "message_stop":
            return self.finish()
        if typ == "error" or obj.get("error"):
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
            out.append(_sse_data({"error": {"message": err.get("message") or "upstream error"}}))
            out.append(b"data: [DONE]\n\n")
            self._closed = True
        return out

    def finish(self) -> List[bytes]:
        if self._closed:
            return []
        if self._think_open is not None:
            length = len(self._think_text) - int(self._think_open)
            self._think_specs.append({
                "type": "thinking",
                "signature": self._think_sig,
                "length": length,
            })
            self._think_open = None
        suffix = reasoning_meta_suffix(self._think_specs)
        self._closed = True
        stop = getattr(self, "_stop", None)
        finish = "tool_calls" if self.saw_tool or stop == "tool_use" else "length" if stop == "max_tokens" else "stop"
        usage = None
        if self.usage:
            prompt = int(self.usage.get("input_tokens") or 0)
            completion = int(self.usage.get("output_tokens") or 0)
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
        chunks = []
        if suffix:
            chunks.append(self._emit(delta={"reasoning_content": suffix}))
        chunks.extend([self._emit(finish_reason=finish, usage=usage), b"data: [DONE]\n\n"])
        return chunks

    def feed_openai_event(self, obj: Optional[dict]) -> List[bytes]:
        return self.feed_event(obj)


class ChatToResponsesStream:
    """chat.completion.chunk SSE → Responses SSE。"""

    def __init__(self, client_model: str = ""):
        self.client_model = client_model
        self._closed = False
        self.saw_tool = False
        self.usage = None
        self.max_block_index = -1

    def _emit(self, payload: dict) -> bytes:
        return _sse_data(payload)

    def feed_event(self, obj: Optional[dict]) -> List[bytes]:
        if not isinstance(obj, dict) or self._closed:
            return []
        if obj.get("error"):
            self._closed = True
            return [self._emit({"type": "error", "error": obj.get("error")})]
        out: List[bytes] = []
        choices = obj.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if isinstance(delta.get("reasoning_content"), str) and delta.get("reasoning_content"):
            out.append(self._emit({
                "type": "response.reasoning_summary_text.delta",
                "delta": delta.get("reasoning_content"),
            }))
        if isinstance(delta.get("content"), str) and delta.get("content"):
            out.append(self._emit({"type": "response.output_text.delta", "delta": delta.get("content")}))
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            if call.get("id") or fn.get("name"):
                self.saw_tool = True
                out.append(self._emit({
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "id": call.get("id") or f"item_{call.get('index') or 0}",
                        "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "name": fn.get("name") or "tool",
                    },
                }))
            if fn.get("arguments"):
                out.append(self._emit({
                    "type": "response.function_call_arguments.delta",
                    "item_id": call.get("id") or "",
                    "delta": fn.get("arguments") or "",
                }))
        if obj.get("usage"):
            self.usage = obj.get("usage")
        if choice.get("finish_reason"):
            out.extend(self.finish())
        return out

    def finish(self) -> List[bytes]:
        if self._closed:
            return []
        self._closed = True
        usage = None
        if isinstance(self.usage, dict):
            usage = {
                "input_tokens": int(self.usage.get("prompt_tokens") or self.usage.get("input_tokens") or 0),
                "output_tokens": int(self.usage.get("completion_tokens") or self.usage.get("output_tokens") or 0),
            }
        return [self._emit({
            "type": "response.completed",
            "response": {
                "id": f"resp_{uuid.uuid4().hex[:24]}",
                "object": "response",
                "model": self.client_model,
                "status": "completed",
                "usage": usage,
            },
        })]

    def feed_openai_event(self, obj: Optional[dict]) -> List[bytes]:
        return self.feed_event(obj)


class ProtocolStreamBridge:
    """上游协议的 SSE 事件转成客户端协议。已有的两条桥不走这里。"""

    def __init__(self, upstream: str, client: str, client_model: str = ""):
        self.upstream = upstream
        self.client = client
        self._closed = False
        self._anth_to_oai = AnthropicToOpenAIStream(client_model)
        self._chat_to_resp = ChatToResponsesStream(client_model)
        self._resp_to_chat = ResponsesToChatStream(client_model)
        self._oai_to_anth = OpenAIToAnthropicStream(client_model=client_model)

    @property
    def max_block_index(self) -> int:
        return max(
            self._anth_to_oai.max_block_index,
            self._oai_to_anth.max_block_index,
            self._resp_to_chat.max_block_index,
            self._chat_to_resp.max_block_index,
        )

    def _chain(self, first: Iterable[bytes], second_feed) -> List[bytes]:
        out: List[bytes] = []
        for obj in _json_from_sse(first):
            out.extend(second_feed(obj))
        return out

    def feed_openai_event(self, obj: Optional[dict]) -> List[bytes]:
        if self._closed or not isinstance(obj, dict):
            return []
        if self.upstream == "anthropic" and self.client == "openai":
            chunks = self._anth_to_oai.feed_event(obj)
        elif self.upstream == "openai" and self.client == "responses":
            chunks = self._chat_to_resp.feed_event(obj)
        elif self.upstream == "responses" and self.client == "anthropic":
            chunks = self._chain(self._resp_to_chat.feed_event(obj), self._oai_to_anth.feed_openai_event)
        elif self.upstream == "anthropic" and self.client == "responses":
            chunks = self._chain(self._anth_to_oai.feed_event(obj), self._chat_to_resp.feed_event)
        else:
            chunks = []
        if self._anth_to_oai._closed or self._chat_to_resp._closed or self._resp_to_chat._closed or self._oai_to_anth._closed:
            self._closed = True
        return list(chunks)

    def finish(self) -> List[bytes]:
        if self._closed:
            return []
        self._closed = True
        if self.upstream == "anthropic" and self.client == "openai":
            return self._anth_to_oai.finish()
        if self.upstream == "openai" and self.client == "responses":
            return self._chat_to_resp.finish()
        if self.upstream == "responses" and self.client == "anthropic":
            return self._chain(self._resp_to_chat.finish(), self._oai_to_anth.feed_openai_event) + self._oai_to_anth.finish()
        if self.upstream == "anthropic" and self.client == "responses":
            return self._chain(self._anth_to_oai.finish(), self._chat_to_resp.feed_event) + self._chat_to_resp.finish()
        return []
