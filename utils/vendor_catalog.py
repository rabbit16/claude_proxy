"""多供应商模型价目表：按 vendor / base_url 隔离模型与价格。

表：
- tb_upstream_vendor：供应商（code + base_url）
- tb_vendor_model_price：某供应商下的模型价（USD / 1M tokens）

计费与前端展示都读这里；同步失败时回退到启动种子与代码快照。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import select, text

from settings.database import AsyncSessionLocal
from settings.macro import (
    IMAGE_MODEL_MONEY_DICT,
    MODEL_MAP_MONEY_DICT,
    OFFICIAL_PROVIDER_IMAGE_MODEL_MONEY_DICT,
    OFFICIAL_PROVIDER_MODEL_MONEY_DICT,
    PROFIT_MULTIPLIER,
    TEAMO_FAST_MODEL_MONEY_DICT,
    TEAMO_HTTP_PROXY,
    TEAMO_IMAGE_MODEL_MONEY_DICT,
    TEAMO_MODEL_ALIASES,
    TEAMO_MODEL_MONEY_DICT,
    TEAMO_UPSTREAM_BASE,
    UPSTREAM_PROVIDER,
    YUNXIN_UPSTREAM_BASE,
    LEGACY_UPSTREAM_BASE,
)
from settings.models import UpstreamVendor, VendorModelPrice
from utils.get_log import process_log

PRICING_MODE_MANUAL = "manual"
PRICING_MODE_CATALOG_RATE = "catalog_rate"
VALID_PRICING_MODES = {PRICING_MODE_MANUAL, PRICING_MODE_CATALOG_RATE}
# 模型原始供应商。xai 在界面上显示为 Grok。
CATALOG_PROVIDERS = (
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic"),
    ("xai", "Grok"),
    ("google", "Google"),
    ("deepseek", "DeepSeek"),
    ("zhipu", "智谱"),
    ("other", "其他"),
)
_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write", "text_input", "image_input")
_ORIGINAL_PRICE_FIELDS = (
    "original_input",
    "original_output",
    "original_cache_read",
    "original_cache_write",
    "original_text_input",
    "original_image_input",
)

_REFRESH_INTERVAL = 300.0
# vendor_code -> model -> service_tier -> price row
_cache: dict[str, dict[str, dict[str, dict]]] = {}
_vendor_meta: dict[str, dict] = {}
_last_refresh: dict[str, float] = {}
_refresh_lock = asyncio.Lock()


def normalize_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")).rstrip("/")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _opt_price(value: Any) -> Optional[float]:
    """原价：None/空/≤0 视为未设置；正数才生效。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _price_row(
    *,
    model: str,
    service_tier: str = "standard",
    kind: str = "chat",
    input_price: float = 0,
    output_price: float = 0,
    cache_read_price: float = 0,
    cache_write_price: float = 0,
    text_input_price: float = 0,
    image_input_price: float = 0,
    original_input_price: Any = None,
    original_output_price: Any = None,
    original_cache_read_price: Any = None,
    original_cache_write_price: Any = None,
    original_text_input_price: Any = None,
    original_image_input_price: Any = None,
    display_name: str = "",
    owned_by: str = "",
    raw: Any = None,
    source: str = "manual",
) -> dict:
    row = {
        "model": model,
        "service_tier": service_tier,
        "kind": kind,
        "input": float(input_price or 0),
        "output": float(output_price or 0),
        "cache_read": float(cache_read_price or 0),
        "cache_write": float(cache_write_price or 0),
        "text_input": float(text_input_price or 0),
        "image_input": float(image_input_price or 0),
        "display_name": display_name or model,
        "owned_by": owned_by or "",
        "raw": raw,
        "source": source,
    }
    originals = {
        "original_input": _opt_price(original_input_price),
        "original_output": _opt_price(original_output_price),
        "original_cache_read": _opt_price(original_cache_read_price),
        "original_cache_write": _opt_price(original_cache_write_price),
        "original_text_input": _opt_price(original_text_input_price),
        "original_image_input": _opt_price(original_image_input_price),
    }
    # 未设置时不写键，避免远程同步 upsert 把手工原价清掉
    if any(value is not None for value in originals.values()):
        row.update(originals)
    return row


def _put_cache(vendor_code: str, row: dict) -> None:
    vendor = _cache.setdefault(vendor_code, {})
    model_map = vendor.setdefault(row["model"], {})
    model_map[row["service_tier"]] = row


def catalog_provider(model: str, owned_by: str = "") -> str:
    """按模型名识别原始供应商：OpenAI / Anthropic / Grok / Google / DeepSeek / 智谱。"""
    mid = (model or "").strip().lower()
    owned = (owned_by or "").strip().lower()
    source = f"{owned} {mid}"
    if "claude" in source or "anthropic" in source:
        return "anthropic"
    if "grok" in source or owned == "xai":
        return "xai"
    if "gemini" in source or "google" in source:
        return "google"
    if "deepseek" in source:
        return "deepseek"
    if "glm" in mid or "zhipu" in source:
        return "zhipu"
    if (
        "gpt" in source
        or "openai" in source
        or mid.startswith(("o1", "o3", "o4", "text-embedding", "chatgpt", "dall", "codex"))
    ):
        return "openai"
    return "other"


def _tag_provider(row: dict) -> dict:
    provider = catalog_provider(row.get("model") or "", row.get("owned_by") or "")
    row["catalog_provider"] = provider
    if not row.get("owned_by"):
        row["owned_by"] = provider
    return row


def normalize_catalog_policy(policy: Any) -> dict:
    """校验各原始供应商是否对外展示，以及单独倍率。"""
    if not isinstance(policy, dict):
        raise ValueError("catalog_policy must be an object")
    providers_in = policy.get("providers")
    if not isinstance(providers_in, dict):
        raise ValueError("catalog_policy.providers must be an object")
    providers: dict[str, dict] = {}
    for name, _label in CATALOG_PROVIDERS:
        spec = providers_in.get(name) or {}
        if spec and not isinstance(spec, dict):
            raise ValueError(f"catalog_policy.providers.{name} must be an object")
        enabled = spec.get("enabled", True)
        enabled = enabled if isinstance(enabled, bool) else bool(enabled)
        override = spec.get("cost_rate", None)
        rate = None
        if override not in (None, ""):
            rate = float(override)
            if rate <= 0 or rate > 100:
                raise ValueError(f"{name} cost_rate must be > 0 and <= 100")
        providers[name] = {"enabled": enabled, "cost_rate": rate}
    if not any(item["enabled"] for item in providers.values()):
        raise ValueError("at least one provider must be visible")
    return {"providers": providers}


def provider_cost_factor(provider: str, default_rate: float, policy: Any = None) -> float:
    """某原始供应商的成本倍率；没单独设置时用统一倍率。是否对外展示不影响倍率。"""
    factor = float(default_rate)
    if not isinstance(policy, dict):
        return factor
    providers = policy.get("providers")
    if not isinstance(providers, dict):
        return factor
    spec = providers.get(provider)
    if not isinstance(spec, dict):
        return factor
    override = spec.get("cost_rate", None)
    if override in (None, ""):
        return factor
    value = float(override)
    if value <= 0:
        raise ValueError("cost_rate must be > 0")
    return value


def provider_rate_plan(policy: Any, default_rate: float) -> dict[str, float]:
    return {name: provider_cost_factor(name, default_rate, policy) for name, _label in CATALOG_PROVIDERS}


def _plans_differ(left: dict[str, float], right: dict[str, float]) -> bool:
    names = {name for name, _label in CATALOG_PROVIDERS}
    names.update(left)
    names.update(right)
    for name in names:
        if abs(float(left.get(name) or 0) - float(right.get(name) or 0)) > 1e-12:
            return True
    return False


def _missing_enabled_providers(vendor_code: str, policy: Any) -> bool:
    """已勾选的供应商如果价目里还没有对应模型，保存时需要重写目录。"""
    visible = _visible_providers({
        "pricing_mode": PRICING_MODE_CATALOG_RATE,
        "catalog_policy": policy if isinstance(policy, dict) else None,
    })
    if visible is None:
        visible = {name for name, _label in CATALOG_PROVIDERS}
    present = set()
    for model, tiers in (_cache.get(vendor_code) or {}).items():
        if not tiers:
            continue
        sample = next(iter(tiers.values()))
        present.add(catalog_provider(sample.get("model") or model, sample.get("owned_by") or ""))
    for row in list_official_catalog():
        provider = row.get("catalog_provider") or catalog_provider(row.get("model") or "", row.get("owned_by") or "")
        if provider in visible and provider not in present:
            return True
    return False


def _visible_providers(meta: dict) -> Optional[set[str]]:
    """None 表示不按供应商过滤。关闭的供应商仍保留成本价，只是不出现在对外价目。"""
    if (meta.get("pricing_mode") or "") != PRICING_MODE_CATALOG_RATE:
        return None
    policy = meta.get("catalog_policy")
    if not isinstance(policy, dict) or not isinstance(policy.get("providers"), dict):
        return None
    visible = set()
    for name, _label in CATALOG_PROVIDERS:
        spec = policy["providers"].get(name)
        if isinstance(spec, dict) and spec.get("enabled") is False:
            continue
        visible.add(name)
    return visible


def _derive_tier(std: dict, tier: str, factor: float) -> dict:
    row = dict(std)
    for key in _PRICE_FIELDS:
        row[key] = float(row.get(key) or 0) * factor
    for key in _ORIGINAL_PRICE_FIELDS:
        if key not in row:
            continue
        original = _opt_price(row.get(key))
        row[key] = original * factor if original is not None else None
    row["service_tier"] = tier
    row["source"] = "derived"
    return row


def list_official_catalog() -> list[dict]:
    """官网标准价目录（USD / 1M tokens）。

    OpenAI / xAI 来自 MODEL_MAP_MONEY_DICT；Claude、Gemini、DeepSeek、智谱来自
    OFFICIAL_PROVIDER_MODEL_MONEY_DICT。中转 catalog_rate：成本价 = 本目录 × 倍率。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for model, prices in MODEL_MAP_MONEY_DICT.items():
        if not isinstance(prices, dict):
            continue
        owned = "xai" if str(model).startswith("grok-") else "openai"
        rows.append(_tag_provider(_price_row(
            model=model,
            kind="embedding" if str(model).startswith("text-embedding-") else "chat",
            input_price=prices.get("input", 0),
            output_price=prices.get("output", 0),
            cache_read_price=prices.get("cache_read", 0),
            cache_write_price=prices.get("cache_write", 0),
            owned_by=owned,
            raw=prices,
            source="official_catalog",
        )))
        seen.add(model)
    for model, prices in IMAGE_MODEL_MONEY_DICT.items():
        if not isinstance(prices, dict):
            continue
        rows.append(_tag_provider(_price_row(
            model=model,
            kind="image",
            input_price=prices.get("text_input", 0),
            output_price=prices.get("output", 0),
            text_input_price=prices.get("text_input", 0),
            image_input_price=prices.get("image_input", 0),
            cache_read_price=prices.get("text_cache", 0),
            owned_by="openai",
            raw=prices,
            source="official_catalog",
        )))
        seen.add(model)
    # Claude / Gemini / DeepSeek / 智谱用官网标准价，不用 Teamo 折后价。
    for model, prices in OFFICIAL_PROVIDER_MODEL_MONEY_DICT.items():
        if model in seen or not isinstance(prices, dict):
            continue
        rows.append(_tag_provider(_price_row(
            model=model,
            kind="chat",
            input_price=prices.get("input", 0),
            output_price=prices.get("output", 0),
            cache_read_price=prices.get("cache_read", 0),
            cache_write_price=prices.get("cache_write", 0),
            raw=prices,
            source="official_catalog",
        )))
        seen.add(model)
    for model, prices in OFFICIAL_PROVIDER_IMAGE_MODEL_MONEY_DICT.items():
        if model in seen or not isinstance(prices, dict):
            continue
        rows.append(_tag_provider(_price_row(
            model=model,
            kind="image",
            input_price=prices.get("text_input", 0),
            output_price=prices.get("output", 0),
            text_input_price=prices.get("text_input", 0),
            image_input_price=prices.get("image_input", 0),
            cache_read_price=prices.get("text_cache", 0),
            raw=prices,
            source="official_catalog",
        )))
        seen.add(model)
    rows.sort(key=lambda item: (item.get("catalog_provider") or "", item["model"]))
    return rows


def scale_catalog_rows(
    rate: float,
    *,
    catalog: Optional[list[dict]] = None,
    policy: Any = None,
) -> list[dict]:
    """标准目录 × 倍率 → 供应商成本价行。policy 里的分供应商倍率覆盖统一倍率。"""
    default_rate = float(rate)
    if default_rate <= 0:
        raise ValueError("cost_rate must be > 0")
    source = catalog if catalog is not None else list_official_catalog()
    scaled: list[dict] = []
    for item in source:
        row = dict(item)
        provider = row.get("catalog_provider") or catalog_provider(row.get("model") or "", row.get("owned_by") or "")
        factor = provider_cost_factor(provider, default_rate, policy)
        # 只写成本价 = 官方 × 接入倍率。原价必须管理员手工设置，绝不能随目录重算自动写入，
        # 否则会误触「原价 1:1」计费（跳过加价/用户倍率/折扣）。
        for key in _PRICE_FIELDS:
            row[key] = float(row.get(key) or 0) * factor
        for key in _ORIGINAL_PRICE_FIELDS:
            row.pop(key, None)
        raw = row.get("raw")
        if isinstance(raw, dict) and (raw.get("audio_input") is not None or raw.get("audio_output") is not None):
            raw = dict(raw)
            for key in ("audio_input", "audio_output", "input", "output", "cache_read", "cache_write"):
                if raw.get(key) is not None:
                    raw[key] = float(raw.get(key) or 0) * factor
            row["raw"] = raw
        row["catalog_provider"] = provider
        row["source"] = "catalog_rate"
        row["catalog_rate"] = factor
        scaled.append(row)
    return scaled


async def clear_auto_filled_original_prices() -> int:
    """清掉曾被 scale_catalog_rows 误写入的原价（与官方目录价一致的行）。

    手工设置且与官方目录不同的原价会保留；恰好等于官方价的手工原价也会被清掉，需重填。
    """
    official = {
        (item["model"], str(item.get("service_tier") or "standard").lower()): item
        for item in list_official_catalog()
    }
    cleared = 0
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(VendorModelPrice))).scalars().all()
        for db_row in rows:
            key = (db_row.model, str(db_row.service_tier or "standard").lower())
            catalog = official.get(key)
            if not catalog:
                continue
            # 仅当原价与官方目录一致时视为误写入
            oi = getattr(db_row, "original_input_price", None)
            oo = getattr(db_row, "original_output_price", None)
            if oi is None and oo is None:
                continue
            catalog_in = float(catalog.get("input") or 0)
            catalog_out = float(catalog.get("output") or 0)
            matches_input = oi is None or catalog_in <= 0 or abs(float(oi) - catalog_in) < 1e-9
            matches_output = oo is None or catalog_out <= 0 or abs(float(oo) - catalog_out) < 1e-9
            if not (matches_input and matches_output):
                continue
            db_row.original_input_price = None
            db_row.original_output_price = None
            db_row.original_cache_read_price = None
            db_row.original_cache_write_price = None
            db_row.original_text_input_price = None
            db_row.original_image_input_price = None
            cleared += 1
        if cleared:
            await db.commit()
    if cleared:
        await reload_cache_from_db()
    return cleared


def _builtin_seed_catalog(vendor_code: str) -> list[dict]:
    rows: list[dict] = []
    if vendor_code == "teamo":
        for model, prices in TEAMO_MODEL_MONEY_DICT.items():
            rows.append(_price_row(
                model=model,
                input_price=prices.get("input", 0),
                output_price=prices.get("output", 0),
                cache_read_price=prices.get("cache_read", 0),
                cache_write_price=prices.get("cache_write", 0),
                source="builtin",
            ))
        for model, prices in TEAMO_FAST_MODEL_MONEY_DICT.items():
            rows.append(_price_row(
                model=model,
                service_tier="fast",
                input_price=prices.get("input", 0),
                output_price=prices.get("output", 0),
                cache_read_price=prices.get("cache_read", 0),
                cache_write_price=prices.get("cache_write", 0),
                source="builtin",
            ))
        for model, prices in TEAMO_IMAGE_MODEL_MONEY_DICT.items():
            if not isinstance(prices, dict):
                continue
            rows.append(_price_row(
                model=model,
                kind="image",
                input_price=prices.get("text_input", 0),
                output_price=prices.get("output", 0),
                text_input_price=prices.get("text_input", 0),
                image_input_price=prices.get("image_input", 0),
                cache_read_price=prices.get("text_cache", 0),
                owned_by="openai" if str(model).startswith("gpt-image") else "google",
                raw=prices,
                source="builtin",
            ))
        return rows

    for model, prices in MODEL_MAP_MONEY_DICT.items():
        rows.append(_price_row(
            model=model,
            input_price=prices.get("input", 0),
            output_price=prices.get("output", 0),
            cache_read_price=prices.get("cache_read", 0),
            cache_write_price=prices.get("cache_write", 0),
            source="builtin",
        ))
    for model, prices in IMAGE_MODEL_MONEY_DICT.items():
        rows.append(_price_row(
            model=model,
            kind="image",
            input_price=prices.get("text_input", 0),
            output_price=prices.get("output", 0),
            text_input_price=prices.get("text_input", 0),
            image_input_price=prices.get("image_input", 0),
            cache_read_price=prices.get("text_cache", 0),
            source="builtin",
        ))
    return rows


def _is_teamo_image_model(model_id: str, cost: dict, item: dict) -> bool:
    mid = (model_id or "").lower()
    if item.get("per_call_tiers"):
        return True
    if isinstance(cost, dict) and "image_input" in cost:
        return True
    if mid.startswith("gpt-image") or mid.endswith("-image") or "-image-" in mid:
        return True
    return False


def _teamo_remote_row(item: dict) -> list[dict]:
    model_id = str(item.get("id") or "").strip()
    cost = item.get("cost") or {}
    if not model_id or not isinstance(cost, dict):
        return []
    kind = "image" if _is_teamo_image_model(model_id, cost, item) else "chat"
    if cost.get("input") is None or cost.get("output") is None:
        return []
    tiers = item.get("per_call_tiers") if isinstance(item.get("per_call_tiers"), list) else None
    raw = dict(item)
    if tiers:
        raw["billing"] = "per_image"
        raw["per_image_tiers"] = {
            str(t.get("id") or "").lower(): float(t.get("cost") or 0)
            for t in tiers
            if isinstance(t, dict) and t.get("id") is not None
        }
    elif kind == "image":
        raw["billing"] = "token"
    row = _price_row(
        model=model_id,
        kind=kind,
        input_price=cost.get("input", 0),
        output_price=cost.get("output", 0),
        cache_read_price=cost.get("cache_read", cost.get("cached_input", 0)),
        cache_write_price=cost.get("cache_creation", 0),
        text_input_price=cost.get("input", 0) if kind == "image" else 0,
        image_input_price=cost.get("image_input", 0),
        display_name=str(item.get("display_name") or model_id),
        owned_by=str(item.get("owned_by") or ""),
        raw=raw,
        source="remote",
    )
    return [row]


async def ensure_vendor_catalog() -> None:
    """建表后的幂等种子：写入默认供应商，并在价目为空时灌入代码快照。"""
    defaults = [
        {
            "code": "teamo",
            "name": "TeamoRouter",
            "base_url": normalize_base_url(TEAMO_UPSTREAM_BASE),
            "pricing_url": "https://teamorouter.com/v1/models/pricing",
            "is_default": UPSTREAM_PROVIDER == "teamo",
            "sort_order": 10,
        },
        {
            "code": "legacy",
            "name": "LiteLLM / Local",
            "base_url": normalize_base_url(LEGACY_UPSTREAM_BASE),
            "pricing_url": None,
            "is_default": UPSTREAM_PROVIDER == "legacy",
            "sort_order": 20,
        },
        {
            "code": "yunxin",
            "name": "Yunxin Hub",
            "base_url": normalize_base_url(YUNXIN_UPSTREAM_BASE or "https://ai.yunxinapi.com/hub"),
            "pricing_url": None,
            "is_default": UPSTREAM_PROVIDER == "yunxin",
            "sort_order": 30,
        },
    ]
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(UpstreamVendor))).scalars().all()
        by_code = {row.code: row for row in existing}
        has_default = any(bool(row.is_default) for row in existing)
        # 仅在库里还没有任何默认供应商时，用环境变量初始化；已有默认则保留，支持热切换持久化。
        if not has_default and UPSTREAM_PROVIDER in by_code:
            for row in by_code.values():
                row.is_default = row.code == UPSTREAM_PROVIDER
        for item in defaults:
            row = by_code.get(item["code"])
            if row is None:
                row = UpstreamVendor(
                    code=item["code"],
                    name=item["name"],
                    base_url=item["base_url"],
                    pricing_url=item["pricing_url"],
                    is_active=True,
                    is_default=item["is_default"],
                    sort_order=item["sort_order"],
                )
                db.add(row)
                await db.flush()
                by_code[item["code"]] = row
            else:
                row.name = item["name"]
                row.base_url = item["base_url"] or row.base_url
                if item["pricing_url"] and not row.pricing_url:
                    row.pricing_url = item["pricing_url"]
                row.is_active = True
                row.sort_order = item["sort_order"]

        for code, vendor in by_code.items():
            count = await db.scalar(
                select(VendorModelPrice.id).where(VendorModelPrice.vendor_id == vendor.id).limit(1)
            )
            if count is not None:
                continue
            for price in _builtin_seed_catalog(code):
                db.add(VendorModelPrice(
                    vendor_id=vendor.id,
                    model=price["model"],
                    service_tier=price["service_tier"],
                    kind=price["kind"],
                    display_name=price["display_name"],
                    owned_by=price["owned_by"],
                    input_price=price["input"],
                    output_price=price["output"],
                    cache_read_price=price["cache_read"],
                    cache_write_price=price["cache_write"],
                    text_input_price=price["text_input"],
                    image_input_price=price["image_input"],
                    currency="USD",
                    unit="per_1m_tokens",
                    is_active=True,
                    raw_payload=price.get("raw"),
                    source_updated_at=_now(),
                ))
        await db.commit()
    await reload_cache_from_db()


async def reload_cache_from_db() -> None:
    global _cache, _vendor_meta
    next_cache: dict[str, dict[str, dict[str, dict]]] = {}
    next_meta: dict[str, dict] = {}
    try:
        async with AsyncSessionLocal() as db:
            vendors = (await db.execute(
                select(UpstreamVendor).where(UpstreamVendor.is_active.is_(True))
                .order_by(UpstreamVendor.sort_order, UpstreamVendor.id)
            )).scalars().all()
            prices = (await db.execute(
                select(VendorModelPrice).where(VendorModelPrice.is_active.is_(True))
            )).scalars().all()
        vendor_by_id = {v.id: v for v in vendors}
        for vendor in vendors:
            next_meta[vendor.code] = {
                "id": vendor.id,
                "code": vendor.code,
                "name": vendor.name,
                "base_url": vendor.base_url,
                "api_key": (getattr(vendor, "api_key", None) or "").strip(),
                "pricing_url": vendor.pricing_url,
                "pricing_mode": getattr(vendor, "pricing_mode", None) or PRICING_MODE_MANUAL,
                "cost_rate": getattr(vendor, "cost_rate", None),
                "catalog_policy": getattr(vendor, "catalog_policy", None),
                "profit_multiplier": getattr(vendor, "profit_multiplier", None),
                "is_default": bool(vendor.is_default),
                "is_active": bool(vendor.is_active),
                "sort_order": vendor.sort_order,
                "last_synced_at": vendor.last_synced_at.isoformat() if vendor.last_synced_at else None,
            }
            next_cache[vendor.code] = {}
        for price in prices:
            vendor = vendor_by_id.get(price.vendor_id)
            if not vendor:
                continue
            row = _price_row(
                model=price.model,
                service_tier=price.service_tier or "standard",
                kind=price.kind or "chat",
                input_price=price.input_price,
                output_price=price.output_price,
                cache_read_price=price.cache_read_price,
                cache_write_price=price.cache_write_price,
                text_input_price=price.text_input_price,
                image_input_price=price.image_input_price,
                original_input_price=getattr(price, "original_input_price", None),
                original_output_price=getattr(price, "original_output_price", None),
                original_cache_read_price=getattr(price, "original_cache_read_price", None),
                original_cache_write_price=getattr(price, "original_cache_write_price", None),
                original_text_input_price=getattr(price, "original_text_input_price", None),
                original_image_input_price=getattr(price, "original_image_input_price", None),
                display_name=price.display_name or price.model,
                owned_by=price.owned_by or "",
                raw=price.raw_payload,
                source="database",
            )
            vendor_map = next_cache.setdefault(vendor.code, {})
            model_map = vendor_map.setdefault(row["model"], {})
            model_map[row["service_tier"]] = row
    except Exception:
        process_log.exception("reload vendor catalog failed")
        return
    _cache = next_cache
    _vendor_meta = next_meta


def active_vendor_code() -> str:
    """当前生效供应商：以 DB is_default 为准（支持热切换），否则回退环境变量。"""
    for code, meta in _vendor_meta.items():
        if meta.get("is_default"):
            return code
    if UPSTREAM_PROVIDER in _vendor_meta:
        return UPSTREAM_PROVIDER
    if _vendor_meta:
        return next(iter(_vendor_meta))
    return UPSTREAM_PROVIDER or "teamo"


def get_runtime_upstream(*, vendor: str = "") -> dict:
    """请求转发/计费使用的运行时上游（读内存缓存，由 supervisor/管理接口刷新）。"""
    meta = resolve_vendor(vendor=vendor) if vendor else resolve_vendor()
    code = (meta or {}).get("code") or (vendor.strip().lower() if vendor else active_vendor_code())
    base = normalize_base_url((meta or {}).get("base_url") or "") or {
        "teamo": normalize_base_url(TEAMO_UPSTREAM_BASE),
        "legacy": normalize_base_url(LEGACY_UPSTREAM_BASE),
        "yunxin": normalize_base_url(YUNXIN_UPSTREAM_BASE or "https://ai.yunxinapi.com/hub"),
    }.get(code, normalize_base_url(TEAMO_UPSTREAM_BASE))
    return {
        "code": code,
        "name": (meta or {}).get("name") or code,
        "base_url": base,
        "pricing_url": (meta or {}).get("pricing_url"),
        "teamo": code == "teamo",
        "yunxin": code == "yunxin",
        "legacy": code not in {"teamo", "yunxin"},
        "last_synced_at": (meta or {}).get("last_synced_at"),
        "profit_multiplier": (meta or {}).get("profit_multiplier"),
    }


def resolve_vendor(*, vendor: str = "", base_url: str = "") -> Optional[dict]:
    code = (vendor or "").strip().lower()
    if code and code in _vendor_meta:
        return dict(_vendor_meta[code])
    normalized = normalize_base_url(base_url)
    if normalized:
        for meta in _vendor_meta.values():
            if normalize_base_url(meta.get("base_url") or "") == normalized:
                return dict(meta)
    code = active_vendor_code()
    meta = _vendor_meta.get(code)
    return dict(meta) if meta else None


def vendor_profit_multiplier(vendor: str = "", fallback: Optional[float] = None) -> float:
    """供应商单独加价系数；未设置或非法时回落全局/传入 fallback。"""
    meta = resolve_vendor(vendor=vendor) if vendor else resolve_vendor()
    raw = (meta or {}).get("profit_multiplier")
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    try:
        fallback_value = float(fallback)
        if fallback_value > 0:
            return fallback_value
    except (TypeError, ValueError):
        pass
    return float(PROFIT_MULTIPLIER)


def _mask_api_key(key: str) -> str:
    raw = (key or "").strip()
    if not raw:
        return ""
    if len(raw) <= 8:
        return "****"
    return f"{raw[:4]}…{raw[-4:]}"


def vendor_api_key(code: str = "") -> str:
    """某供应商自己的上游密钥。未配置时返回空串，调用方回落全局密钥。"""
    code = (code or "").strip().lower()
    meta = _vendor_meta.get(code) if code else resolve_vendor()
    return ((meta or {}).get("api_key") or "").strip()


def redact_vendor_meta(meta: Optional[dict], *, admin: bool = False) -> dict:
    item = dict(meta or {})
    raw = (item.pop("api_key", None) or "").strip()
    if admin:
        item["has_api_key"] = bool(raw)
        item["api_key_hint"] = _mask_api_key(raw)
    return item


def list_vendors(*, include_pricing_meta: bool = False) -> list[dict]:
    items = []
    for meta in sorted(_vendor_meta.values(), key=lambda x: (x.get("sort_order", 100), x["code"])):
        catalog = _cache.get(meta["code"], {})
        item = {
            **meta,
            "model_count": len(catalog),
            "is_current": meta["code"] == active_vendor_code(),
        }
        raw_key = (item.pop("api_key", None) or "").strip()
        if include_pricing_meta:
            item["has_api_key"] = bool(raw_key)
            item["api_key_hint"] = _mask_api_key(raw_key)
        else:
            # 前台/普通鉴权不暴露接入倍率、计价模式、分类型策略和上游密钥
            item.pop("pricing_mode", None)
            item.pop("cost_rate", None)
            item.pop("catalog_policy", None)
            item.pop("profit_multiplier", None)
        items.append(item)
    return items


def lookup_vendor_price(
    model: str,
    *,
    vendor: str = "",
    base_url: str = "",
    service_tier: str = "standard",
) -> Optional[dict]:
    meta = resolve_vendor(vendor=vendor, base_url=base_url)
    if not meta:
        return None
    code = meta["code"]
    model = (model or "").strip()
    if code == "teamo":
        model = TEAMO_MODEL_ALIASES.get(model, model)
    vendor_catalog = _cache.get(code) or {}
    from utils.check_user import match_price_key
    matched = match_price_key(model, vendor_catalog)
    tiers = vendor_catalog.get(matched) or {} if matched else {}
    tier = service_tier if service_tier in {"fast", "priority", "standard", "batch"} else "standard"
    if tier in {"fast", "priority"} and "fast" in tiers:
        row = tiers["fast"]
    else:
        row = tiers.get("standard") or tiers.get(tier)
    if not row:
        return None
    if row.get("kind") == "image":
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        tiers = raw.get("per_image_tiers") if isinstance(raw.get("per_image_tiers"), dict) else None
        billing = raw.get("billing") or ("per_image" if tiers else "token")
        out = {
            "kind": "image",
            "billing": billing,
            "text_input": row.get("text_input") or row.get("input") or 0,
            "image_input": row.get("image_input") or 0,
            "output": row.get("output") or 0,
            "text_cache": row.get("cache_read") or 0,
            "image_cache": 0,
            "original_text_input": row.get("original_text_input"),
            "original_image_input": row.get("original_image_input"),
            "original_output": row.get("original_output"),
        }
        if tiers:
            out["per_image_tiers"] = {str(k).lower(): float(v or 0) for k, v in tiers.items()}
        return out
    return {
        "input": row.get("input") or 0,
        "output": row.get("output") or 0,
        "cache_read": row.get("cache_read") or 0,
        "cache_write": row.get("cache_write") or 0,
        "original_input": row.get("original_input"),
        "original_output": row.get("original_output"),
        "original_cache_read": row.get("original_cache_read"),
        "original_cache_write": row.get("original_cache_write"),
    }


def get_vendor_catalog(*, vendor: str = "", base_url: str = "") -> tuple[Optional[dict], list[dict]]:
    meta = resolve_vendor(vendor=vendor, base_url=base_url)
    if not meta:
        return None, []
    catalog = _cache.get(meta["code"]) or {}
    visible = _visible_providers(meta)
    rows: list[dict] = []
    for model, tiers in catalog.items():
        std = tiers.get("standard")
        sample = std or next(iter(tiers.values()))
        provider = catalog_provider(sample.get("model") or model, sample.get("owned_by") or "")
        if visible is not None and provider not in visible:
            continue
        for price in tiers.values():
            rows.append(dict(price))
        if "fast" not in tiers and std and meta["code"] != "teamo" and std.get("kind") != "image":
            rows.append(_derive_tier(std, "fast", 2.0))
    rows.sort(key=lambda item: (item["model"], item["service_tier"]))
    return meta, rows


def list_admin_vendor_models(vendor_code: str, *, include_inactive: bool = False) -> tuple[Optional[dict], list[dict]]:
    """管理端：返回某供应商原始美元成本价（不含用户倍率）。"""
    meta = resolve_vendor(vendor=vendor_code)
    if not meta:
        return None, []
    catalog = _cache.get(meta["code"]) or {}
    rows: list[dict] = []
    for model, tiers in catalog.items():
        for tier, price in tiers.items():
            row = {
                "model": price.get("model") or model,
                "display_name": price.get("display_name") or model,
                "owned_by": price.get("owned_by") or "",
                "service_tier": price.get("service_tier") or tier,
                "kind": price.get("kind") or "chat",
                "currency": "USD",
                "unit": "per_1m_tokens",
                "input": float(price.get("input") or 0),
                "output": float(price.get("output") or 0),
                "cache_read": float(price.get("cache_read") or 0),
                "cache_write": float(price.get("cache_write") or 0),
                "text_input": float(price.get("text_input") or 0),
                "image_input": float(price.get("image_input") or 0),
                "original_input": _opt_price(price.get("original_input")),
                "original_output": _opt_price(price.get("original_output")),
                "original_cache_read": _opt_price(price.get("original_cache_read")),
                "original_cache_write": _opt_price(price.get("original_cache_write")),
                "original_text_input": _opt_price(price.get("original_text_input")),
                "original_image_input": _opt_price(price.get("original_image_input")),
                "is_active": True,
                "source": price.get("source") or "database",
            }
            rows.append(row)
    rows.sort(key=lambda item: (item["model"], item["service_tier"]))
    return meta, rows


def get_admin_vendor_model(vendor_code: str, model: str, *, service_tier: str = "standard") -> Optional[dict]:
    meta, rows = list_admin_vendor_models(vendor_code)
    if not meta:
        return None
    model = (model or "").strip()
    tier = (service_tier or "standard").strip().lower() or "standard"
    for row in rows:
        if row["model"] == model and row["service_tier"] == tier:
            return {"vendor": meta, "price": row}
    return {"vendor": meta, "price": None}


async def upsert_vendor_prices(vendor_code: str, rows: list[dict], *, source: str = "manual") -> int:
    async with AsyncSessionLocal() as db:
        vendor = (await db.execute(
            select(UpstreamVendor).where(UpstreamVendor.code == vendor_code)
        )).scalars().first()
        if vendor is None:
            raise ValueError(f"unknown vendor: {vendor_code}")
        existing = (await db.execute(
            select(VendorModelPrice).where(VendorModelPrice.vendor_id == vendor.id)
        )).scalars().all()
        by_key = {(row.model, row.service_tier): row for row in existing}
        touched = set()
        for item in rows:
            model = str(item.get("model") or "").strip()
            if not model:
                continue
            tier = str(item.get("service_tier") or "standard").strip().lower() or "standard"
            key = (model, tier)
            touched.add(key)
            db_row = by_key.get(key)
            if db_row is None:
                db_row = VendorModelPrice(vendor_id=vendor.id, model=model, service_tier=tier)
                db.add(db_row)
                by_key[key] = db_row
            db_row.kind = str(item.get("kind") or "chat")
            db_row.display_name = str(item.get("display_name") or model)
            db_row.owned_by = str(item.get("owned_by") or "")
            db_row.input_price = float(item.get("input") or 0)
            db_row.output_price = float(item.get("output") or 0)
            db_row.cache_read_price = float(item.get("cache_read") or 0)
            db_row.cache_write_price = float(item.get("cache_write") or 0)
            db_row.text_input_price = float(item.get("text_input") or 0)
            db_row.image_input_price = float(item.get("image_input") or 0)
            # 仅更新请求里出现的原价字段（含显式 null/0 清空）；未带则保留
            _original_column = {
                "original_input": "original_input_price",
                "original_output": "original_output_price",
                "original_cache_read": "original_cache_read_price",
                "original_cache_write": "original_cache_write_price",
                "original_text_input": "original_text_input_price",
                "original_image_input": "original_image_input_price",
            }
            for api_key, column in _original_column.items():
                if api_key in item:
                    setattr(db_row, column, _opt_price(item.get(api_key)))
            db_row.currency = "USD"
            db_row.unit = "per_1m_tokens"
            if "is_active" in item and item.get("is_active") is not None:
                db_row.is_active = bool(item.get("is_active"))
            else:
                db_row.is_active = True
            db_row.raw_payload = item.get("raw")
            db_row.source_updated_at = _now()
        if source == "remote":
            for key, db_row in by_key.items():
                if key not in touched and db_row.is_active:
                    db_row.is_active = False
        if source != "remote":
            vendor.last_synced_at = vendor.last_synced_at or _now()
        else:
            vendor.last_synced_at = _now()
        await db.commit()
    await reload_cache_from_db()
    return len(rows)


async def upsert_single_vendor_model(vendor_code: str, item: dict) -> dict:
    """新增或覆盖单个模型+档位价格。"""
    model = str(item.get("model") or "").strip()
    if not model:
        raise ValueError("model is required")
    await upsert_vendor_prices(vendor_code, [item], source="manual")
    found = get_admin_vendor_model(
        vendor_code, model, service_tier=str(item.get("service_tier") or "standard"),
    )
    if not found or not found.get("price"):
        raise ValueError("upsert failed")
    return found


async def delete_vendor_model_price(vendor_code: str, model: str, *, service_tier: str = "standard") -> dict:
    """软删除：is_active=false，立即从计费缓存移除。"""
    code = (vendor_code or "").strip().lower()
    model = (model or "").strip()
    tier = (service_tier or "standard").strip().lower() or "standard"
    async with AsyncSessionLocal() as db:
        vendor = (await db.execute(
            select(UpstreamVendor).where(UpstreamVendor.code == code)
        )).scalars().first()
        if vendor is None:
            raise ValueError(f"unknown vendor: {code}")
        row = (await db.execute(
            select(VendorModelPrice).where(
                VendorModelPrice.vendor_id == vendor.id,
                VendorModelPrice.model == model,
                VendorModelPrice.service_tier == tier,
            )
        )).scalars().first()
        if row is None:
            raise ValueError(f"model price not found: {model}/{tier}")
        row.is_active = False
        row.source_updated_at = _now()
        await db.commit()
    await reload_cache_from_db()
    return {"vendor": code, "model": model, "service_tier": tier, "deleted": True}


async def refresh_vendor_prices(vendor_code: str = "", *, force: bool = False) -> dict:
    code = (vendor_code or active_vendor_code()).strip().lower()
    meta = _vendor_meta.get(code) or resolve_vendor(vendor=code)
    if not meta:
        return {"ok": False, "updated": False, "vendor": code, "error": "vendor not found"}
    pricing_url = meta.get("pricing_url")
    if not pricing_url:
        return {"ok": True, "updated": False, "vendor": code, "count": len(_cache.get(code) or {}), "note": "no pricing_url"}
    now = time.monotonic()
    if not force and now - _last_refresh.get(code, 0) < _REFRESH_INTERVAL:
        return {"ok": True, "updated": False, "vendor": code, "count": len(_cache.get(code) or {})}
    async with _refresh_lock:
        now = time.monotonic()
        if not force and now - _last_refresh.get(code, 0) < _REFRESH_INTERVAL:
            return {"ok": True, "updated": False, "vendor": code, "count": len(_cache.get(code) or {})}
        try:
            client_kwargs = {
                "timeout": httpx.Timeout(20.0, connect=8.0),
                "trust_env": False,
            }
            if code == "teamo" and TEAMO_HTTP_PROXY:
                client_kwargs["proxy"] = TEAMO_HTTP_PROXY
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(pricing_url)
                resp.raise_for_status()
                payload = resp.json()
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                raise ValueError("pricing payload missing models[]")
            rows: list[dict] = []
            for item in models:
                if isinstance(item, dict):
                    rows.extend(_teamo_remote_row(item))
            if code == "teamo":
                rows.extend(
                    _price_row(
                        model=model,
                        service_tier="fast",
                        input_price=prices.get("input", 0),
                        output_price=prices.get("output", 0),
                        cache_read_price=prices.get("cache_read", 0),
                        cache_write_price=prices.get("cache_write", 0),
                        source="builtin",
                    )
                    for model, prices in TEAMO_FAST_MODEL_MONEY_DICT.items()
                )
            count = await upsert_vendor_prices(code, rows, source="remote")
            _last_refresh[code] = time.monotonic()
            process_log.info("vendor pricing refreshed vendor=%s count=%s url=%s", code, count, pricing_url)
            return {"ok": True, "updated": True, "vendor": code, "count": count, "source": pricing_url}
        except Exception as exc:
            process_log.warning("vendor pricing refresh failed vendor=%s err=%r", code, exc)
            return {
                "ok": False,
                "updated": False,
                "vendor": code,
                "count": len(_cache.get(code) or {}),
                "error": repr(exc) or str(exc) or exc.__class__.__name__,
                "source": pricing_url,
            }


async def pricing_supervisor(stop_event: asyncio.Event) -> None:
    """定期：1) 从 DB 热加载供应商/价目（多 worker 也能跟上管理端改动）2) 拉取有 pricing_url 的远程价。"""
    while not stop_event.is_set():
        await reload_cache_from_db()
        try:
            from utils.model_route import reload_routes
            await reload_routes()
        except Exception:
            process_log.exception("reload model routes in supervisor failed")
        try:
            from utils.model_protocol import reload_protocols
            await reload_protocols()
        except Exception:
            process_log.exception("reload model protocols in supervisor failed")
        try:
            from utils.model_alias import reload_aliases
            await reload_aliases()
        except Exception:
            process_log.exception("reload model aliases in supervisor failed")
        try:
            from utils.model_rewrite import reload_rewrites
            await reload_rewrites()
        except Exception:
            process_log.exception("reload model rewrites in supervisor failed")
        try:
            from utils.prompt_injection import reload_injections
            await reload_injections()
        except Exception:
            process_log.exception("reload prompt injections in supervisor failed")
        for code, meta in list(_vendor_meta.items()):
            if meta.get("pricing_url"):
                await refresh_vendor_prices(code)
        if "teamo" in _vendor_meta:
            try:
                from utils.teamo_billing import fetch_teamo_balance
                await fetch_teamo_balance(vendor_base_url=(_vendor_meta["teamo"] or {}).get("base_url") or "")
            except Exception:
                process_log.exception("refresh teamo balance in supervisor failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=min(30.0, _REFRESH_INTERVAL))
        except asyncio.TimeoutError:
            pass


async def activate_vendor(code: str) -> dict:
    """把某供应商设为当前默认上游：立即改 DB + 刷新缓存，无需重启进程。"""
    code = (code or "").strip().lower()
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(UpstreamVendor))).scalars().all()
        target = next((row for row in rows if row.code == code), None)
        if target is None:
            raise ValueError(f"unknown vendor: {code}")
        if not target.is_active:
            raise ValueError(f"vendor inactive: {code}")
        for row in rows:
            row.is_default = row.code == code
        await db.commit()
    await reload_cache_from_db()
    return get_runtime_upstream()


async def apply_catalog_rate_prices(vendor_code: str, cost_rate: float, policy: Any = None) -> int:
    """用官方标准目录 × 倍率覆盖写入该供应商价目。分类型倍率来自 catalog_policy。"""
    code = (vendor_code or "").strip().lower()
    if policy is None:
        meta = resolve_vendor(vendor=code)
        policy = (meta or {}).get("catalog_policy")
    rows = scale_catalog_rows(cost_rate, policy=policy)
    # source=remote：未出现在目录中的旧行会软删，保证价目与标准目录一致
    return await upsert_vendor_prices(code, rows, source="remote")


async def create_or_update_vendor(
    *,
    code: str,
    name: str,
    base_url: str,
    pricing_url: Optional[str] = None,
    pricing_mode: str = PRICING_MODE_MANUAL,
    cost_rate: Optional[float] = None,
    catalog_policy: Any = None,
    profit_multiplier: Optional[float] = None,
    api_key: Optional[str] = None,
    is_active: bool = True,
    is_default: bool = False,
    sort_order: int = 100,
    reapply_catalog: bool = False,
) -> dict:
    code = (code or "").strip().lower()
    base_url = normalize_base_url(base_url)
    if not code or not base_url:
        raise ValueError("code and base_url are required")
    mode = (pricing_mode or PRICING_MODE_MANUAL).strip().lower()
    if mode not in VALID_PRICING_MODES:
        raise ValueError(f"pricing_mode must be one of {sorted(VALID_PRICING_MODES)}")
    rate_value: Optional[float] = None
    if cost_rate is not None and cost_rate != "":
        rate_value = float(cost_rate)
        if rate_value <= 0 or rate_value > 100:
            raise ValueError("cost_rate must be > 0 and <= 100")
    if mode == PRICING_MODE_CATALOG_RATE and rate_value is None:
        raise ValueError("cost_rate is required when pricing_mode=catalog_rate")
    profit_value: Optional[float] = None
    if profit_multiplier is not None and profit_multiplier != "":
        profit_value = float(profit_multiplier)
        if profit_value <= 0 or profit_value > 100:
            raise ValueError("profit_multiplier must be > 0 and <= 100")
    if mode == PRICING_MODE_CATALOG_RATE:
        # 目录倍率模式不走远程价目拉取，避免被 sync 覆盖
        pricing_url = None

    prev_mode = None
    prev_rate = None
    prev_policy = None
    stored_policy = None
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(UpstreamVendor).where(UpstreamVendor.code == code))).scalars().first()
        if row is None:
            row = UpstreamVendor(code=code)
            db.add(row)
        else:
            prev_mode = getattr(row, "pricing_mode", None) or PRICING_MODE_MANUAL
            prev_rate = getattr(row, "cost_rate", None)
            prev_policy = getattr(row, "catalog_policy", None)
        if catalog_policy is not None:
            stored_policy = normalize_catalog_policy(catalog_policy)
        else:
            stored_policy = prev_policy
        row.name = name or code
        row.base_url = base_url
        row.pricing_url = (pricing_url or "").strip() or None
        row.pricing_mode = mode
        row.cost_rate = rate_value if mode == PRICING_MODE_CATALOG_RATE else None
        row.catalog_policy = stored_policy
        row.profit_multiplier = profit_value
        if api_key is not None:
            row.api_key = (api_key or "").strip() or None
        row.is_active = bool(is_active)
        row.sort_order = int(sort_order)
        if is_default:
            others = (await db.execute(select(UpstreamVendor))).scalars().all()
            for other in others:
                other.is_default = other.code == code
            row.is_default = True
        await db.commit()

    compare_rate = float(rate_value if rate_value is not None else (prev_rate or 0) or 0)
    policy_rates_changed = (
        mode == PRICING_MODE_CATALOG_RATE
        and compare_rate > 0
        and _plans_differ(provider_rate_plan(prev_policy, compare_rate), provider_rate_plan(stored_policy, compare_rate))
    )
    missing_providers = mode == PRICING_MODE_CATALOG_RATE and _missing_enabled_providers(code, stored_policy)
    should_apply = mode == PRICING_MODE_CATALOG_RATE and (
        reapply_catalog
        or policy_rates_changed
        or missing_providers
        or prev_mode != mode
        or (prev_rate is None and rate_value is not None)
        or (prev_rate is not None and rate_value is not None and abs(float(prev_rate) - float(rate_value)) > 1e-12)
        or prev_mode is None  # 新建
    )
    if should_apply and rate_value is not None:
        await apply_catalog_rate_prices(code, rate_value, stored_policy)
    else:
        await reload_cache_from_db()

    meta = resolve_vendor(vendor=code) or {
        "code": code,
        "base_url": base_url,
        "pricing_mode": mode,
        "cost_rate": rate_value,
        "catalog_policy": stored_policy,
    }
    return redact_vendor_meta(meta, admin=True)


async def ensure_vendor_schema_columns(conn) -> None:
    """幂等补齐 tb_upstream_vendor / 模型原价等新列（已有库升级用）。"""
    for ddl in (
        "ALTER TABLE tb_upstream_vendor ADD COLUMN pricing_mode VARCHAR(32) NOT NULL DEFAULT 'manual'",
        "ALTER TABLE tb_upstream_vendor ADD COLUMN cost_rate DOUBLE NULL",
        "ALTER TABLE tb_upstream_vendor ADD COLUMN profit_multiplier DOUBLE NULL",
        "ALTER TABLE tb_upstream_vendor ADD COLUMN api_key VARCHAR(512) NULL",
        "ALTER TABLE tb_upstream_vendor ADD COLUMN catalog_policy JSON NULL",
        "ALTER TABLE tb_model_route ADD COLUMN api_key VARCHAR(512) NULL",
        "ALTER TABLE tb_model_route ADD COLUMN user_ids JSON NULL",
        "ALTER TABLE tb_user ADD COLUMN phone_key_revoked TINYINT(1) NOT NULL DEFAULT 0",
        "UPDATE tb_user u SET phone_key_revoked = 1 WHERE phone_key_revoked = 0 AND EXISTS (SELECT 1 FROM tb_user_api_key k WHERE k.user_id = u.user_id)",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_input_price DOUBLE NULL",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_output_price DOUBLE NULL",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_cache_read_price DOUBLE NULL",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_cache_write_price DOUBLE NULL",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_text_input_price DOUBLE NULL",
        "ALTER TABLE tb_vendor_model_price ADD COLUMN original_image_input_price DOUBLE NULL",
    ):
        try:
            await conn.execute(text(ddl))
        except Exception as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "1060" in msg:
                continue
            # 表尚不存在时由 create(checkfirst=True) 负责
            if "doesn't exist" in msg or "1146" in msg:
                continue
            raise
