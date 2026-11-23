"""按模型覆盖上游。读内存缓存，管理接口写入后立刻刷新，无需重启。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select, text

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
    vendor_protocol_key_secret,
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
        "group_name": (getattr(row, "group_name", None) or "").strip(),
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


def route_user_id_from_key(user_key: str) -> Optional[int]:
    """公开包装：把鉴权得到的手机号/用户主键解析成路由名单里的用户主键。"""
    return _user_id_from_key(user_key)


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


async def ensure_model_route_indexes(conn) -> None:
    """放开 tb_model_route.model 的唯一约束，改建成普通索引（幂等）。

    同名路由是特性：同一个模型按用户拆成多条，分别指向不同供应商。老库曾经
    把 model 建成唯一索引，启动时拆掉；新库建表时已是普通索引，这里只补索引。
    """
    rows = (await conn.execute(text(
        "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
        "FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tb_model_route' "
        "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
    ))).all()
    columns: dict[str, list[str]] = {}
    unique: dict[str, bool] = {}
    for name, non_unique, _seq, column in rows:
        key = str(name)
        columns.setdefault(key, []).append(str(column or "").strip().lower())
        unique[key] = int(non_unique or 0) == 0
    model_only_unique = {
        name for name, cols in columns.items() if cols == ["model"] and unique.get(name)
    }
    for name in model_only_unique:
        # 只拆「model 单列」的唯一索引；复合唯一索引不动。
        await conn.execute(text(f"ALTER TABLE tb_model_route DROP INDEX `{name}`"))
        process_log.info("dropped unique index on tb_model_route.model name=%s", name)
    plain_model_index = any(
        "model" in cols and not unique.get(name)
        for name, cols in columns.items()
        if name not in model_only_unique
    )
    if not plain_model_index:
        await conn.execute(text("CREATE INDEX ix_tb_model_route_model ON tb_model_route (model)"))


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


def _scope_key(item: dict) -> tuple[int, int]:
    """同名规则的用户优先级：指定用户 > 全体用户；指定用户里名单更短（更精准）的优先。"""
    ids = item.get("user_id_set")
    if ids is None:
        ids = set(normalize_user_ids(item.get("user_ids")))
    if not ids:
        return (0, 0)
    return (1, -len(ids))


def match_model_route(model: str, user_id: Optional[int] = None) -> Optional[dict]:
    """优先级：(模型精确度, 用户范围, id)，越大越优先。

    模型精确度沿用原规则：精确 > 最长前缀（- 边界或末尾 *）> 整段 token，
    所以非重名场景的行为完全不变。模型同分时才比用户范围：指定了该用户的
    规则压过「全部用户」的规则，名单更短的压过更长的，最后后建（id 大）的胜出。
    这样同一个模型可以按用户拆成多条，分别指向不同供应商。
    user_id 为空时只命中「全部用户」规则；gemini* 命中所有以 gemini 开头的模型。
    """
    model = _norm(model)
    if not model:
        return None
    best = None
    best_score = None
    for item in _cache:
        if not item["is_active"] or not item["model"] or not rule_applies_to_user(item, user_id):
            continue
        specificity = pattern_specificity(item["model"], model)
        if specificity is None:
            continue
        tier, length, literal = specificity
        score = (tier, length, literal, *_scope_key(item), item.get("id") or 0)
        if best_score is None or score > best_score:
            best = item
            best_score = score
    return dict(best) if best else None


def resolve_runtime_for_model(
    model: str, user_key: str = "", protocol: str = "",
) -> dict:
    """请求转发/计费用的运行时。未命中规则时回退全局默认供应商。

    user_key 是鉴权得到的手机号。只有被写进规则的用户会改走对应厂商。
    软路由生效时（某号商对该模型连续失败），本轮全量改走备用号商。
    protocol（openai/anthropic/responses）用于挑该号商绑定的同协议 Key，
    未绑定协议时沿用旧的「默认 Key」逻辑。
    """
    proto = (protocol or "").strip().lower()
    rule = match_model_route(model, user_id=_user_id_from_key(user_key))
    default = get_runtime_upstream()
    if rule:
        override = get_runtime_upstream(vendor=rule["vendor_code"])
        if override and override.get("code"):
            # 优先级：规则临时覆盖 Key > 规则指定的命名 Key >
            #        该供应商按协议绑定的 Key > 该供应商默认命名 Key
            # 后续调用方再用 vendor 兜底 Key / 全局 Key
            route_key = (rule.get("api_key") or "").strip()
            key_id = rule.get("api_key_id")
            if not route_key:
                if key_id:
                    route_key = vendor_key_secret(rule["vendor_code"], key_id)
                else:
                    route_key = (
                        vendor_protocol_key_secret(rule["vendor_code"], proto)
                        or vendor_default_key_secret(rule["vendor_code"])
                    )
            same_vendor = override.get("code") == default.get("code")
            if not (same_vendor and not route_key):
                out = dict(default if same_vendor else override)
                out["route_model"] = rule["model"]
                out["routed"] = True
                if route_key:
                    out["route_api_key"] = route_key
                return _apply_soft_route(model, out, proto)
    return _apply_soft_route(model, default, proto)


def _apply_soft_route(model: str, runtime: dict, protocol: str = "") -> dict:
    """软路由命中时把运行时整体替换为备用号商。未命中原样返回。"""
    try:
        from utils.soft_route import resolve_soft_route
        state = resolve_soft_route(model, runtime.get("code") or "")
        if not state:
            return runtime
        target = get_runtime_upstream(vendor=state["to_vendor_code"])
        if not target or not target.get("code"):
            return runtime
        out = dict(target)
        out["routed"] = True
        out["soft_routed"] = True
        out["soft_from_vendor"] = runtime.get("code") or ""
        out["soft_until"] = state.get("active_until")
        key = (
            vendor_protocol_key_secret(state["to_vendor_code"], protocol)
            or vendor_default_key_secret(state["to_vendor_code"])
        )
        if key:
            out["route_api_key"] = key
        return out
    except Exception:
        process_log.exception("apply soft route failed model=%s", model)
        return runtime


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
            # 同名规则可能指向不同供应商，只有真正命中的那一条才补价目，
            # 否则落选规则会把同一模型再加一遍。
            winner = match_model_route(model, user_id=user_id)
            if not winner or winner["id"] != rule["id"]:
                continue
            key = (model, row.get("service_tier") or "standard")
            if key in seen:
                continue
            out.append(attach(row, rule))
            seen.add(key)
    out.sort(key=lambda item: (item.get("model") or "", item.get("service_tier") or ""))
    return out, summaries


def normalize_route_models(models: Optional[list], fallback: str = "") -> list[str]:
    """一组对外模型名 → 去重后的规范化模式。每项都要通过星号规则校验。"""
    raw = list(models or [])
    if not raw and fallback:
        raw = [fallback]
    out: list[str] = []
    for item in raw:
        pattern = normalize_route_pattern(str(item or ""))
        if pattern not in out:
            out.append(pattern)
    if not out:
        raise ValueError("model is required")
    if len(out) > 200:
        raise ValueError("一条规则最多 200 个模型")
    return out


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
    group_name: Optional[str] = None,
    models: Optional[list] = None,
) -> dict:
    """单条或分组批量写入。传 models 时把它们全部指向同一 vendor / Key / 用户名单。

    分组名相同即视为同一组：改分组时先按「编辑时的旧分组」或 route_id 找旧成员，
    再对齐到本次提交的模型集合，并把整组 vendor / Key / 用户名单同步过去。

    model 允许重名：新建时总是插一条新规则（同一模型可以给不同用户指向不同供应商）；
    带 route_id 编辑时只改这一条。分组批量编辑按组成员对齐，组内已有同名成员直接复用。
    """
    patterns = normalize_route_models(models, fallback=model)
    vendor_code = (vendor_code or "").strip().lower()
    if not resolve_vendor(vendor=vendor_code):
        raise ValueError(f"unknown vendor: {vendor_code}")
    if api_key_id and not vendor_key_secret(vendor_code, api_key_id):
        raise ValueError(f"unknown vendor key: {api_key_id}")
    # None = 本次不动分组（开关这类局部更新）；"" = 显式改成不分组
    group = None if group_name is None else (group_name or "").strip()[:128]
    ids: list[int] = []
    if user_ids is not None:
        ids = normalize_user_ids(user_ids)
        if len(ids) > 500:
            raise ValueError("too many users on one route")

    async with AsyncSessionLocal() as db:
        # 名单校验必须在会话内查库：之前放在 async with 之外，db 尚未定义，
        # 只要提交了「适用用户」就抛 NameError 500。
        if ids:
            found = set((await db.execute(
                select(User.user_id).where(User.user_id.in_(ids))
            )).scalars().all())
            missing = [item for item in ids if item not in found]
            if missing:
                raise ValueError(f"unknown user: {missing[0]}")
        existing = list((await db.execute(select(ModelRoute))).scalars().all())
        target_row = next((row for row in existing if route_id and row.id == route_id), None)
        if route_id and target_row is None:
            raise ValueError(f"route not found: {route_id}")
        # 分组改名：按编辑前那条记录的分组，把旧成员一起带过来
        old_group = (getattr(target_row, "group_name", "") or "").strip() if target_row else ""
        if group is None:
            old_members = [target_row] if target_row else []
        else:
            old_members = (
                [row for row in existing if old_group and (row.group_name or "").strip() == old_group]
                or ([target_row] if target_row else [])
            )
        # 组内按模型名对齐要复用的旧行；被编辑的那条永远优先，避免同名时改错行。
        reusable: dict[str, ModelRoute] = {}
        for row in ([target_row] if target_row else []) + [
            item for item in old_members if item is not target_row
        ]:
            reusable.setdefault((row.model or "").strip().lower(), row)

        if group is not None:
            # 本组不再包含的旧成员：拆分出去的放开为独立规则，而不是直接删除
            kept = set(patterns)
            for row in old_members:
                if row.model not in kept:
                    row.group_name = ""
            if old_group and group and old_group != group:
                for row in old_members:
                    row.group_name = group

        saved_ids: list[int] = []
        for pattern in patterns:
            row = reusable.pop(pattern, None)
            if row is None and target_row is not None and len(patterns) == 1:
                # 单条编辑改了模型名：沿用原来那条记录，而不是新插一行。
                row = target_row
            if row is None:
                row = ModelRoute(model=pattern, group_name=group or "")
                db.add(row)
            row.model = pattern
            if group is not None:
                row.group_name = group
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
                row.user_ids = ids
            await db.flush()
            saved_ids.append(row.id)
        await db.commit()
    await reload_routes()
    saved = next((item for item in list_routes() if item["id"] == saved_ids[0]), None)
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
