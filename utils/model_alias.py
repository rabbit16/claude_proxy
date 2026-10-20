"""模型别名：对外 id → 真实模型。读内存缓存，管理接口写入后立刻刷新。"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import ModelAlias
from utils.get_log import process_log

PROTOCOLS = ("auto", "openai", "anthropic", "responses")
_cache: list[dict] = []


def _norm(name: str) -> str:
    try:
        from utils.check_user import normalize_model_name
        return (normalize_model_name(name) or name or "").strip().lower()
    except Exception:
        return (name or "").strip().lower()


def infer_protocol(model: str) -> str:
    name = (model or "").strip().lower()
    if "claude" in name or name.startswith("anthropic"):
        return "anthropic"
    if name.startswith("gemini") or "/gemini" in name:
        return "openai"
    return "openai"


def explicit_protocol(rule: dict) -> str:
    protocol = (rule.get("protocol") or "auto").strip().lower()
    if protocol in {"openai", "anthropic", "responses"}:
        return protocol
    return ""


def assign_alias_protocol(item: dict, rule: dict) -> dict:
    """别名上写死的协议盖过模型协议规则；auto 则沿用已标好的协议，没有再按模型名推断。"""
    patched = dict(item)
    chosen = explicit_protocol(rule)
    if chosen:
        patched["protocol"] = chosen
    elif not patched.get("protocol"):
        patched["protocol"] = infer_protocol(rule.get("target_model") or "")
    return patched


def resolved_protocol(rule: dict) -> str:
    protocol = (rule.get("protocol") or "auto").strip().lower()
    if protocol in {"openai", "anthropic", "responses"}:
        return protocol
    return infer_protocol(rule.get("target_model") or "")


def _row_dict(row: ModelAlias) -> dict:
    return {
        "id": row.id,
        "alias": (row.alias or "").strip().lower(),
        "target_model": (row.target_model or "").strip().lower(),
        "protocol": (row.protocol or "auto").strip().lower() or "auto",
        "hide_target": bool(row.hide_target),
        "is_active": bool(row.is_active),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def reload_aliases() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ModelAlias).order_by(ModelAlias.id.desc()))).scalars().all()
        _cache = [_row_dict(row) for row in rows]
    except Exception:
        process_log.exception("reload model aliases failed")
    return list(_cache)


def list_aliases(*, active_only: bool = False) -> list[dict]:
    items = [dict(item) for item in _cache]
    if active_only:
        items = [item for item in items if item["is_active"]]
    return items


def match_alias(model: str) -> Optional[dict]:
    """只做精确匹配，避免 gg-46 误伤其它模型。"""
    model = _norm(model)
    if not model:
        return None
    for item in _cache:
        if item["is_active"] and item["alias"] == model:
            return dict(item)
    return None


def resolve_alias(model: str) -> str:
    rule = match_alias(model)
    return rule["target_model"] if rule else _norm(model) or (model or "")


def _replace_json_body(body: bytes, updated: dict) -> tuple[bytes, dict]:
    stripped = body.lstrip() if body else b""
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            return json.dumps(updated, ensure_ascii=False).encode("utf-8"), updated
        except Exception:
            return body, updated
    return body, updated


def apply_alias_to_request(body: bytes, parsed_body: dict) -> tuple[bytes, dict, Optional[dict]]:
    if not isinstance(parsed_body, dict):
        return body, parsed_body, None
    model = parsed_body.get("model")
    if not isinstance(model, str):
        return body, parsed_body, None
    rule = match_alias(model)
    if not rule:
        return body, parsed_body, None
    updated = dict(parsed_body)
    updated["model"] = rule["target_model"]
    new_body, new_parsed = _replace_json_body(body, updated)
    return new_body, new_parsed, rule


def apply_aliases_to_models_payload(payload: dict) -> dict:
    """往 /v1/models 注入别名，可选隐藏真实模型名。不回传 target。"""
    if not isinstance(payload, dict):
        return payload
    aliases = list_aliases(active_only=True)
    data = payload.get("data")
    if not isinstance(data, list):
        return payload
    hide = {item["target_model"] for item in aliases if item.get("hide_target") and item.get("target_model")}
    by_id = {
        item.get("id"): item
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    out = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            out.append(item)
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and _norm(model_id) in hide:
            continue
        if isinstance(model_id, str):
            seen.add(_norm(model_id))
        out.append(item)
    for rule in aliases:
        alias = rule["alias"]
        if not alias:
            continue
        protocol = resolved_protocol(rule)
        if alias in seen:
            for idx, item in enumerate(out):
                if isinstance(item, dict) and _norm(item.get("id") or "") == alias:
                    out[idx] = assign_alias_protocol(item, rule)
                    break
            continue
        template = by_id.get(rule["target_model"])
        item = dict(template) if isinstance(template, dict) else {"object": "model", "owned_by": protocol}
        item["id"] = alias
        item["object"] = item.get("object") or "model"
        item.pop("root", None)
        item = assign_alias_protocol(item, rule)
        out.append(item)
        seen.add(alias)
    updated = dict(payload)
    updated["data"] = out
    return updated


def apply_aliases_to_catalog(rows: list[dict]) -> list[dict]:
    """价目展示别名行；hide_target 时去掉原模型。不附带上游路由字段。"""
    aliases = list_aliases(active_only=True)
    hide = {item["target_model"] for item in aliases if item.get("hide_target") and item.get("target_model")}
    by_model: dict[str, list[dict]] = {}
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model = row.get("model") or ""
        item = {key: value for key, value in row.items() if not str(key).startswith("route_")}
        by_model.setdefault(model, []).append(item)
        if model in hide:
            continue
        out.append(item)
    seen = {(item.get("model"), item.get("service_tier") or "standard") for item in out}
    for rule in aliases:
        for src in by_model.get(rule["target_model"], []):
            item = dict(src)
            item["model"] = rule["alias"]
            if not item.get("display_name") or item.get("display_name") == rule["target_model"]:
                item["display_name"] = rule["alias"]
            key = (item["model"], item.get("service_tier") or "standard")
            if key in seen:
                continue
            out.append(item)
            seen.add(key)
    return out


async def upsert_alias(
    *,
    alias: str,
    target_model: str,
    protocol: str = "auto",
    hide_target: bool = False,
    is_active: bool = True,
    note: str = "",
    alias_id: Optional[int] = None,
) -> dict:
    alias = _norm(alias)
    target_model = _norm(target_model)
    protocol = (protocol or "auto").strip().lower()
    if protocol not in PROTOCOLS:
        raise ValueError(f"unsupported protocol: {protocol}")
    if not alias:
        raise ValueError("alias is required")
    if not target_model:
        raise ValueError("target_model is required")
    if alias == target_model:
        raise ValueError("alias cannot equal target_model")
    async with AsyncSessionLocal() as db:
        row = None
        if alias_id:
            row = await db.get(ModelAlias, alias_id)
        if row is None:
            row = (await db.execute(select(ModelAlias).where(ModelAlias.alias == alias))).scalars().first()
        if row is None:
            row = ModelAlias(alias=alias)
            db.add(row)
        else:
            clash = (await db.execute(
                select(ModelAlias).where(ModelAlias.alias == alias, ModelAlias.id != row.id)
            )).scalars().first()
            if clash:
                raise ValueError(f"alias already exists: {alias}")
        row.alias = alias
        row.target_model = target_model
        row.protocol = protocol
        row.hide_target = bool(hide_target)
        row.is_active = bool(is_active)
        row.note = (note or "")[:200]
        await db.commit()
        await db.refresh(row)
        saved = _row_dict(row)
    await reload_aliases()
    return saved


async def delete_alias(alias_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(ModelAlias, alias_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_aliases()
    return True
