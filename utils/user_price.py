"""特定用户专属模型价。

价格直接按 USD / 1M tokens 录入，命中后即用户应付价：扣费只再乘人民币汇率，
不再叠加上游成本倍率、用户倍率（money_rate）或折扣（discount）。

匹配规则与模型路由一致：精确 > 最长前缀（- 边界或末尾 *）> 整段 token；
用户用 tb_user.user_id 主键指定。读内存缓存，管理接口写入后立刻刷新。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import User, UserModelPrice
from utils.get_log import process_log
from utils.model_route import (
    _norm,
    normalize_route_pattern,
    pattern_specificity,
)

_cache: list[dict] = []
_users_by_id: dict[int, dict] = {}
_AUTH_SKIP = {"", "-", "not exist", "no money"}


def _row_dict(row: UserModelPrice) -> dict:
    try:
        phone = int(row.user_id)
    except (TypeError, ValueError):
        phone = 0
    return {
        "id": row.id,
        "user_id": phone,
        "model": (row.model or "").strip().lower(),
        "service_tier": (row.service_tier or "standard").strip() or "standard",
        "input": float(row.input_price or 0),
        "output": float(row.output_price or 0),
        "cache_read": float(row.cache_read_price or 0),
        "cache_write": float(row.cache_write_price or 0),
        "text_input": float(row.text_input_price or 0),
        "image_input": float(row.image_input_price or 0),
        "per_image_tiers": row.per_image_tiers if isinstance(row.per_image_tiers, dict) else None,
        "is_active": bool(row.is_active),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _public_price(item: dict) -> dict:
    out = dict(item)
    snap = _users_by_id.get(out.get("user_id"))
    out["user"] = dict(snap) if snap else {
        "user_id": out.get("user_id"),
        "name": "",
        "telephone": "",
        "email": "",
    }
    return out


async def _load_user_snapshots(user_ids: set[int]) -> None:
    global _users_by_id
    snapshots: dict[int, dict] = {}
    if user_ids:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(User.user_id, User.name, User.telephone, User.email).where(User.user_id.in_(user_ids))
            )).all()
        for user_id, name, telephone, email in rows:
            uid = int(user_id)
            snapshots[uid] = {
                "user_id": uid,
                "name": name or "",
                "telephone": telephone or "",
                "email": email or "",
            }
    _users_by_id = snapshots


async def reload_user_prices() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(UserModelPrice).order_by(UserModelPrice.id.desc()))).scalars().all()
        loaded = sorted((_row_dict(row) for row in rows), key=lambda item: -len(item["model"]))
        await _load_user_snapshots({item["user_id"] for item in loaded if item["user_id"]})
        _cache = loaded
    except Exception:
        process_log.exception("reload user model prices failed")
    return list(_cache)


def list_user_prices(*, active_only: bool = False) -> list[dict]:
    items = [_public_price(item) for item in _cache]
    if active_only:
        items = [item for item in items if item["is_active"]]
    return items


def _user_id_from_key(user_key: str) -> Optional[int]:
    """鉴权给的是手机号；只有配置过专属价的用户才解析成主键。"""
    key = (user_key or "").strip()
    if key in _AUTH_SKIP:
        return None
    for item in _cache:
        if (item.get("telephone") or "") == key:
            return item["user_id"]
    if key.isdigit():
        value = int(key)
        if value in _users_by_id:
            return value
    return None


def match_user_price(model: str, user_key: str = "", service_tier: str = "") -> Optional[dict]:
    """命中该用户的专属价；没有则为 None（继续走供应商 + 用户倍率计价）。"""
    model = _norm(model)
    if not model or not _cache:
        return None
    uid = _user_id_from_key(user_key)
    if not uid:
        return None
    tier = (service_tier or "standard").strip() or "standard"
    best = None
    best_score = None
    for item in _cache:
        if not item["is_active"] or item["user_id"] != uid:
            continue
        if item["service_tier"] != tier and not (tier in {"fast", "priority"} and item["service_tier"] == "fast"):
            continue
        score = pattern_specificity(item["model"], model)
        if score is None:
            continue
        # 同一强度下，档位精确匹配的优先于 fast 兜底
        exact_tier = 1 if item["service_tier"] == tier else 0
        score = (*score, exact_tier)
        if best_score is None or score > best_score:
            best = item
            best_score = score
    return dict(best) if best else None


async def upsert_user_price(
    *,
    user_id: int,
    model: str,
    service_tier: str = "standard",
    input_price: float = 0,
    output_price: float = 0,
    cache_read_price: Optional[float] = None,
    cache_write_price: Optional[float] = None,
    text_input_price: Optional[float] = None,
    image_input_price: Optional[float] = None,
    per_image_tiers: Optional[dict] = None,
    is_active: bool = True,
    note: str = "",
    price_id: Optional[int] = None,
) -> dict:
    pattern = normalize_route_pattern(model)
    tier = (service_tier or "standard").strip() or "standard"
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("user_id is required")
    if uid <= 0:
        raise ValueError("user_id is required")
    async with AsyncSessionLocal() as db:
        exists = (await db.execute(select(User.user_id).where(User.user_id == uid))).scalar()
        if exists is None:
            raise ValueError(f"unknown user: {uid}")
        row = await db.get(UserModelPrice, price_id) if price_id else None
        if row is None:
            row = (await db.execute(select(UserModelPrice).where(
                UserModelPrice.user_id == uid,
                UserModelPrice.model == pattern,
                UserModelPrice.service_tier == tier,
            ))).scalars().first()
        if row is None:
            row = UserModelPrice(user_id=uid, model=pattern, service_tier=tier)
            db.add(row)
        elif price_id:
            clash = (await db.execute(select(UserModelPrice).where(
                UserModelPrice.user_id == uid,
                UserModelPrice.model == pattern,
                UserModelPrice.service_tier == tier,
                UserModelPrice.id != row.id,
            ))).scalars().first()
            if clash:
                raise ValueError(f"这个用户已经为 {pattern}（{tier}）设过专属价")
        row.user_id = uid
        row.model = pattern
        row.service_tier = tier
        row.input_price = max(0.0, float(input_price or 0))
        row.output_price = max(0.0, float(output_price or 0))
        row.cache_read_price = max(0.0, float(cache_read_price or 0))
        row.cache_write_price = max(0.0, float(cache_write_price or 0))
        row.text_input_price = max(0.0, float(text_input_price or 0))
        row.image_input_price = max(0.0, float(image_input_price or 0))
        row.per_image_tiers = per_image_tiers or None
        row.is_active = bool(is_active)
        row.note = (note or "")[:200]
        await db.commit()
        await db.refresh(row)
        saved_id = row.id
    await reload_user_prices()
    saved = next((item for item in list_user_prices() if item["id"] == saved_id), None)
    if saved is None:
        raise ValueError("price save failed")
    return saved


async def delete_user_price(price_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(UserModelPrice, price_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_user_prices()
    return True


def apply_user_price_to_catalog(rows: list[dict], user_key: str) -> list[dict]:
    """价目页：该用户有专属价的模型直接展示专属价（1:1 美元）。"""
    if not _cache:
        return rows
    out = []
    for row in rows:
        hit = match_user_price(
            row.get("model") or "",
            user_key=user_key,
            service_tier=row.get("service_tier") or "standard",
        )
        if not hit:
            out.append(row)
            continue
        item = dict(row)
        if row.get("kind") == "image" or hit.get("per_image_tiers"):
            tiers = hit.get("per_image_tiers") or {}
            if tiers:
                item.update({
                    "text_input": hit["text_input"],
                    "image_input": hit["image_input"],
                    "output": hit["output"],
                    "per_image_tiers": {str(k).lower(): float(v or 0) for k, v in tiers.items()},
                })
        else:
            item.update({
                "input": hit["input"],
                "output": hit["output"],
                "cache_read": hit["cache_read"],
                "cache_write": hit["cache_write"],
            })
        item["user_price_id"] = hit["id"]
        item["route_vendor"] = ""
        item["route_vendor_name"] = ""
        item["bill_vendor"] = ""
        out.append(item)
    return out
