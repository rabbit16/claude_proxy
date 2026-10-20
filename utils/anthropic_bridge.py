# -*- coding: utf-8 -*-
"""号商只提供 OpenAI 兼容接口。Claude Code 走 Anthropic /v1/messages，需在此转换。"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _sse(payload: dict, event: Optional[str] = None) -> bytes:
    data = json.dumps(payload, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {data}\n\n".encode("utf-8")
    return f"data: {data}\n\n".encode("utf-8")


# 思考正文在前，签名和脱敏块附在后缀里，方便 OpenAI 侧原样带回。
_REASONING_META = "\n\n<anth_reasoning_meta>"


def _text_with_citations(block: dict) -> str:
    text = block.get("text") or ""
    cites = block.get("citations")
    if not isinstance(cites, list):
        return text
    lines = []
    for cite in cites:
        if not isinstance(cite, dict):
            continue
        piece = " ".join(
            str(cite.get(key))
            for key in ("title", "url", "cited_text")
            if cite.get(key)
        )
        if piece:
            lines.append(f"[cite] {piece}")
    if not lines:
        return text
    return text + "\n" + "\n".join(lines)


def _web_result_text(block: dict) -> str:
    """把网页搜索、代码执行等服务端工具结果收成可读文本。"""
    lines = []
    content = block.get("content")
    items = content if isinstance(content, list) else [block]
    for item in items:
        if isinstance(item, str):
            if item:
                lines.append(item)
            continue
        if not isinstance(item, dict):
            continue
        nested = item.get("content")
        if item.get("type") in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result"} and isinstance(nested, list):
            lines.append(_web_result_text(item))
            continue
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = item.get("cited_text") or ""
        if not snippet and isinstance(item.get("text"), str):
            snippet = item.get("text") or ""
        piece = " ".join(part for part in (str(title), str(url), str(snippet)) if part)
        if piece:
            lines.append(piece)
            continue
        stdout = item.get("stdout") or item.get("stderr") or ""
        if isinstance(stdout, str) and stdout:
            lines.append(stdout)
    return "\n".join(line for line in lines if line)


def pack_thinking_blocks(blocks: list) -> str:
    texts: List[str] = []
    specs: List[dict] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "thinking":
            thinking = block.get("thinking") or ""
            texts.append(thinking)
            specs.append({
                "type": "thinking",
                "signature": block.get("signature") or "",
                "length": len(thinking),
            })
        elif block.get("type") == "redacted_thinking" and block.get("data"):
            specs.append({"type": "redacted_thinking", "data": block.get("data")})
    body = "".join(texts)
    if not any(spec.get("signature") or spec.get("data") for spec in specs):
        return body
    return body + _REASONING_META + json.dumps({"blocks": specs}, ensure_ascii=False)


def thinking_blocks_from_reasoning(raw: Any) -> List[dict]:
    if not isinstance(raw, str) or _REASONING_META not in raw:
        return []
    prefix, meta_raw = raw.rsplit(_REASONING_META, 1)
    try:
        meta = json.loads(meta_raw)
    except Exception:
        return []
    specs = meta.get("blocks") if isinstance(meta, dict) else None
    if not isinstance(specs, list):
        return []
    cursor = 0
    out: List[dict] = []
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        if spec.get("type") == "redacted_thinking" and spec.get("data"):
            out.append({"type": "redacted_thinking", "data": spec.get("data")})
            continue
        if spec.get("type") != "thinking":
            continue
        length = int(spec.get("length") or 0)
        text = prefix[cursor:cursor + length] if length else ""
        cursor += length
        if spec.get("signature"):
            out.append({
                "type": "thinking",
                "thinking": text,
                "signature": spec.get("signature"),
            })
    return out


def reasoning_meta_suffix(specs: list) -> str:
    usable = [
        spec for spec in specs or []
        if isinstance(spec, dict) and (spec.get("signature") or spec.get("data"))
    ]
    if not usable:
        return ""
    return _REASONING_META + json.dumps({"blocks": usable}, ensure_ascii=False)


def _text_from_blocks(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in {"text", "input_text", "output_text"}:
            parts.append(_text_with_citations(block) if btype == "text" else (block.get("text") or ""))
        elif btype in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result", "web_search_result"}:
            parts.append(_web_result_text(block))
        elif btype == "thinking":
            parts.append(block.get("thinking") or "")
        elif btype == "document":
            kind, value = _anthropic_document_payload(block)
            if kind == "text" and value:
                parts.append(value)
        elif btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                parts.append(_text_from_blocks(inner))
            elif inner is not None:
                parts.append(json.dumps(inner, ensure_ascii=False))
    return "".join(parts)


def _system_to_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    return _text_from_blocks(system)


def _openai_image_url(block: dict) -> Optional[dict]:
    source = block.get("source") or {}
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "base64":
        media = source.get("media_type") or "image/png"
        data = source.get("data") or ""
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media};base64,{data}"},
        }
    if stype == "url":
        return {
            "type": "image_url",
            "image_url": {"url": source.get("url") or ""},
        }
    return None


def _split_data_url(url: str) -> tuple:
    if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
        header, data = url.split(";base64,", 1)
        return header[5:] or "application/octet-stream", data
    return "", ""


def _anthropic_document_payload(block: dict) -> tuple:
    """Anthropic document → ('text', str) 或 ('file', openai file part)。"""
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    title = block.get("title") or block.get("name") or "document"
    stype = source.get("type")
    if stype == "text":
        body = source.get("data") or ""
        return "text", f"[{title}]\n{body}" if title else body
    if stype == "base64":
        media = source.get("media_type") or "application/octet-stream"
        filename = str(title)
        if "." not in filename:
            filename = f"{filename}.pdf" if media == "application/pdf" else filename
        return "file", {
            "type": "file",
            "file": {
                "filename": filename,
                "file_data": f"data:{media};base64,{source.get('data') or ''}",
            },
        }
    if stype == "url" and source.get("url"):
        return "text", f"[{title}] {source.get('url')}"
    if stype == "file" and source.get("file_id"):
        return "file", {
            "type": "file",
            "file": {"file_id": source.get("file_id"), "filename": title},
        }
    return "", ""


def openai_file_to_anthropic_document(part: dict) -> Optional[dict]:
    """OpenAI / Responses 的文件块 → Anthropic document。"""
    if not isinstance(part, dict):
        return None
    if part.get("type") == "document" and isinstance(part.get("source"), dict):
        return part
    file_obj = None
    if part.get("type") == "file" and isinstance(part.get("file"), dict):
        file_obj = part["file"]
    elif part.get("type") == "input_file":
        file_obj = part
    if not isinstance(file_obj, dict):
        return None
    title = file_obj.get("filename") or file_obj.get("title") or "document"
    if file_obj.get("file_id"):
        return {
            "type": "document",
            "title": title,
            "source": {"type": "file", "file_id": file_obj.get("file_id")},
        }
    raw = file_obj.get("file_data") or file_obj.get("file_url") or ""
    media, data = _split_data_url(raw) if isinstance(raw, str) else ("", "")
    if media and data:
        return {
            "type": "document",
            "title": title,
            "source": {"type": "base64", "media_type": media, "data": data},
        }
    if isinstance(raw, str) and raw.startswith(("http://", "https://")):
        return {
            "type": "document",
            "title": title,
            "source": {"type": "url", "url": raw},
        }
    return None


def _flush_text(parts: List[dict], text_bits: List[str]) -> None:
    if text_bits:
        parts.append({"type": "text", "text": "".join(text_bits)})
        text_bits.clear()


def _content_to_openai_parts(content: Any) -> Any:
    """字符串保持字符串；含图或文件时转 multipart content。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: List[dict] = []
    text_bits: List[str] = []
    has_rich = False
    for block in content:
        if isinstance(block, str):
            text_bits.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype in {"text", "input_text", "output_text"}:
            text_bits.append(_text_with_citations(block) if btype == "text" else (block.get("text") or ""))
        elif btype in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result", "web_search_result"}:
            text_bits.append(_web_result_text(block))
        elif btype == "refusal":
            text_bits.append(block.get("refusal") or "")
        elif btype == "input_audio":
            audio = block.get("input_audio") if isinstance(block.get("input_audio"), dict) else block
            if audio.get("data"):
                has_rich = True
                _flush_text(parts, text_bits)
                parts.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": audio.get("data") or "",
                        "format": audio.get("format") or "wav",
                    },
                })
        elif btype == "image":
            img = _openai_image_url(block)
            if img:
                has_rich = True
                _flush_text(parts, text_bits)
                parts.append(img)
            else:
                source = block.get("source") if isinstance(block.get("source"), dict) else {}
                if source.get("file_id"):
                    text_bits.append(f"[image file_id={source.get('file_id')}]")
        elif btype in {"image_url", "input_image"}:
            image_url = block.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else block.get("url") or ""
            if url:
                has_rich = True
                _flush_text(parts, text_bits)
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif btype == "document":
            kind, value = _anthropic_document_payload(block)
            if kind == "text" and value:
                text_bits.append(value)
            elif kind == "file" and isinstance(value, dict):
                has_rich = True
                _flush_text(parts, text_bits)
                parts.append(value)
        elif btype in {"file", "input_file"}:
            has_rich = True
            _flush_text(parts, text_bits)
            if btype == "file":
                parts.append(block)
            else:
                file_part = {"type": "file", "file": {}}
                for key in ("file_id", "filename", "file_data", "file_url"):
                    if block.get(key):
                        file_part["file"][key] = block.get(key)
                parts.append(file_part)
        elif btype == "tool_result":
            text_bits.append(_text_from_blocks([block]))
    if has_rich:
        _flush_text(parts, text_bits)
        return parts or ""
    return "".join(text_bits)


def anthropic_messages_to_openai(payload: dict) -> dict:
    """Anthropic Messages 请求 → OpenAI Chat Completions 请求。"""
    out: Dict[str, Any] = {
        "model": payload.get("model"),
        "stream": bool(payload.get("stream")),
    }
    if payload.get("max_tokens") is not None:
        out["max_tokens"] = payload.get("max_tokens")
    if payload.get("temperature") is not None:
        out["temperature"] = payload.get("temperature")
    if payload.get("top_p") is not None:
        out["top_p"] = payload.get("top_p")
    if payload.get("stop_sequences"):
        out["stop"] = payload.get("stop_sequences")
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        cleaned = {key: value for key, value in metadata.items() if isinstance(value, str)}
        if cleaned:
            out["metadata"] = cleaned

    messages: List[dict] = []
    system_text = _system_to_text(payload.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            text_parts = []
            tool_calls = []
            thinking_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(_text_with_citations(block))
                elif btype in {"thinking", "redacted_thinking"}:
                    thinking_blocks.append(block)
                elif btype in {"tool_use", "server_tool_use", "mcp_tool_use"}:
                    tool_calls.append(
                        {
                            "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "",
                                "arguments": json.dumps(
                                    block.get("input") if block.get("input") is not None else {},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    )
                elif btype in {"web_search_tool_result", "code_execution_tool_result", "mcp_tool_result"}:
                    extra = _web_result_text(block)
                    if extra:
                        text_parts.append(extra)
            item: Dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            packed = pack_thinking_blocks(thinking_blocks)
            if packed:
                item["reasoning_content"] = packed
            if tool_calls:
                item["tool_calls"] = tool_calls
            messages.append(item)
            continue

        if role == "user" and isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") in {"tool_result", "mcp_tool_result"} for b in content
        ):
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in {"tool_result", "mcp_tool_result"}:
                    continue
                inner = block.get("content")
                if isinstance(inner, list):
                    tool_content = _content_to_openai_parts(inner)
                elif inner is None:
                    tool_content = ""
                elif isinstance(inner, str):
                    tool_content = inner
                else:
                    tool_content = json.dumps(inner, ensure_ascii=False)
                if block.get("is_error"):
                    if isinstance(tool_content, list):
                        tool_content = [{"type": "text", "text": "[tool error]"}] + tool_content
                    else:
                        tool_content = "[tool error]\n" + str(tool_content or "")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id") or "",
                        "content": tool_content,
                    }
                )
            # 同一条 user 里夹带的纯文本仍保留
            leftover = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") in {
                    "text", "image", "document", "file", "input_file", "input_audio",
                    "web_search_tool_result", "code_execution_tool_result",
                }
            ]
            if leftover:
                messages.append({"role": "user", "content": _content_to_openai_parts(leftover)})
            continue

        messages.append({"role": role, "content": _content_to_openai_parts(content)})

    out["messages"] = messages

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        openai_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name") or "",
                        "description": tool.get("description") or "",
                        "parameters": tool.get("input_schema")
                        or tool.get("parameters")
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        if openai_tools:
            out["tools"] = openai_tools
            tool_choice = payload.get("tool_choice")
            if isinstance(tool_choice, dict):
                ttype = tool_choice.get("type")
                if ttype == "any":
                    out["tool_choice"] = "required"
                elif ttype == "auto":
                    out["tool_choice"] = "auto"
                elif ttype == "tool" and tool_choice.get("name"):
                    out["tool_choice"] = {
                        "type": "function",
                        "function": {"name": tool_choice["name"]},
                    }
            elif isinstance(tool_choice, str):
                out["tool_choice"] = tool_choice

    return out


def prepare_anthropic_upstream(
    body: bytes, parsed_body: dict, headers: dict
) -> Tuple[bytes, dict, dict, str]:
    """返回 (openai_body, openai_parsed, headers, upstream_path)。"""
    if not isinstance(parsed_body, dict):
        parsed_body = {}
    # body 可能已被换成号商 model，优先用 wire JSON
    wire = parsed_body
    try:
        loaded = json.loads((body or b"").decode(errors="ignore") or "{}")
        if isinstance(loaded, dict) and loaded.get("model"):
            wire = loaded
    except Exception:
        pass

    openai_req = anthropic_messages_to_openai(wire)
    new_body = json.dumps(openai_req, ensure_ascii=False).encode("utf-8")
    new_headers = dict(headers)
    # 去掉 Anthropic 专用头，避免上游误判
    for key in list(new_headers):
        lower = key.lower()
        if lower.startswith("anthropic-") or lower in {"x-api-key"}:
            # x-api-key 已在鉴权里写成号商 key；OpenAI 用 Authorization 即可
            if lower.startswith("anthropic-"):
                new_headers.pop(key, None)
    new_headers["content-type"] = "application/json"
    return new_body, openai_req, new_headers, "/v1/chat/completions"


def _map_finish_reason(reason: Optional[str], has_tools: bool) -> str:
    if reason == "tool_calls" or has_tools:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason in {"stop", "end_turn", None}:
        return "end_turn"
    return "end_turn"


def openai_completion_to_anthropic(payload: dict, *, client_model: str = "") -> dict:
    """OpenAI chat.completion → Anthropic message。"""
    choices = payload.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    content_blocks: List[dict] = thinking_blocks_from_reasoning(message.get("reasoning_content"))

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for part in text:
            if not isinstance(part, dict):
                continue
            document = openai_file_to_anthropic_document(part)
            if document:
                content_blocks.append(document)
                continue
            ptype = part.get("type")
            if ptype in {"text", "output_text", "input_text"}:
                content_blocks.append({"type": "text", "text": part.get("text") or ""})
            elif ptype == "image_url":
                image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
                url = image.get("url") or ""
                media, data = _split_data_url(url)
                if media and data:
                    content_blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media, "data": data},
                    })
                elif url:
                    content_blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            elif ptype == "refusal" and part.get("refusal"):
                content_blocks.append({"type": "text", "text": part.get("refusal")})
    if message.get("refusal") and not any(block.get("type") == "text" for block in content_blocks):
        content_blocks.append({"type": "text", "text": message.get("refusal")})

    tool_calls = message.get("tool_calls") or []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception:
            args = {"raw": raw_args}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name") or "",
                "input": args if isinstance(args, dict) else {"value": args},
            }
        )

    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    usage_in = payload.get("usage") or {}
    anthropic_usage = {
        "input_tokens": int(usage_in.get("prompt_tokens") or usage_in.get("input_tokens") or 0),
        "output_tokens": int(usage_in.get("completion_tokens") or usage_in.get("output_tokens") or 0),
    }
    details = usage_in.get("prompt_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        anthropic_usage["cache_read_input_tokens"] = int(details.get("cached_tokens") or 0)
    for key in ("cache_write_tokens", "cache_creation_input_tokens"):
        if usage_in.get(key) is not None:
            anthropic_usage["cache_creation_input_tokens"] = int(usage_in.get(key) or 0)
            break

    return {
        "id": payload.get("id") or f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": client_model or payload.get("model") or "",
        "content": content_blocks,
        "stop_reason": _map_finish_reason(choice.get("finish_reason"), bool(tool_calls)),
        "stop_sequence": None,
        "usage": anthropic_usage,
    }


class OpenAIToAnthropicStream:
    """把 OpenAI chat.completion.chunk SSE 转成 Anthropic Messages SSE。"""

    def __init__(self, *, client_model: str = ""):
        self.client_model = client_model
        self.msg_id = f"msg_{uuid.uuid4().hex}"
        self.started = False
        self.text_started = False
        self.text_index = 0
        self.tool_states: Dict[int, dict] = {}
        self.next_block_index = 0
        self.usage: Dict[str, int] = {}
        self.stop_reason = "end_turn"
        self._closed = False
        self._reasoning_buf = ""
        self._thinking_flushed = False

    def _flush_thinking(self) -> List[bytes]:
        if self._thinking_flushed or self.text_started or self.tool_states:
            return []
        blocks = thinking_blocks_from_reasoning(self._reasoning_buf)
        if not blocks:
            return []
        self._thinking_flushed = True
        events = self._ensure_message_start()
        for block in blocks:
            index = self.next_block_index
            self.next_block_index += 1
            if block.get("type") == "redacted_thinking":
                events.append(_sse(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "redacted_thinking", "data": block.get("data") or ""},
                    },
                    "content_block_start",
                ))
            else:
                events.append(_sse(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                    "content_block_start",
                ))
                if block.get("thinking"):
                    events.append(_sse(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": block.get("thinking") or ""},
                        },
                        "content_block_delta",
                    ))
                events.append(_sse(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": block.get("signature") or ""},
                    },
                    "content_block_delta",
                ))
            events.append(_sse({"type": "content_block_stop", "index": index}, "content_block_stop"))
        return events

    def _ensure_message_start(self) -> List[bytes]:
        if self.started:
            return []
        self.started = True
        return [
            _sse(
                {
                    "type": "message_start",
                    "message": {
                        "id": self.msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.client_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
                "message_start",
            ),
            _sse({"type": "ping"}, "ping"),
        ]

    def _ensure_text_block(self) -> List[bytes]:
        out = []
        if not self.text_started:
            self.text_started = True
            self.text_index = self.next_block_index
            self.next_block_index += 1
            out.append(
                _sse(
                    {
                        "type": "content_block_start",
                        "index": self.text_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                    "content_block_start",
                )
            )
        return out

    def feed_openai_event(self, obj: Optional[dict]) -> List[bytes]:
        if not obj or self._closed:
            return []
        events = self._ensure_message_start()

        usage = obj.get("usage")
        if isinstance(usage, dict):
            prompt = usage.get("prompt_tokens")
            completion = usage.get("completion_tokens")
            if prompt is not None:
                self.usage["input_tokens"] = int(prompt)
            if completion is not None:
                self.usage["output_tokens"] = int(completion)
            details = usage.get("prompt_tokens_details") or {}
            if isinstance(details, dict) and details.get("cached_tokens") is not None:
                self.usage["cache_read_input_tokens"] = int(details.get("cached_tokens") or 0)

        choices = obj.get("choices") or []
        if not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")

        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._reasoning_buf += reasoning

        content = delta.get("content")
        if isinstance(content, str) and content:
            events.extend(self._flush_thinking())
            events.extend(self._ensure_text_block())
            events.append(
                _sse(
                    {
                        "type": "content_block_delta",
                        "index": self.text_index,
                        "delta": {"type": "text_delta", "text": content},
                    },
                    "content_block_delta",
                )
            )

        if delta.get("tool_calls"):
            events.extend(self._flush_thinking())
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = int(tc.get("index") or 0)
            state = self.tool_states.get(idx)
            if state is None:
                block_index = self.next_block_index
                self.next_block_index += 1
                tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                state = {
                    "block_index": block_index,
                    "id": tool_id,
                    "name": name,
                    "arguments": "",
                }
                self.tool_states[idx] = state
                events.append(
                    _sse(
                        {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": name,
                                "input": {},
                            },
                        },
                        "content_block_start",
                    )
                )
            fn = tc.get("function") or {}
            if fn.get("name") and not state["name"]:
                state["name"] = fn["name"]
            chunk_args = fn.get("arguments")
            if isinstance(chunk_args, str) and chunk_args:
                state["arguments"] += chunk_args
                events.append(
                    _sse(
                        {
                            "type": "content_block_delta",
                            "index": state["block_index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": chunk_args,
                            },
                        },
                        "content_block_delta",
                    )
                )

        if finish:
            self.stop_reason = _map_finish_reason(finish, bool(self.tool_states))
            events.extend(self.finish())
        return events

    def finish(self) -> List[bytes]:
        if self._closed:
            return []
        events = self._flush_thinking()
        self._closed = True
        events.extend(self._ensure_message_start())
        if self.text_started:
            events.append(
                _sse({"type": "content_block_stop", "index": self.text_index}, "content_block_stop")
            )
        for state in sorted(self.tool_states.values(), key=lambda s: s["block_index"]):
            events.append(
                _sse(
                    {"type": "content_block_stop", "index": state["block_index"]},
                    "content_block_stop",
                )
            )
        usage = {
            "input_tokens": int(self.usage.get("input_tokens") or 0),
            "output_tokens": int(self.usage.get("output_tokens") or 0),
        }
        if "cache_read_input_tokens" in self.usage:
            usage["cache_read_input_tokens"] = self.usage["cache_read_input_tokens"]
        events.append(
            _sse(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": usage["output_tokens"]},
                },
                "message_delta",
            )
        )
        # 把完整 usage 挂到 message_delta 之外的兼容字段，供 extract_usage
        # extract_usage 认 message_delta.usage；补 input 到同一事件不够（规范只给 output）。
        # 额外发一个带完整 usage 的 message 风格字段：修改 message_delta usage。
        events[-1] = _sse(
            {
                "type": "message_delta",
                "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                "usage": usage,
            },
            "message_delta",
        )
        events.append(_sse({"type": "message_stop"}, "message_stop"))
        return events

    @property
    def max_block_index(self) -> int:
        return max(0, self.next_block_index - 1)
