"""把请求里的旧模型改写成新模型。读内存缓存，管理接口写入后立刻刷新。

命中后，转发、用量记录、扣费和计费展示都使用目标模型。只改写一次，避免目标名再次命中。
"""
from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import ModelRewrite, User
from utils.get_log import process_log
from utils.model_route import (
    _norm,
    normalize_route_pattern,
    normalize_user_ids,
    pattern_specificity,
    rule_applies_to_user,
)

_cache: list[dict] = []
_users_by_id: dict[int, dict] = {}
_phone_to_user: dict[str, int] = {}
_AUTH_SKIP = {"", "-", "not exist", "no money"}


def _row_dict(row: ModelRewrite) -> dict:
    user_ids = normalize_user_ids(getattr(row, "user_ids", None))
    return {
        "id": row.id,
        "model": (row.model or "").strip().lower(),
        "target_model": _norm(row.target_model or ""),
        "user_ids": user_ids,
        "user_id_set": set(user_ids),
        "is_active": bool(row.is_active),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _public_rewrite(item: dict) -> dict:
    out = dict(item)
    out.pop("user_id_set", None)
    user_ids = normalize_user_ids(out.get("user_ids"))
    out["user_ids"] = user_ids
    users = []
    for uid in user_ids:
        snap = _users_by_id.get(uid)
        users.append(dict(snap) if snap else {"user_id": uid, "name": "", "telephone": "", "email": ""})
    out["users"] = users
    return out


def _user_id_from_key(user_key: str) -> Optional[int]:
    key = (user_key or "").strip()
    if key in _AUTH_SKIP:
        return None
    mapped = _phone_to_user.get(key)
    if mapped:
        return mapped
    if key.isdigit():
        value = int(key)
        if value in _users_by_id:
            return value
        for item in _cache:
            if value in (item.get("user_id_set") or ()):
                return value
    return None


async def _load_user_snapshots(user_ids: set[int]) -> None:
    global _users_by_id, _phone_to_user
    snapshots: dict[int, dict] = {}
    phones: dict[str, int] = {}
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
            phone = (telephone or "").strip()
            if phone:
                phones[phone] = uid
    _users_by_id = snapshots
    _phone_to_user = phones


async def reload_rewrites() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ModelRewrite).order_by(ModelRewrite.id.desc()))).scalars().all()
        loaded = sorted((_row_dict(row) for row in rows), key=lambda item: -len(item["model"]))
        wanted: set[int] = set()
        for item in loaded:
            wanted.update(item["user_ids"])
        await _load_user_snapshots(wanted)
        _cache = loaded
    except Exception:
        process_log.exception("reload model rewrites failed")
    return list(_cache)


def list_rewrites(*, active_only: bool = False) -> list[dict]:
    items = [_public_rewrite(item) for item in _cache]
    if active_only:
        items = [item for item in items if item["is_active"]]
    return items


def match_model_rewrite(model: str, user_key: str = "") -> Optional[dict]:
    """精确 > 最长前缀（- 边界或末尾 *）> 整段 token。

    user_key 是鉴权得到的手机号。未写入规则的用户只命中「全部用户」规则。
    """
    model = _norm(model)
    if not model:
        return None
    user_id = _user_id_from_key(user_key)
    best = None
    best_score = None
    for item in _cache:
        if not item["is_active"] or not item["model"] or not item.get("target_model"):
            continue
        if not rule_applies_to_user(item, user_id):
            continue
        score = pattern_specificity(item["model"], model)
        if score is None:
            continue
        if best_score is None or score > best_score:
            best = item
            best_score = score
    if not best:
        return None
    if _norm(best["target_model"]) == model:
        return None
    out = dict(best)
    out.pop("user_id_set", None)
    return out


def _replace_json_body(body: bytes, updated: dict) -> tuple[bytes, dict]:
    stripped = body.lstrip() if body else b""
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            return json.dumps(updated, ensure_ascii=False).encode("utf-8"), updated
        except Exception:
            return body, updated
    return body, updated


def apply_model_rewrite(
    body: bytes,
    parsed_body: dict,
    *,
    user_key: str = "",
) -> tuple[bytes, dict, Optional[dict]]:
    """命中则把请求模型改成目标模型。返回的规则带 from_model，供日志使用。"""
    if not isinstance(parsed_body, dict):
        return body, parsed_body, None
    model = parsed_body.get("model")
    if not isinstance(model, str) or not model.strip():
        return body, parsed_body, None
    rule = match_model_rewrite(model, user_key=user_key)
    if not rule:
        return body, parsed_body, None
    updated = dict(parsed_body)
    updated["model"] = rule["target_model"]
    # 路径里的模型（Gemini）只改 parsed，避免把 model 字段写进原本没有它的请求体。
    original_has_model = False
    stripped = body.lstrip() if body else b""
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            original = json.loads(body)
            original_has_model = isinstance(original, dict) and isinstance(original.get("model"), str)
        except Exception:
            original_has_model = False
    if original_has_model:
        new_body, new_parsed = _replace_json_body(body, updated)
    else:
        new_body, new_parsed = body, updated
    hit = dict(rule)
    hit["from_model"] = model
    return new_body, new_parsed, hit


def _normalize_target(target_model: str) -> str:
    target = _norm(target_model)
    if not target:
        raise ValueError("请填写要改成的模型")
    if "*" in (target_model or ""):
        raise ValueError("目标模型请写完整模型名，不要用 *")
    if "/" in target or ":" in target:
        raise ValueError("目标模型名不能包含 / 或 :")
    return target


async def upsert_rewrite(
    *,
    model: str,
    target_model: str,
    is_active: bool = True,
    note: str = "",
    rewrite_id: Optional[int] = None,
    user_ids: Optional[list] = None,
) -> dict:
    model = normalize_route_pattern(model)
    target_model = _normalize_target(target_model)
    if "*" not in model and model == target_model:
        raise ValueError("目标模型不能和来源相同")
    async with AsyncSessionLocal() as db:
        row = None
        if rewrite_id:
            row = await db.get(ModelRewrite, rewrite_id)
        if row is None:
            row = (await db.execute(select(ModelRewrite).where(ModelRewrite.model == model))).scalars().first()
        if row is None:
            row = ModelRewrite(model=model, target_model=target_model)
            db.add(row)
        else:
            clash = (await db.execute(
                select(ModelRewrite).where(ModelRewrite.model == model, ModelRewrite.id != row.id)
            )).scalars().first()
            if clash:
                raise ValueError(f"这条模型改写已经存在: {model}")
        row.model = model
        row.target_model = target_model
        row.is_active = bool(is_active)
        row.note = (note or "")[:200]
        if user_ids is not None:
            ids = normalize_user_ids(user_ids)
            if len(ids) > 500:
                raise ValueError("一条规则里的用户太多")
            if ids:
                found = set((await db.execute(
                    select(User.user_id).where(User.user_id.in_(ids))
                )).scalars().all())
                missing = [item for item in ids if item not in found]
                if missing:
                    raise ValueError(f"unknown user: {missing[0]}")
            row.user_ids = ids
        elif row.user_ids is None:
            row.user_ids = []
        await db.commit()
        await db.refresh(row)
        saved_id = row.id
    await reload_rewrites()
    saved = next((item for item in list_rewrites() if item["id"] == saved_id), None)
    if saved is None:
        raise ValueError("rewrite save failed")
    return saved


async def delete_rewrite(rewrite_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(ModelRewrite, rewrite_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_rewrites()
    return True
