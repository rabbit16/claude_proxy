"""按模型注入系统提示。读内存缓存，管理接口写入后立刻刷新。"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import PromptInjection
from utils.get_log import process_log
from utils.vendor_catalog import catalog_provider

SCOPES = ("all", "category", "model")
POSITIONS = ("append", "prepend", "first")
_FOLLOWUP_TYPES = {"function_call", "function_call_output", "reasoning"}
CATEGORIES = ("anthropic", "openai", "xai", "google", "deepseek", "zhipu", "other")
CHAT_SOURCES = {"anthropic", "openai", "responses"}
_cache: list[dict] = []


def _norm(name: str) -> str:
    try:
        from utils.check_user import normalize_model_name
        return (normalize_model_name(name) or name or "").strip().lower()
    except Exception:
        return (name or "").strip().lower()


def _row_dict(row: PromptInjection) -> dict:
    return {
        "id": row.id,
        "name": row.name or "",
        "scope": (row.scope or "all").strip().lower() or "all",
        "target": (row.target or "").strip().lower(),
        "content": row.content or "",
        "position": (row.position or "append").strip().lower() or "append",
        "is_active": bool(row.is_active),
        "sort_order": int(row.sort_order or 0),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def reload_injections() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(PromptInjection).order_by(PromptInjection.sort_order.asc(), PromptInjection.id.asc())
            )).scalars().all()
        _cache = [_row_dict(row) for row in rows]
    except Exception:
        process_log.exception("reload prompt injections failed")
    return list(_cache)


def list_injections() -> list[dict]:
    return [dict(item) for item in _cache]


def _model_match(model: str, key: str) -> bool:
    model = _norm(model)
    key = _norm(key)
    if not model or not key:
        return False
    if model == key:
        return True
    if model.startswith(key) and model[len(key):len(key) + 1] == "-":
        return True
    if "-" not in key and key in set(model.split("-")):
        return True
    return False


def select_injections(model: str, client_model: str = "") -> list[dict]:
    """全部模型规则，加上命中的类别和指定模型规则。按 sort_order、id 排序。"""
    names = []
    for raw in (model, client_model):
        name = _norm(raw)
        if name and name not in names and name != "-":
            names.append(name)
    provider = catalog_provider(names[0]) if names else ""
    selected = []
    for item in _cache:
        if not item.get("is_active"):
            continue
        content = (item.get("content") or "").strip()
        if not content:
            continue
        scope = item.get("scope") or "all"
        target = item.get("target") or ""
        if scope == "all":
            selected.append(item)
        elif scope == "category" and provider and target == provider:
            selected.append(item)
        elif scope == "model" and any(_model_match(name, target) for name in names):
            selected.append(item)
    selected.sort(key=lambda rule: (int(rule.get("sort_order") or 0), int(rule.get("id") or 0)))
    return selected


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                bits.append(part["text"])
        return "\n".join(bits)
    return ""


def _pending(rules: list[dict], position: str, existing: str) -> list[tuple[int, str]]:
    out = []
    seen = []
    for rule in rules:
        if (rule.get("position") or "append") != position:
            continue
        text = (rule.get("content") or "").strip()
        if not text or text in existing or text in seen:
            continue
        seen.append(text)
        out.append((int(rule["id"]), text))
    return out


def _join(items: list[tuple[int, str]]) -> str:
    return "\n\n".join(text for _id, text in items)


def _inject_anthropic(parsed: dict, rules: list[dict]) -> tuple[dict, list[int]]:
    system = parsed.get("system")
    if isinstance(system, (str, list)):
        existing = _content_text(system)
    elif system is None:
        existing = ""
    else:
        existing = str(system)
    prepend = _pending(rules, "prepend", existing)
    append = _pending(rules, "append", existing)
    ids = [item[0] for item in prepend + append]
    if not ids:
        return parsed, []
    updated = dict(parsed)
    if isinstance(system, list):
        parts = list(system)
        for _id, text in reversed(prepend):
            parts.insert(0, {"type": "text", "text": text})
        for _id, text in append:
            parts.append({"type": "text", "text": text})
        updated["system"] = parts
        return updated, ids
    base = system if isinstance(system, str) else ("" if system is None else str(system))
    chunks = [piece for piece in (_join(prepend), base, _join(append)) if piece]
    updated["system"] = "\n\n".join(chunks)
    return updated, ids


def _message_index(messages: list) -> Optional[int]:
    developer = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "system":
            return index
        if role == "developer" and developer is None:
            developer = index
    return developer


def _is_responses_text_message(item: dict) -> bool:
    """Responses 里只有普通 message 能带 content。additional_tools 也带 role=developer。"""
    item_type = item.get("type")
    if item_type not in (None, "message"):
        return False
    return item.get("role") in {"system", "developer", "user", "assistant"}


def _responses_injection_index(items: list) -> Optional[int]:
    developer = None
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not _is_responses_text_message(item):
            continue
        role = item.get("role")
        if role == "system":
            return index
        if role == "developer" and developer is None:
            developer = index
    return developer


def _responses_insert_index(items: list) -> int:
    """插在开头的 additional_tools 之后，保持 Codex Responses Lite 的工具项在 input[0]。"""
    index = 0
    while index < len(items):
        item = items[index]
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            index += 1
            continue
        break
    return index


def _inject_message_list(
    messages: list,
    rules: list[dict],
    *,
    part_type: str = "text",
    index_fn=None,
    insert_at=None,
    insert_role: str = "system",
) -> tuple[list, list[int]]:
    finder = index_fn or _message_index
    index = finder(messages)
    existing = _content_text(messages[index].get("content")) if index is not None else ""
    prepend = _pending(rules, "prepend", existing)
    append = _pending(rules, "append", existing)
    ids = [item[0] for item in prepend + append]
    if not ids:
        return messages, []
    updated = list(messages)
    if index is None:
        text = "\n\n".join(piece for piece in (_join(prepend), _join(append)) if piece)
        content = text
        if part_type != "text":
            content = [{"type": part_type, "text": text}]
        pos = insert_at(updated) if insert_at else 0
        updated.insert(pos, {"role": insert_role, "content": content})
        return updated, ids
    message = dict(updated[index])
    content = message.get("content")
    if isinstance(content, list):
        parts = list(content)
        kind = part_type
        for part in parts:
            if isinstance(part, dict) and part.get("type") and isinstance(part.get("text"), str):
                kind = part["type"]
                break
        for _id, text in reversed(prepend):
            parts.insert(0, {"type": kind, "text": text})
        for _id, text in append:
            parts.append({"type": kind, "text": text})
        message["content"] = parts
    else:
        base = content if isinstance(content, str) else ("" if content is None else str(content))
        chunks = [piece for piece in (_join(prepend), base, _join(append)) if piece]
        message["content"] = "\n\n".join(chunks)
    updated[index] = message
    return updated, ids


def _inject_openai(parsed: dict, rules: list[dict]) -> tuple[dict, list[int]]:
    messages = parsed.get("messages")
    if not isinstance(messages, list):
        return parsed, []
    updated_messages, ids = _inject_message_list(messages, rules)
    if not ids:
        return parsed, []
    updated = dict(parsed)
    updated["messages"] = updated_messages
    return updated, ids


def _inject_responses(parsed: dict, rules: list[dict]) -> tuple[dict, list[int]]:
    if "instructions" in parsed or not isinstance(parsed.get("input"), list):
        instructions = parsed.get("instructions")
        if isinstance(instructions, list):
            updated_parts, ids = _inject_message_list(
                [{"role": "system", "content": instructions}],
                rules,
                part_type="input_text",
            )
            if not ids:
                return parsed, []
            updated = dict(parsed)
            updated["instructions"] = updated_parts[0]["content"]
            return updated, ids
        existing = instructions if isinstance(instructions, str) else ("" if instructions is None else str(instructions))
        prepend = _pending(rules, "prepend", existing)
        append = _pending(rules, "append", existing)
        ids = [item[0] for item in prepend + append]
        if not ids:
            return parsed, []
        chunks = [piece for piece in (_join(prepend), existing, _join(append)) if piece]
        updated = dict(parsed)
        updated["instructions"] = "\n\n".join(chunks)
        return updated, ids
    updated_input, ids = _inject_message_list(
        parsed.get("input") or [],
        rules,
        part_type="input_text",
        index_fn=_responses_injection_index,
        insert_at=_responses_insert_index,
        insert_role="developer",
    )
    if not ids:
        return parsed, []
    updated = dict(parsed)
    updated["input"] = updated_input
    return updated, ids


def _is_followup_item(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("role") == "assistant":
        return True
    return item.get("type") in _FOLLOWUP_TYPES


def _last_user_index(items: list) -> Optional[int]:
    found = None
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        if item.get("type") not in (None, "message"):
            continue
        found = index
    return found


def _is_first_turn(parsed: dict, source: str) -> bool:
    """还没有助手回复的那一次请求。后续轮次客户端会带回原文，不能再改。"""
    if source == "responses" and parsed.get("previous_response_id"):
        return False
    if source == "responses" and isinstance(parsed.get("input"), str):
        return True
    items = parsed.get("input") if source == "responses" else parsed.get("messages")
    if not isinstance(items, list) or any(_is_followup_item(item) for item in items):
        return False
    return _last_user_index(items) is not None


def _append_block(content, text: str, default_type: str):
    """加在末尾的新块上，不改已经带 cache_control 的块。"""
    if isinstance(content, list):
        parts = list(content)
        kind = default_type
        for part in parts:
            if isinstance(part, dict) and part.get("type") and isinstance(part.get("text"), str):
                kind = part["type"]
                break
        parts.append({"type": kind, "text": text})
        return parts
    base = content if isinstance(content, str) else ("" if content is None else str(content))
    if not base:
        return text
    return f"{base}\n\n{text}"


def _inject_first_turn(parsed: dict, rules: list[dict], source: str) -> tuple[dict, list[int]]:
    if not _is_first_turn(parsed, source):
        return parsed, []
    default_type = "input_text" if source == "responses" else "text"
    if source == "responses" and isinstance(parsed.get("input"), str):
        pending = _pending(rules, "first", parsed.get("input") or "")
        if not pending:
            return parsed, []
        updated = dict(parsed)
        updated["input"] = _append_block(parsed.get("input"), _join(pending), default_type)
        return updated, [item[0] for item in pending]
    items = parsed.get("input") if source == "responses" else parsed.get("messages")
    index = _last_user_index(items)
    if index is None:
        return parsed, []
    message = items[index]
    pending = _pending(rules, "first", _content_text(message.get("content")))
    if not pending:
        return parsed, []
    updated_items = list(items)
    updated_message = dict(message)
    updated_message["content"] = _append_block(message.get("content"), _join(pending), default_type)
    updated_items[index] = updated_message
    updated = dict(parsed)
    if source == "responses":
        updated["input"] = updated_items
    else:
        updated["messages"] = updated_items
    return updated, [item[0] for item in pending]


def _inject(parsed: dict, rules: list[dict], source: str) -> tuple[dict, list[int]]:
    system_rules = [rule for rule in rules if (rule.get("position") or "append") != "first"]
    first_rules = [rule for rule in rules if (rule.get("position") or "append") == "first"]
    updated = parsed
    ids: list[int] = []
    if system_rules:
        if source == "anthropic":
            updated, ids = _inject_anthropic(updated, system_rules)
        elif source == "responses":
            updated, ids = _inject_responses(updated, system_rules)
        elif source == "openai":
            updated, ids = _inject_openai(updated, system_rules)
    if first_rules:
        updated, first_ids = _inject_first_turn(updated, first_rules, source)
        ids.extend(first_ids)
    return updated, ids


def apply_prompt_injection(
    body: bytes,
    parsed_body: dict,
    *,
    model: str,
    client_model: str = "",
    source: str,
) -> tuple[bytes, dict, list[int]]:
    """把命中的提示写进请求。未改动时原样返回，避免重排 JSON。"""
    if source not in CHAT_SOURCES or not isinstance(parsed_body, dict):
        return body, parsed_body, []
    rules = select_injections(model, client_model)
    if not rules:
        return body, parsed_body, []
    updated, ids = _inject(parsed_body, rules, source)
    if not ids:
        return body, parsed_body, []
    raw = body.lstrip() if body else b""
    if not raw.startswith(b"{") and not raw.startswith(b"["):
        return body, parsed_body, []
    try:
        new_body = json.dumps(updated, ensure_ascii=False).encode("utf-8")
    except Exception:
        process_log.exception("prompt injection serialize failed")
        return body, parsed_body, []
    return new_body, updated, ids


async def upsert_injection(
    *,
    name: str,
    content: str,
    scope: str = "all",
    target: str = "",
    position: str = "append",
    is_active: bool = True,
    sort_order: int = 100,
    note: str = "",
    injection_id: Optional[int] = None,
) -> dict:
    name = (name or "").strip()
    content = (content or "").strip()
    scope = (scope or "all").strip().lower()
    position = (position or "append").strip().lower()
    target = (target or "").strip().lower()
    if not name:
        raise ValueError("name is required")
    if len(name) > 80:
        raise ValueError("name is too long")
    if not content:
        raise ValueError("content is required")
    if len(content) > 8000:
        raise ValueError("content is too long")
    if scope not in SCOPES:
        raise ValueError(f"unsupported scope: {scope}")
    if position not in POSITIONS:
        raise ValueError(f"unsupported position: {position}")
    if scope == "all":
        target = ""
    elif scope == "category":
        if target not in CATEGORIES:
            raise ValueError(f"unsupported category: {target}")
    elif not target:
        raise ValueError("target model is required")
    else:
        target = _norm(target)
    try:
        sort_order = int(sort_order)
    except (TypeError, ValueError) as exc:
        raise ValueError("sort_order must be an integer") from exc
    if sort_order < 0 or sort_order > 10000:
        raise ValueError("sort_order must be between 0 and 10000")
    async with AsyncSessionLocal() as db:
        row = await db.get(PromptInjection, injection_id) if injection_id else None
        if row is None:
            row = PromptInjection(name=name, content=content)
            db.add(row)
        row.name = name
        row.scope = scope
        row.target = target
        row.content = content
        row.position = position
        row.is_active = bool(is_active)
        row.sort_order = sort_order
        row.note = (note or "")[:200]
        await db.commit()
        await db.refresh(row)
        saved = _row_dict(row)
    await reload_injections()
    return saved


async def delete_injection(injection_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(PromptInjection, injection_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_injections()
    return True
