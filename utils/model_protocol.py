"""按模型指定调用协议。读内存缓存，管理接口写入后立刻刷新。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import ModelProtocol
from utils.get_log import process_log
from utils.model_route import normalize_route_pattern, pattern_specificity

PROTOCOLS = ("openai", "anthropic", "responses")
_cache: list[dict] = []


def _row_dict(row: ModelProtocol) -> dict:
    return {
        "id": row.id,
        "model": (row.model or "").strip().lower(),
        "protocol": (row.protocol or "").strip().lower(),
        "is_active": bool(row.is_active),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def reload_protocols() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ModelProtocol).order_by(ModelProtocol.id.desc()))).scalars().all()
        _cache = [_row_dict(row) for row in rows]
    except Exception:
        process_log.exception("reload model protocols failed")
    return list(_cache)


def list_protocols(*, active_only: bool = False) -> list[dict]:
    items = [dict(item) for item in _cache]
    if active_only:
        items = [item for item in items if item["is_active"]]
    return items


def match_model_protocol(model: str, rules: Optional[list[dict]] = None) -> Optional[dict]:
    """精确 > 更长前缀 > 整段 token。gemini* 命中所有以 gemini 开头的模型。"""
    best = None
    best_score = None
    for item in rules if rules is not None else _cache:
        if not item.get("is_active") or not item.get("model") or not item.get("protocol"):
            continue
        score = pattern_specificity(item["model"], model)
        if score is None:
            continue
        if best_score is None or score > best_score:
            best = item
            best_score = score
    return dict(best) if best else None


def apply_protocol_to_models_payload(payload: dict) -> dict:
    """给 /v1/models 的每个模型标上协议，并附上规则供客户端匹配未列出的型号。"""
    if not isinstance(payload, dict):
        return payload
    rules = list_protocols(active_only=True)
    data = payload.get("data")
    updated = dict(payload)
    if isinstance(data, list):
        out = []
        for item in data:
            if not isinstance(item, dict):
                out.append(item)
                continue
            model_id = item.get("id")
            rule = match_model_protocol(model_id, rules) if isinstance(model_id, str) else None
            if not rule:
                out.append(item)
                continue
            patched = dict(item)
            patched["protocol"] = rule["protocol"]
            out.append(patched)
        updated["data"] = out
    updated["protocol_routes"] = [
        {"model": item["model"], "protocol": item["protocol"]}
        for item in rules
    ]
    return updated


async def upsert_protocol(
    *,
    model: str,
    protocol: str,
    is_active: bool = True,
    note: str = "",
    protocol_id: Optional[int] = None,
) -> dict:
    model = normalize_route_pattern(model)
    protocol = (protocol or "").strip().lower()
    if protocol not in PROTOCOLS:
        raise ValueError("protocol 只能是 openai、anthropic 或 responses")
    async with AsyncSessionLocal() as db:
        row = None
        if protocol_id:
            row = await db.get(ModelProtocol, protocol_id)
        if row is None:
            row = (await db.execute(select(ModelProtocol).where(ModelProtocol.model == model))).scalars().first()
        if row is None:
            row = ModelProtocol(model=model, protocol=protocol)
            db.add(row)
        else:
            clash = (await db.execute(
                select(ModelProtocol).where(ModelProtocol.model == model, ModelProtocol.id != row.id)
            )).scalars().first()
            if clash:
                raise ValueError(f"model protocol already exists: {model}")
        row.model = model
        row.protocol = protocol
        row.is_active = bool(is_active)
        row.note = (note or "")[:200]
        await db.commit()
        await db.refresh(row)
        saved_id = row.id
    await reload_protocols()
    saved = next((item for item in list_protocols() if item["id"] == saved_id), None)
    if saved is None:
        raise ValueError("protocol save failed")
    return saved


async def delete_protocol(protocol_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(ModelProtocol, protocol_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_protocols()
    return True
