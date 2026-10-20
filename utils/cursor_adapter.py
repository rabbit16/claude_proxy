# -*- coding: utf-8 -*-
"""Cursor 专用适配：仅影响 Cursor chat/completions，不影响 Claude Code / Codex。"""
from __future__ import annotations

import json
import os
from typing import Optional

from utils.common_utils import (
    _replace_json_request_body,
    _runtime_flags,
    model_prefers_max_completion_tokens,
)
from utils.get_log import process_log


_CURSOR_CHAT_DROP_KEYS = {
    "prompt_cache_key",
    "prompt_cache_retention",
    "safety_identifier",
    "stream_options",
    "service_tier",
    "store",
}

_CURSOR_TOOL_HINTS = {
    "ReadFile",
    "ApplyPatch",
    "AskQuestion",
    "GetDynamicTools",
    "CallDynamicTool",
    "rg",
    "EditNotebook",
    "Delete",
    "TodoWrite",
}


def is_cursor_client(headers: Optional[dict], parsed_body: Optional[dict]) -> bool:
    """识别 Cursor（含云端 Agent）。Claude Code / Codex 不应命中。"""
    headers = headers or {}
    ua = (headers.get("user-agent") or "").lower()
    if "cursor" in ua:
        return True
    for key in headers:
        if key.lower().startswith("x-cursor"):
            return True

    body = parsed_body or {}
    tools = body.get("tools") or []
    names = set()
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "custom" and tool.get("name"):
                names.add(str(tool.get("name")))
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            if fn.get("name"):
                names.add(str(fn["name"]))
    if names & _CURSOR_TOOL_HINTS:
        return True

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        tid = msg.get("tool_call_id")
        if isinstance(tid, str) and "\n" in tid:
            return True
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and isinstance(tc.get("id"), str) and "\n" in tc["id"]:
                return True
    return False


# 号商对 tool_call id 长度敏感（Cursor 的 call-...\nfc_... 会直接 400）。
_MAX_TOOL_CALL_ID_LEN = 40


def _short_tool_call_id(seq: int) -> str:
    return f"call_{seq}"


def _strip_schema_noise(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in {"$schema", "$id", "$defs", "definitions", "strict"}:
                continue
            if key == "const":
                out["enum"] = [value]
                continue
            out[key] = _strip_schema_noise(value)
        return out
    if isinstance(node, list):
        return [_strip_schema_noise(x) for x in node]
    return node


def _convert_custom_tool_to_function(tool: dict) -> dict:
    """Cursor ApplyPatch 等 type=custom → function，号商 chat/completions 才认。"""
    name = tool.get("name") or "custom_tool"
    description = tool.get("description") or name
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Tool input payload (e.g. patch text)",
                    }
                },
                "required": ["input"],
            },
        },
    }


def _sanitize_cursor_tools(tools):
    if not isinstance(tools, list):
        return tools, False
    changed = False
    cleaned = []
    for tool in tools:
        if not isinstance(tool, dict):
            cleaned.append(tool)
            continue
        if tool.get("type") == "custom":
            cleaned.append(_convert_custom_tool_to_function(tool))
            changed = True
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


def _sanitize_cursor_messages(messages):
    if not isinstance(messages, list):
        return messages, False
    changed = False
    out = []
    id_map: dict[str, str] = {}
    seq = 0

    def map_id(tid: str) -> str:
        nonlocal seq, changed
        if not isinstance(tid, str) or not tid:
            return tid
        if tid in id_map:
            return id_map[tid]
        needs_short = (
            "\n" in tid
            or len(tid) > _MAX_TOOL_CALL_ID_LEN
            or any(ch in tid for ch in " \t\r")
        )
        if needs_short:
            seq += 1
            nid = _short_tool_call_id(seq)
            id_map[tid] = nid
            changed = True
            return nid
        id_map[tid] = tid
        return tid

    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        item = dict(msg)
        # Cursor 私有字段，上游不认
        if "modelProviderMessageId" in item:
            item.pop("modelProviderMessageId", None)
            changed = True
        # Codex/Cursor 的 developer → system（DeepSeek 等上游不认 developer）
        if item.get("role") == "developer":
            item["role"] = "system"
            changed = True
        if item.get("content") is None and not item.get("tool_calls"):
            item["content"] = ""
            changed = True
        if item.get("name") == "":
            item.pop("name", None)
            changed = True
        tid = item.get("tool_call_id")
        if isinstance(tid, str):
            new_tid = map_id(tid)
            if new_tid != tid:
                item["tool_call_id"] = new_tid
        tool_calls = item.get("tool_calls")
        if isinstance(tool_calls, list):
            fixed = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    fixed.append(call)
                    continue
                c2 = dict(call)
                if c2.get("type") == "custom":
                    c2["type"] = "function"
                    if "function" not in c2:
                        raw_input = c2.get("input")
                        if isinstance(raw_input, str):
                            args = raw_input
                        else:
                            args = json.dumps(raw_input or {}, ensure_ascii=False)
                        c2["function"] = {
                            "name": c2.get("name") or "custom_tool",
                            "arguments": args,
                        }
                    changed = True
                cid = c2.get("id")
                if isinstance(cid, str):
                    new_cid = map_id(cid)
                    if new_cid != cid:
                        c2["id"] = new_cid
                if "index" in c2:
                    c2.pop("index", None)
                    changed = True
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


def sanitize_cursor_openai_request(body: bytes, parsed_body: dict) -> tuple:
    """仅清洗 Cursor 的 chat/completions。"""
    if not isinstance(parsed_body, dict):
        return body, parsed_body

    updated = dict(parsed_body)
    changed = False

    for key in list(_CURSOR_CHAT_DROP_KEYS):
        if key in updated:
            updated.pop(key, None)
            changed = True

    # 号商多认 max_tokens；官方 OpenAI（经 LiteLLM）对 gpt-5/6、o 系列要求 max_completion_tokens。
    teamo, yunxin = _runtime_flags()
    prefer_mct = (not yunxin and not teamo) and model_prefers_max_completion_tokens(
        updated.get("model") or ""
    )
    has_max = updated.get("max_tokens") is not None
    has_mct = updated.get("max_completion_tokens") is not None
    if prefer_mct:
        if has_max and has_mct:
            updated.pop("max_tokens", None)
            changed = True
        elif has_max and not has_mct:
            updated["max_completion_tokens"] = updated.pop("max_tokens")
            changed = True
        elif not has_max and not has_mct and updated.get("tools"):
            updated["max_completion_tokens"] = 16384
            changed = True
    else:
        if has_max and has_mct:
            updated.pop("max_completion_tokens", None)
            changed = True
        elif has_mct and not has_max:
            updated["max_tokens"] = updated.pop("max_completion_tokens")
            changed = True
        elif not has_max and not has_mct and updated.get("tools"):
            updated["max_tokens"] = 16384
            changed = True

    if "tools" in updated:
        tools, tools_changed = _sanitize_cursor_tools(updated.get("tools"))
        if tools_changed:
            updated["tools"] = tools
            changed = True

    if "messages" in updated:
        messages, msg_changed = _sanitize_cursor_messages(updated.get("messages"))
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


def dump_cursor_request_debug(req_id: str, parsed_body: dict, body: bytes) -> None:
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"openai_req_{req_id}.json")
        summary = {
            "keys": sorted(parsed_body.keys()) if isinstance(parsed_body, dict) else [],
            "model": (parsed_body or {}).get("model"),
            "stream": (parsed_body or {}).get("stream"),
            "max_tokens": (parsed_body or {}).get("max_tokens"),
            "tool_names": [],
            "message_count": len((parsed_body or {}).get("messages") or []),
            "body_bytes": len(body or b""),
        }
        for t in (parsed_body or {}).get("tools") or []:
            if not isinstance(t, dict):
                continue
            if t.get("type") == "custom":
                summary["tool_names"].append(t.get("name") or "custom")
            else:
                fn = t.get("function") if isinstance(t.get("function"), dict) else {}
                summary["tool_names"].append((fn or {}).get("name") or t.get("type") or "?")
        payload = {
            "summary": summary,
            "tools": (parsed_body or {}).get("tools"),
            "body_head": (body or b"")[: 2 * 1024 * 1024].decode("utf-8", "ignore"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        process_log.info(
            "dumped cursor req=%s path=%s bytes=%s msgs=%s tools=%s",
            req_id,
            path,
            summary["body_bytes"],
            summary["message_count"],
            len(summary["tool_names"]),
        )
    except Exception:
        process_log.exception("dump cursor request failed req=%s", req_id)
