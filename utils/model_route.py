"""按模型覆盖上游。读内存缓存，管理接口写入后立刻刷新，无需重启。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.models import ModelRoute, User
from utils.get_log import process_log
from utils.vendor_catalog import (
    _mask_api_key,
    get_runtime_upstream,
    get_vendor_catalog,
    resolve_vendor,
    vendor_default_key_secret,
    vendor_key_label,
    vendor_key_secret,
)

# 按 model 长度降序，便于最长前缀命中
_cache: list[dict] = []
_users_by_id: dict[int, dict] = {}
_phone_to_user: dict[str, int] = {}
_AUTH_SKIP = {"", "-", "not exist", "no money"}


def _norm(model: str) -> str:
    try:
        from utils.check_user import normalize_model_name
        return (normalize_model_name(model) or model or "").strip().lower()
    except Exception:
        return (model or "").strip().lower()


def normalize_user_ids(raw) -> list[int]:
    """去重并保留正整数用户主键。空列表表示规则对全部用户生效。"""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, int):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _row_dict(row: ModelRoute) -> dict:
    user_ids = normalize_user_ids(getattr(row, "user_ids", None))
    return {
        "id": row.id,
        "model": (row.model or "").strip().lower(),
        "vendor_code": (row.vendor_code or "").strip().lower(),
        "api_key": (getattr(row, "api_key", None) or "").strip(),
        "api_key_id": getattr(row, "api_key_id", None),
        "user_ids": user_ids,
        "user_id_set": set(user_ids),
        "is_active": bool(row.is_active),
        "note": row.note or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def rule_applies_to_user(item: dict, user_id: Optional[int]) -> bool:
    """user_ids 为空时全体生效；否则只有名单内用户命中。"""
    ids = item.get("user_id_set")
    if ids is None:
        ids = set(normalize_user_ids(item.get("user_ids")))
    if not ids:
        return True
    if user_id is None:
        return False
    try:
        return int(user_id) in ids
    except (TypeError, ValueError):
        return False


def _public_route(item: dict) -> dict:
    """管理接口不回传完整密钥。"""
    out = dict(item)
    raw = (out.pop("api_key", None) or "").strip()
    out.pop("user_id_set", None)
    user_ids = normalize_user_ids(out.get("user_ids"))
    out["user_ids"] = user_ids
    users = []
    for uid in user_ids:
        snap = _users_by_id.get(uid)
        users.append(dict(snap) if snap else {"user_id": uid, "name": "", "telephone": "", "email": ""})
    out["users"] = users
    out["has_api_key"] = bool(raw)
    out["api_key_hint"] = _mask_api_key(raw)
    key_id = out.get("api_key_id")
    out["key_name"] = vendor_key_label(out.get("vendor_code") or "", key_id) if key_id else ""
    return out


def _user_id_from_key(user_key: str) -> Optional[int]:
    """请求鉴权给的是手机号。只把已加入某条规则的用户解析成主键。"""
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


async def reload_routes() -> list[dict]:
    global _cache
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(ModelRoute).order_by(ModelRoute.id.desc()))).scalars().all()
        loaded = sorted((_row_dict(row) for row in rows), key=lambda item: -len(item["model"]))
        wanted: set[int] = set()
        for item in loaded:
            wanted.update(item["user_ids"])
        await _load_user_snapshots(wanted)
        _cache = loaded
    except Exception:
        process_log.exception("reload model routes failed")
    return list(_cache)


def list_routes(*, active_only: bool = False) -> list[dict]:
    items = [_public_route(item) for item in _cache]
    if active_only:
        items = [item for item in items if item["is_active"]]
    return items


def wildcard_prefix(pattern: str) -> str:
    """gemini* 这种尾部星号表示「以此开头」。星号只允许出现在末尾。"""
    pattern = (pattern or "").strip().lower()
    if len(pattern) >= 3 and pattern.endswith("*") and "*" not in pattern[:-1]:
        return pattern[:-1]
    return ""


def normalize_route_pattern(model: str) -> str:
    model = _norm(model)
    if not model:
        raise ValueError("model is required")
    if "*" in model:
        if not wildcard_prefix(model):
            raise ValueError("前缀请写成 gemini*，星号只放在末尾")
    return model


def query_hits_route(rule_model: str, query: str) -> bool:
    """检索替换：规则名与输入的模型互相按完整名、前缀、通配前缀或单个片段命中。"""
    rule_model = _norm(rule_model)
    query = _norm(query)
    if not rule_model or not query:
        return False
    rule_prefix = wildcard_prefix(rule_model)
    query_prefix = wildcard_prefix(query)
    if rule_prefix:
        target = query_prefix or query
        return target.startswith(rule_prefix)
    if query_prefix:
        return rule_model.startswith(query_prefix)
    if rule_model == query:
        return True
    if rule_model.startswith(f"{query}-") or query.startswith(f"{rule_model}-"):
        return True
    if "-" not in rule_model and rule_model in set(query.split("-")):
        return True
    if "-" not in query and query in set(rule_model.split("-")):
        return True
    return False


def pattern_specificity(pattern: str, model: str) -> Optional[tuple[int, int, int]]:
    """匹配强度 (档位, 长度, 非通配优先)。越大越优先。

    档位：精确 3，前缀 2（以 - 为边界，或 gemini* 这种开头通配），整段 token 1。
    同为前缀时更长的优先，所以 gemini-3.1* 盖过 gemini*。
    """
    pattern = (pattern or "").strip().lower()
    model = (model or "").strip().lower()
    if not pattern or not model:
        return None
    star = wildcard_prefix(pattern)
    if star:
        if model.startswith(star):
            return (2, len(star), 0)
        return None
    if pattern == model:
        return (3, len(pattern), 1)
    if model.startswith(pattern) and (len(model) == len(pattern) or model[len(pattern)] == "-"):
        return (2, len(pattern), 1)
    if "-" not in pattern and pattern in set(model.split("-")):
        return (1, len(pattern), 1)
    return None


def match_model_route(model: str, user_id: Optional[int] = None) -> Optional[dict]:
    """精确 > 最长前缀（- 边界或末尾 *）> 整段 token。

    gemini* 命中所有以 gemini 开头的模型。user_id 为空时，只命中「全部用户」规则。
    """
    model = _norm(model)
    if not model:
        return None
    best = None
    best_score = None
    for item in _cache:
        if not item["is_active"] or not item["model"] or not rule_applies_to_user(item, user_id):
            continue
        score = pattern_specificity(item["model"], model)
        if score is None:
            continue
        if best_score is None or score > best_score:
            best = item
            best_score = score
    return dict(best) if best else None


def resolve_runtime_for_model(model: str, user_key: str = "") -> dict:
    """请求转发/计费用的运行时。未命中规则时回退全局默认供应商。

    user_key 是鉴权得到的手机号。只有被写进规则的用户会改走对应厂商。
    """
    rule = match_model_route(model, user_id=_user_id_from_key(user_key))
    default = get_runtime_upstream()
    if not rule:
        return default
    override = get_runtime_upstream(vendor=rule["vendor_code"])
    if not override or not override.get("code"):
        return default
    # 优先级：规则临时覆盖 Key > 规则指定的命名 Key > 该供应商默认命名 Key
    # 后续调用方再用 vendor 兜底 Key / 全局 Key
    route_key = (rule.get("api_key") or "").strip()
    key_id = rule.get("api_key_id")
    if not route_key:
        if key_id:
            route_key = vendor_key_secret(rule["vendor_code"], key_id)
        else:
            route_key = vendor_default_key_secret(rule["vendor_code"])
    same_vendor = override.get("code") == default.get("code")
    if same_vendor and not route_key:
        return default
    out = dict(default if same_vendor else override)
    out["route_model"] = rule["model"]
    out["routed"] = True
    if route_key:
        out["route_api_key"] = route_key
    return out


def overlay_catalog_rows(
    default_rows: list[dict],
    user_id: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """把默认供应商价目叠上该用户命中的路由规则对应供应商的价。返回 (rows, 规则摘要)。"""
    rules = [item for item in list_routes(active_only=True) if rule_applies_to_user(item, user_id)]
    if not rules:
        return [dict(row) for row in default_rows], []

    override_catalogs: dict[str, list[dict]] = {}
    summaries = []
    for rule in rules:
        meta, catalog = get_vendor_catalog(vendor=rule["vendor_code"])
        if not meta:
            continue
        override_catalogs[rule["vendor_code"]] = catalog
        summaries.append({
            "id": rule["id"],
            "model": rule["model"],
            "vendor_code": meta["code"],
            "vendor_name": meta.get("name") or meta["code"],
        })

    def attach(row: dict, rule: dict) -> dict:
        item = dict(row)
        meta = resolve_vendor(vendor=rule["vendor_code"]) or {}
        item["route_vendor"] = rule["vendor_code"]
        item["route_vendor_name"] = meta.get("name") or rule["vendor_code"]
        item["bill_vendor"] = rule["vendor_code"]
        return item

    def pick_override(model: str, tier: str, vendor: str) -> Optional[dict]:
        for row in override_catalogs.get(vendor) or []:
            if row.get("model") == model and (row.get("service_tier") or "standard") == (tier or "standard"):
                return row
        return None

    seen = set()
    out = []
    for row in default_rows:
        model = row.get("model") or ""
        tier = row.get("service_tier") or "standard"
        rule = match_model_route(model, user_id=user_id)
        if rule and rule["vendor_code"] in override_catalogs:
            replaced = pick_override(model, tier, rule["vendor_code"])
            if replaced:
                out.append(attach(replaced, rule))
                seen.add((replaced.get("model"), replaced.get("service_tier") or "standard"))
                continue
        item = dict(row)
        if rule:
            item = attach(item, rule)
        out.append(item)
        seen.add((model, tier))

    for rule in rules:
        for row in override_catalogs.get(rule["vendor_code"]) or []:
            model = row.get("model") or ""
            if not match_model_route(model, user_id=user_id):
                continue
            key = (model, row.get("service_tier") or "standard")
            if key in seen:
                continue
            out.append(attach(row, rule))
            seen.add(key)
    out.sort(key=lambda item: (item.get("model") or "", item.get("service_tier") or ""))
    return out, summaries


async def upsert_route(
    *,
    model: str,
    vendor_code: str,
    is_active: bool = True,
    note: str = "",
    route_id: Optional[int] = None,
    api_key: Optional[str] = None,
    api_key_id: Optional[int] = None,
    user_ids: Optional[list] = None,
) -> dict:
    model = normalize_route_pattern(model)
    vendor_code = (vendor_code or "").strip().lower()
    if not resolve_vendor(vendor=vendor_code):
        raise ValueError(f"unknown vendor: {vendor_code}")
    if api_key_id and not vendor_key_secret(vendor_code, api_key_id):
        raise ValueError(f"unknown vendor key: {api_key_id}")
    async with AsyncSessionLocal() as db:
        row = None
        if route_id:
            row = await db.get(ModelRoute, route_id)
        if row is None:
            row = (await db.execute(select(ModelRoute).where(ModelRoute.model == model))).scalars().first()
        if row is None:
            row = ModelRoute(model=model)
            db.add(row)
        else:
            clash = (await db.execute(
                select(ModelRoute).where(ModelRoute.model == model, ModelRoute.id != row.id)
            )).scalars().first()
            if clash:
                raise ValueError(f"model already routed: {model}")
        row.model = model
        row.vendor_code = vendor_code
        row.is_active = bool(is_active)
        row.note = (note or "")[:200]
        if api_key is not None:
            row.api_key = (api_key or "").strip() or None
        if api_key_id is not None:
            # 指定命名 Key 时清掉临时覆盖，避免两套密钥同时存在产生歧义
            row.api_key_id = int(api_key_id) or None
            if row.api_key_id:
                row.api_key = None
        if user_ids is not None:
            ids = normalize_user_ids(user_ids)
            if len(ids) > 500:
                raise ValueError("too many users on one route")
            if ids:
                found = set((await db.execute(
                    select(User.user_id).where(User.user_id.in_(ids))
                )).scalars().all())
                missing = [item for item in ids if item not in found]
                if missing:
                    raise ValueError(f"unknown user: {missing[0]}")
            row.user_ids = ids
        await db.commit()
        await db.refresh(row)
        saved_id = row.id
    await reload_routes()
    saved = next((item for item in list_routes() if item["id"] == saved_id), None)
    if saved is None:
        raise ValueError("route save failed")
    return saved


async def replace_route_api_keys(*, model: str, api_key: str) -> dict:
    """按模型检索，把命中的规则密钥一次换成同一把。"""
    query = _norm(model)
    key = (api_key or "").strip()
    if not query:
        raise ValueError("model is required")
    if not key:
        raise ValueError("api_key is required")
    if len(key) > 512:
        raise ValueError("api_key is too long")
    matched_ids = [item["id"] for item in _cache if query_hits_route(item.get("model") or "", query)]
    if not matched_ids:
        raise ValueError("没有匹配的模型规则")
    models = []
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(ModelRoute).where(ModelRoute.id.in_(matched_ids)))).scalars().all()
        if not rows:
            raise ValueError("没有匹配的模型规则")
        for row in rows:
            row.api_key = key
            models.append((row.model or "").strip().lower())
        await db.commit()
    await reload_routes()
    return {"updated": len(models), "models": sorted(set(models))}


async def delete_route(route_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(ModelRoute, route_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_routes()
    return True
