"""用户专属模型价（一条价格可绑定多个用户）。

价格直接按 USD / 1M tokens 录入，命中后即用户应付价：扣费只再乘人民币汇率，
不再叠加上游成本倍率、用户倍率（money_rate）或折扣（discount）。

匹配规则与模型路由一致：精确 > 最长前缀（- 边界或末尾 *）> 整段 token；
一条价可写多个用户（user_ids），同组用户共用同一套单价。读内存缓存，
管理接口写入后立刻刷新。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select, text

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


def _row_user_ids(row: UserModelPrice) -> list[int]:
    """一行价格覆盖的用户主键。旧数据 user_ids 为空时退回单用户 user_id。"""
    raw = row.user_ids if isinstance(row.user_ids, list) else None
    ids: list[int] = []
    for value in raw or ():
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in ids:
            ids.append(uid)
    if not ids:
        try:
            uid = int(row.user_id)
        except (TypeError, ValueError):
            return []
        if uid > 0:
            ids.append(uid)
    return ids


def _row_dict(row: UserModelPrice) -> dict:
    user_ids = _row_user_ids(row)
    return {
        "id": row.id,
        "user_id": user_ids[0] if user_ids else 0,
        "user_ids": user_ids,
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
    users = []
    for uid in out.get("user_ids") or []:
        snap = _users_by_id.get(uid)
        users.append(dict(snap) if snap else {
            "user_id": uid,
            "name": "",
            "telephone": "",
            "email": "",
        })
    out["users"] = users
    out["user"] = users[0] if users else {
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
        await _load_user_snapshots({
            uid for item in loaded for uid in item["user_ids"] if uid
        })
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
    """鉴权给的是手机号；只有配置过专属价的用户才解析成主键。

    手机号在用户快照里，价格行（_cache）没有 telephone 字段。
    """
    key = (user_key or "").strip()
    if key in _AUTH_SKIP:
        return None
    for uid, snap in _users_by_id.items():
        if (snap.get("telephone") or "") == key:
            return uid
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
        if not item["is_active"] or uid not in (item.get("user_ids") or ()):
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


def _normalize_user_ids(
    user_ids: Optional[Iterable], fallback: Optional[int] = None, *, required: bool = True,
) -> Optional[list[int]]:
    """把 user_ids / user_id 规整成去重列表；两者都为空时按 required 返回 None。"""
    ids: list[int] = []
    for value in list(user_ids or []) + ([fallback] if fallback else []):
        if value in (None, ""):
            continue
        try:
            uid = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"invalid user id: {value}")
        if uid <= 0:
            raise ValueError(f"invalid user id: {value}")
        if uid not in ids:
            ids.append(uid)
    if not ids:
        if required:
            raise ValueError("user_ids is required")
        return None
    return ids


async def ensure_user_price_schema_columns(conn) -> None:
    """老库补 user_ids 列，并放开旧的 (user_id, model, service_tier) 唯一约束。

    一条价格覆盖多用户后，同一用户可能出现在多行（不同分组），唯一约束会
    让「给同组再加一个用户」变成冲突。新库建表时已无该约束，这里幂等处理。
    """
    columns = (await conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_user_model_price'"
    ))).scalars().all()
    if "user_ids" not in {str(name) for name in columns}:
        await conn.execute(text("ALTER TABLE tb_user_model_price ADD COLUMN user_ids JSON NULL"))

    indexes = (await conn.execute(text(
        "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_user_model_price' "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    ))).all()
    columns_by_index: dict[str, list[str]] = {}
    unique: dict[str, bool] = {}
    for name, non_unique, _seq, column in indexes:
        key = str(name)
        columns_by_index.setdefault(key, []).append(str(column or "").strip().lower())
        unique[key] = int(non_unique or 0) == 0
    target = ["user_id", "model", "service_tier"]
    for name, cols in columns_by_index.items():
        if cols == target and unique.get(name):
            await conn.execute(text(f"ALTER TABLE tb_user_model_price DROP INDEX `{name}`"))
            process_log.info("dropped unique index on tb_user_model_price name=%s", name)


def _per_image_tiers(value) -> Optional[dict]:
    if not isinstance(value, dict) or not value:
        return None
    out: dict[str, float] = {}
    for key, price in value.items():
        try:
            out[str(key).strip().lower()] = max(0.0, float(price or 0))
        except (TypeError, ValueError):
            continue
    return out or None


async def upsert_user_price(
    *,
    user_ids: Optional[Iterable[int]] = None,
    user_id: Optional[int] = None,
    model: str,
    service_tier: Optional[str] = None,
    input_price: Optional[float] = None,
    output_price: Optional[float] = None,
    cache_read_price: Optional[float] = None,
    cache_write_price: Optional[float] = None,
    text_input_price: Optional[float] = None,
    image_input_price: Optional[float] = None,
    per_image_tiers: Optional[dict] = None,
    is_active: Optional[bool] = None,
    note: Optional[str] = None,
    price_id: Optional[int] = None,
) -> dict:
    """新建或修改一条专属价。

    新建：user_ids（或单个 user_id）+ model + 价格。
    修改：按 price_id 定位，所有字段都可改；未传的字段保持原值。
    """
    selected_model = (model or "").strip() if model else ""
    tier = ((service_tier or "").strip() or "standard") if service_tier is not None else None
    ids = _normalize_user_ids(user_ids, user_id, required=False)

    async with AsyncSessionLocal() as db:
        row = await db.get(UserModelPrice, price_id) if price_id else None
        creating = row is None
        # 修改时允许只传部分字段：用户组不传就保持原样（开关切换走这条路径）
        if ids is None:
            if creating:
                raise ValueError("user_ids is required")
            ids = _row_user_ids(row)
        exists = set((await db.execute(
            select(User.user_id).where(User.user_id.in_(ids))
        )).scalars().all())
        missing = [str(uid) for uid in ids if int(uid) not in {int(v) for v in exists}]
        if missing:
            raise ValueError(f"unknown user: {','.join(missing)}")

        if creating:
            if not selected_model:
                raise ValueError("model is required")
            if tier is None:
                tier = "standard"
            pattern = normalize_route_pattern(selected_model)
            row = UserModelPrice(user_id=ids[0], model=pattern, service_tier=tier)
            db.add(row)
        else:
            pattern = normalize_route_pattern(selected_model) if selected_model else row.model
            row.model = pattern
            if tier is not None:
                row.service_tier = tier

        row.user_ids = ids
        row.user_id = ids[0]
        if creating or input_price is not None:
            row.input_price = max(0.0, float(input_price or 0))
        if creating or output_price is not None:
            row.output_price = max(0.0, float(output_price or 0))
        if creating or cache_read_price is not None:
            row.cache_read_price = max(0.0, float(cache_read_price or 0))
        if creating or cache_write_price is not None:
            row.cache_write_price = max(0.0, float(cache_write_price or 0))
        if creating or text_input_price is not None:
            row.text_input_price = max(0.0, float(text_input_price or 0))
        if creating or image_input_price is not None:
            row.image_input_price = max(0.0, float(image_input_price or 0))
        if creating or per_image_tiers is not None:
            row.per_image_tiers = _per_image_tiers(per_image_tiers)
        if creating or is_active is not None:
            row.is_active = True if is_active is None else bool(is_active)
        if creating or note is not None:
            row.note = (note or "")[:200]

        # 同一用户 + 同一模型 + 同一档位只能有一条价，否则命中顺序会决定价格。
        # 先 flush 拿到新建行的 id，避免把自己的行当成冲突。
        await db.flush()
        conflicts = (await db.execute(select(UserModelPrice).where(
            UserModelPrice.id != row.id,
            UserModelPrice.model == row.model,
            UserModelPrice.service_tier == row.service_tier,
            UserModelPrice.is_active.is_(True),
        ))).scalars().all()
        clash = {
            uid for other in conflicts
            for uid in (other.user_ids or [other.user_id])
            if int(uid) in ids
        } if row.is_active else set()
        if clash:
            raise ValueError(
                f"这些用户已经为 {row.model}（{row.service_tier}）设过专属价："
                + ",".join(str(uid) for uid in sorted(clash))
            )
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
