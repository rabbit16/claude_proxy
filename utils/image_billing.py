import base64
import re
import struct
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select

from settings.database import AsyncSessionLocal
from settings.macro import (
    CACHE_READ_MULTIPLIER,
    DEFAULT_IMAGE_MODEL,
    DOR2CHY,
    IMAGE_MODEL_MONEY_DICT,
    PROFIT_MULTIPLIER,
    TEAMO_IMAGE_MODEL_MONEY_DICT,
    TEAMO_MODEL_ALIASES,
)
from settings.models import User
from utils.check_user import billing_timing, invalidate_user_auth, normalize_model_name
from utils.request_usage import apply_billed_usage
from utils.get_log import billing_log


def lookup_price_in(table: dict, model: str):
    model = normalize_model_name(model)
    if not model:
        return None
    if model in table:
        return table[model]
    for key in sorted(table, key=len, reverse=True):
        if not model.startswith(key):
            continue
        rest = model[len(key):]
        if not rest or rest.startswith("-"):
            return table[key]
    return None


def lookup_image_price(model: str, default: str = DEFAULT_IMAGE_MODEL, teamo: bool = False, vendor: str = ""):
    """查找绘图模型价。teamo=True 时优先 Teamo 官网/快照价（含按张档位）。"""
    model = normalize_model_name(model)
    vendor = (vendor or "").strip().lower()
    if vendor:
        try:
            from utils.vendor_catalog import lookup_vendor_price
            live = lookup_vendor_price(model, vendor=vendor)
            if live and live.get("kind") == "image":
                return live
        except Exception:
            pass
        if vendor == "teamo":
            aliased = TEAMO_MODEL_ALIASES.get(model, model)
            raw = lookup_price_in(TEAMO_IMAGE_MODEL_MONEY_DICT, aliased)
            return dict(raw) if raw else None
        if vendor not in {"teamo", "yunxin"}:
            raw = lookup_price_in(IMAGE_MODEL_MONEY_DICT, model)
            if raw:
                return raw
            if not model and default:
                return lookup_price_in(IMAGE_MODEL_MONEY_DICT, default)
        return None
    if teamo:
        model = TEAMO_MODEL_ALIASES.get(model, model)
        try:
            from utils.vendor_catalog import lookup_vendor_price
            live = lookup_vendor_price(model, vendor="teamo")
            if live and live.get("kind") == "image":
                return live
        except Exception:
            pass
        raw = lookup_price_in(TEAMO_IMAGE_MODEL_MONEY_DICT, model)
        if raw:
            return dict(raw)
        return None

    raw = lookup_price_in(IMAGE_MODEL_MONEY_DICT, model)
    if raw:
        return raw
    if not model and default:
        return lookup_price_in(IMAGE_MODEL_MONEY_DICT, default)
    return None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def resolve_image_size_tier(
    width: Any = None,
    height: Any = None,
    size: Any = None,
) -> str:
    """Teamo GPT Image：按输出最长边分档。<=1024→1k，<=2048→2k，其余→4k。"""
    w = _to_int(width)
    h = _to_int(height)
    longest = max(w or 0, h or 0)
    if longest <= 0 and isinstance(size, str):
        size = size.strip().lower()
        if size in {"1k", "2k", "4k"}:
            return size
        if "x" in size:
            parts = size.split("x", 1)
            try:
                longest = max(int(parts[0]), int(parts[1]))
            except (TypeError, ValueError, IndexError):
                longest = 0
    if longest <= 0:
        return "1k"
    if longest <= 1024:
        return "1k"
    if longest <= 2048:
        return "2k"
    return "4k"


def _png_size(raw: bytes) -> Tuple[Optional[int], Optional[int]]:
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    w, h = struct.unpack(">II", raw[16:24])
    return int(w), int(h)


def _jpeg_size(raw: bytes) -> Tuple[Optional[int], Optional[int]]:
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        return None, None
    i = 2
    while i < len(raw) - 8:
        if raw[i] != 0xFF:
            break
        marker = raw[i + 1]
        if marker == 0xD8:
            i += 2
            continue
        if marker == 0xD9:
            break
        seglen = int.from_bytes(raw[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            h = int.from_bytes(raw[i + 5:i + 7], "big")
            w = int.from_bytes(raw[i + 7:i + 9], "big")
            return w, h
        i += 2 + seglen
    return None, None


def decode_image_dimensions(b64_or_bytes: Any) -> Tuple[Optional[int], Optional[int]]:
    raw = b64_or_bytes
    if isinstance(raw, str):
        try:
            raw = base64.b64decode(raw, validate=False)
        except Exception:
            return None, None
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None, None
    raw = bytes(raw)
    w, h = _png_size(raw)
    if w and h:
        return w, h
    return _jpeg_size(raw)


def _first_image_item(obj: dict) -> dict:
    data = obj.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


def extract_image_usage(obj: Any) -> Dict[str, Any]:
    """从 Images API / Gemini generateContent / LiteLLM 包装响应里抽出 usage。"""
    if not isinstance(obj, dict):
        return {}

    # Gemini native image response
    usage_meta = obj.get("usageMetadata")
    if isinstance(usage_meta, dict) and (
        usage_meta.get("promptTokenCount") is not None
        or usage_meta.get("candidatesTokenCount") is not None
    ):
        text_tokens = _to_int(usage_meta.get("promptTokenCount")) or 0
        output_tokens = _to_int(usage_meta.get("candidatesTokenCount")) or 0
        image_tokens = 0
        details = usage_meta.get("candidatesTokensDetails")
        if isinstance(details, list):
            for row in details:
                if isinstance(row, dict) and str(row.get("modality") or "").upper() == "IMAGE":
                    image_tokens += _to_int(row.get("tokenCount")) or 0
        width = height = None
        parts = (((obj.get("candidates") or [{}])[0] or {}).get("content") or {}).get("parts") or []
        n = 0
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if not isinstance(inline, dict) or not inline.get("data"):
                continue
            n += 1
            if width is None:
                width, height = decode_image_dimensions(inline.get("data"))
        return {
            "kind": "image",
            "text_tokens": text_tokens,
            "image_input_tokens": 0,
            "output_tokens": output_tokens or image_tokens,
            "input_tokens": text_tokens,
            "cache_read_tokens": 0,
            "n": n or 1,
            "width": width,
            "height": height,
            "size_tier": resolve_image_size_tier(width, height),
        }

    usage = obj.get("usage")
    if not isinstance(usage, dict) and isinstance(obj.get("response"), dict):
        usage = obj["response"].get("usage")
    if not isinstance(usage, dict):
        # 无 usage 但有图片结果时，仍可按张计费（依赖宽高/请求 size）
        item = _first_image_item(obj)
        if not item:
            return {}
        width = _to_int(item.get("width"))
        height = _to_int(item.get("height"))
        if width is None or height is None:
            width, height = decode_image_dimensions(item.get("b64_json"))
        if width is None and height is None and not item.get("b64_json"):
            return {}
        data = obj.get("data")
        n = len(data) if isinstance(data, list) and data else 1
        return {
            "kind": "image",
            "text_tokens": 0,
            "image_input_tokens": 0,
            "output_tokens": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "n": n,
            "width": width,
            "height": height,
            "size_tier": resolve_image_size_tier(width, height),
            "per_image_only": True,
        }

    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    out_details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    text_tokens = _to_int(details.get("text_tokens"))
    image_tokens = _to_int(details.get("image_tokens"))
    input_tokens = _to_int(usage.get("input_tokens"))
    output_tokens = _to_int(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _to_int(out_details.get("image_tokens"))
    cache_read = _to_int(details.get("cached_tokens")) or _to_int(usage.get("cached_tokens")) or 0

    if text_tokens is None and image_tokens is None and input_tokens is None and output_tokens is None:
        return {}

    if text_tokens is None and image_tokens is None:
        text_tokens = input_tokens or 0
        image_tokens = 0
    elif text_tokens is None:
        text_tokens = max(0, (input_tokens or 0) - (image_tokens or 0))
    elif image_tokens is None:
        image_tokens = max(0, (input_tokens or 0) - (text_tokens or 0))

    n = None
    data = obj.get("data")
    if isinstance(data, list) and data:
        n = len(data)

    item = _first_image_item(obj)
    width = _to_int(item.get("width"))
    height = _to_int(item.get("height"))
    if (width is None or height is None) and item.get("b64_json"):
        width, height = decode_image_dimensions(item.get("b64_json"))

    return {
        "kind": "image",
        "text_tokens": text_tokens or 0,
        "image_input_tokens": image_tokens or 0,
        "output_tokens": output_tokens or 0,
        "input_tokens": (text_tokens or 0) + (image_tokens or 0),
        "cache_read_tokens": cache_read,
        "n": n,
        "width": width,
        "height": height,
        "size_tier": resolve_image_size_tier(width, height),
    }


def calc_image_charge(
    *,
    text_tokens: int = 0,
    image_input_tokens: int = 0,
    output_tokens: int = 0,
    n: int = 1,
    prices: Optional[dict] = None,
    money_rate: float = 1,
    discount: float = 1,
    profit_multiplier: float = PROFIT_MULTIPLIER,
    usd_cny_rate: float = DOR2CHY,
    size_tier: str = "",
    width: Any = None,
    height: Any = None,
    request_size: Any = None,
) -> Optional[dict]:
    prices = prices or {}
    n = max(1, _to_int(n) or 1)
    text_tokens = int(text_tokens or 0)
    image_input_tokens = int(image_input_tokens or 0)
    output_tokens = int(output_tokens or 0)

    tiers = prices.get("per_image_tiers") if isinstance(prices.get("per_image_tiers"), dict) else None
    billing = prices.get("billing") or ("per_image" if tiers else "token")

    if billing == "per_image" and tiers:
        tier = (size_tier or resolve_image_size_tier(width, height, request_size) or "1k").lower()
        if tier not in tiers:
            # 缺档时按更高档兜底，避免漏扣
            for candidate in ("4k", "2k", "1k"):
                if candidate in tiers:
                    tier = candidate
                    break
        unit = float(tiers.get(tier) or 0)
        if unit <= 0 and not (text_tokens or image_input_tokens or output_tokens):
            return None
        usd_cost = n * unit
        cost_cny = usd_cost * usd_cny_rate
        charged = usd_cost * profit_multiplier * usd_cny_rate * (money_rate or 1) * (discount or 1)
        return {
            "kind": "image",
            "billing": "per_image",
            "size_tier": tier,
            "per_image_price": unit,
            "text_tokens": text_tokens,
            "image_input_tokens": image_input_tokens,
            "output_tokens": output_tokens,
            "input_tokens": text_tokens + image_input_tokens,
            "n": n,
            "text_price": float(prices.get("text_input") or 0),
            "image_input_price": float(prices.get("image_input") or 0),
            "output_price": float(prices.get("output") or 0),
            "usd_cost": usd_cost,
            "cost_cny": cost_cny,
            "charged": charged,
            "profit": charged - cost_cny,
            "multiplier": profit_multiplier,
            "usd_cny_rate": usd_cny_rate,
            "cache_read_multiplier": CACHE_READ_MULTIPLIER,
        }

    if not (text_tokens or image_input_tokens or output_tokens):
        return None
    if not any(prices.get(key) is not None for key in ("text_input", "image_input", "output")):
        return None

    text_price = float(prices.get("text_input") or 0)
    image_in_price = float(prices.get("image_input") or 0)
    out_price = float(prices.get("output") or 0)
    usd_cost = (
        text_tokens / 1_000_000 * text_price
        + image_input_tokens / 1_000_000 * image_in_price
        + output_tokens / 1_000_000 * out_price
    )
    cost_cny = usd_cost * usd_cny_rate
    charged = usd_cost * profit_multiplier * usd_cny_rate * (money_rate or 1) * (discount or 1)
    return {
        "kind": "image",
        "billing": "token",
        "text_tokens": text_tokens,
        "image_input_tokens": image_input_tokens,
        "output_tokens": output_tokens,
        "input_tokens": text_tokens + image_input_tokens,
        "n": n,
        "text_price": text_price,
        "image_input_price": image_in_price,
        "output_price": out_price,
        "usd_cost": usd_cost,
        "cost_cny": cost_cny,
        "charged": charged,
        "profit": charged - cost_cny,
        "multiplier": profit_multiplier,
        "usd_cny_rate": usd_cny_rate,
        "cache_read_multiplier": CACHE_READ_MULTIPLIER,
    }


def parse_multipart_fields(body: bytes, content_type: str) -> dict:
    match = re.search(r"boundary=([^;]+)", content_type or "", re.I)
    if not match or not body:
        return {}
    boundary = match.group(1).strip().strip('"')
    raw_parts = body.split(b"--" + boundary.encode("utf-8", errors="ignore"))
    fields = {}
    for part in raw_parts:
        chunk = part.strip()
        if not chunk or chunk == b"--":
            continue
        header_blob, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            header_blob, sep, data = chunk.partition(b"\n\n")
        if not sep:
            continue
        header_text = header_blob.decode(errors="ignore")
        name_match = re.search(r'name="([^"]+)"', header_text, re.I)
        if not name_match or re.search(r"filename=", header_text, re.I):
            continue
        name = name_match.group(1)
        value = data.rstrip(b"\r\n-").decode(errors="ignore").strip()
        if name == "n":
            parsed = _to_int(value)
            fields[name] = parsed if parsed is not None else value
        else:
            fields[name] = value
    return fields


async def compute_image_charge(
    usage: Optional[dict],
    body: dict,
    user_id: str,
    source: str = "images",
    req_id: str = "-",
    use_teamo_prices: bool = False,
    vendor: str = "",
    started: Optional[float] = None,
    first_byte_at: Optional[float] = None,
):
    if not user_id or user_id in {"not exist", "no money"}:
        return None

    body = body or {}
    usage = usage or {}
    model = body.get("model") or usage.get("model") or DEFAULT_IMAGE_MODEL
    prices = lookup_image_price(model, teamo=use_teamo_prices, vendor=vendor)
    if not prices:
        billing_log.warning(
            "skip req=%s user=%s source=%s model=%s reason=unknown_image_price",
            req_id,
            user_id,
            source,
            model or "-",
        )
        return None

    n = _to_int(body.get("n")) or _to_int(usage.get("n")) or 1
    request_size = body.get("size")
    if not request_size:
        cfg = body.get("generationConfig") or body.get("generation_config") or {}
        if isinstance(cfg, dict):
            image_cfg = cfg.get("imageConfig") or cfg.get("image_config") or {}
            if isinstance(image_cfg, dict):
                request_size = image_cfg.get("imageSize") or image_cfg.get("image_size")
    from utils.pricing import get_pricing_config
    from utils.vendor_catalog import vendor_profit_multiplier
    pricing_config = await get_pricing_config()
    profit_multiplier = vendor_profit_multiplier(vendor, pricing_config.profit_multiplier)
    summary = calc_image_charge(
        text_tokens=usage.get("text_tokens") or 0,
        image_input_tokens=usage.get("image_input_tokens") or 0,
        output_tokens=usage.get("output_tokens") or 0,
        n=n,
        prices=prices,
        profit_multiplier=profit_multiplier,
        usd_cny_rate=pricing_config.usd_cny_rate,
        size_tier=usage.get("size_tier") or "",
        width=usage.get("width"),
        height=usage.get("height"),
        request_size=request_size,
    )
    if not summary:
        billing_log.warning(
            "skip req=%s user=%s source=%s model=%s reason=no_image_usage",
            req_id,
            user_id,
            source,
            model or "-",
        )
        return None

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).filter_by(telephone=user_id))
        user = result.scalars().first()
        if not user:
            billing_log.warning(
                "skip req=%s user=%s source=%s model=%s reason=user_missing",
                req_id,
                user_id,
                source,
                model or "-",
            )
            return None
        money_rate = user.money_rate or 1
        discount = user.discount or 1
        if money_rate != 1 or discount != 1:
            summary = calc_image_charge(
                text_tokens=summary["text_tokens"],
                image_input_tokens=summary["image_input_tokens"],
                output_tokens=summary["output_tokens"],
                n=summary["n"],
                prices=prices,
                money_rate=money_rate,
                discount=discount,
                profit_multiplier=summary["multiplier"],
                usd_cny_rate=summary["usd_cny_rate"],
                size_tier=summary.get("size_tier") or usage.get("size_tier") or "",
                width=usage.get("width"),
                height=usage.get("height"),
                request_size=request_size,
            )
            if not summary:
                return None
        summary["model"] = model
        summary["cache_read_tokens"] = int(usage.get("cache_read_tokens") or 0)
        summary["cache_write_tokens"] = int(usage.get("cache_write_tokens") or 0)
        remain_before = user.token or 0
        user.token = remain_before - summary["charged"]
        summary["remain"] = user.token
        user.input_token = (user.input_token or 0) + summary["input_tokens"]
        user.output_token = (user.output_token or 0) + summary["output_tokens"]
        await apply_billed_usage(db, req_id, summary)
        await db.commit()
        if summary["remain"] <= 0:
            invalidate_user_auth(user_id)

    ttfb_ms, done_ms = billing_timing(started, first_byte_at)
    billing_log.info(
        "ok req=%s user=%s source=%s model=%s billing=%s tier=%s text=%s image_in=%s out=%s n=%s spent=%.6f profit=%.6f remain=%.6f usd=%.6f multiplier=%s fx=%s money_rate=%s discount=%s ttfb_ms=%s done_ms=%s",
        req_id,
        user_id,
        source,
        model,
        summary.get("billing") or "token",
        summary.get("size_tier") or "-",
        summary["text_tokens"],
        summary["image_input_tokens"],
        summary["output_tokens"],
        summary["n"],
        summary["charged"],
        summary["profit"],
        summary["remain"],
        summary["usd_cost"],
        summary["multiplier"],
        summary["usd_cny_rate"],
        money_rate,
        discount,
        ttfb_ms,
        done_ms,
    )
    return summary
