import logging
import re
import time
from typing import Optional

from settings.database import AsyncSessionLocal
from settings.macro import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CLAUDE_GPT_KEY,
    DOR2CHY,
    MODEL_MAP_MONEY_DICT,
    TEAMO_MODEL_MONEY_DICT,
    TEAMO_FAST_MODEL_MONEY_DICT,
    TEAMO_MODEL_ALIASES,
    TEAMO_UPSTREAM,
    PROFIT_MULTIPLIER,
    LONG_CONTEXT_RULES,
    SERVICE_TIER_MULTIPLIERS,
)
from utils.vendor_catalog import (
    get_runtime_upstream,
    lookup_vendor_price,
    vendor_cost_usd_cny_rate,
    vendor_profit_multiplier,
)
from utils.api_key_auth import billing_key_for_user, lookup_user_by_credential, resolve_billing_user
from utils.request_usage import apply_billed_usage
from utils.task_guard import finish_writes
from utils.get_log import billing_log, process_log
from utils.user_price import match_user_price

check_user_log = process_log


def billing_timing(started: Optional[float], first_byte_at: Optional[float] = None):
    """扣费日志用的首字耗时和结束耗时，单位毫秒。没有对应时点时记为 -。"""
    if started is None:
        return "-", "-"
    done_ms = max(0, int((time.perf_counter() - started) * 1000))
    if first_byte_at is None:
        return "-", done_ms
    ttfb_ms = max(0, int((first_byte_at - started) * 1000))
    return ttfb_ms, done_ms

_AUTH_OK_TTL = 5.0
_AUTH_NEG_TTL = 2.0
_AUTH_CACHE_MAX = 2048
# raw credential -> (expires, status, billing telephone)
_auth_cache: dict[str, tuple[float, str, str]] = {}
_auth_by_billing: dict[str, set[str]] = {}


def invalidate_user_auth(user_id: str) -> None:
    if not user_id:
        return
    _auth_cache.pop(user_id, None)
    for raw in _auth_by_billing.pop(user_id, ()):
        _auth_cache.pop(raw, None)


def _prune_auth_cache(now: float) -> None:
    if len(_auth_cache) < _AUTH_CACHE_MAX:
        return
    expired = [key for key, item in _auth_cache.items() if item[0] <= now]
    for key in expired:
        _auth_cache.pop(key, None)
    if len(_auth_cache) >= _AUTH_CACHE_MAX:
        _auth_cache.clear()
        _auth_by_billing.clear()


def _cache_auth(raw_key: str, status: str, ttl: float, billing_id: str = "") -> None:
    now = time.monotonic()
    _prune_auth_cache(now)
    _auth_cache[raw_key] = (now + ttl, status, billing_id)
    if status == "ok" and billing_id:
        _auth_by_billing.setdefault(billing_id, set()).add(raw_key)


def extract_user_key(headers: dict) -> str:
    auth = headers.get("authorization") or ""
    match = re.search(r"Bearer\s+(\S+)", auth, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    for key in ("x-api-key", "api-key"):
        value = (headers.get(key) or "").strip()
        if value:
            return value
    return ""


def normalize_model_name(model: str) -> str:
    model = (model or "").strip()
    if not model:
        return ""
    if model.startswith("ft:"):
        parts = model.split(":")
        if len(parts) >= 2 and parts[1]:
            model = parts[1]
    if "/" in model:
        model = model.rsplit("/", 1)[-1]
    if model.startswith("gr-"):
        suffix = model[3:]
        model = suffix if suffix.startswith("grok-") else f"grok-{suffix}"
    # 号商 -hc 后缀不影响价目表匹配
    if model.endswith("-hc"):
        model = model[: -len("-hc")]
    return model


def is_grok_46_family(model: str) -> bool:
    return normalize_model_name(model).startswith("grok-4.6")


_DOT_VERSION = re.compile(r"(\d)\.(\d)")


def _hyphen_version_name(model: str) -> str:
    """claude-haiku-5.5 → claude-haiku-5-5。价目键用连字符，调用方有时写成点号。"""
    return _DOT_VERSION.sub(r"\1-\2", model or "")


def match_price_key(model: str, prices: dict):
    """最长前缀命中价目键。点号版本名对不上时，再试连字符写法。"""
    if not model or not prices:
        return None

    def _hit(name: str):
        if name in prices:
            return name
        for key in sorted(prices, key=len, reverse=True):
            if not name.startswith(key):
                continue
            rest = name[len(key):]
            if not rest or rest.startswith("-"):
                return key
        return None

    found = _hit(model)
    if found:
        return found
    aliased = _hyphen_version_name(model)
    if aliased != model:
        return _hit(aliased)
    return None


def _lookup_static_price(model: str, prices: dict):
    key = match_price_key(model, prices)
    if key is None:
        return None
    return prices[key]


def lookup_model_price(model: str, teamo: bool = False, service_tier: str = "standard", vendor: str = ""):
    """先精确匹配，再按最长前缀匹配带日期后缀的快照名。

    模型日期快照通过最长合法前缀匹配价目表。
    前缀必须顶到模型名边界：gpt-5.4-mini-2026-03-05 可以落到 gpt-5.4-mini，
    但 gpt-5.6-luna 不会误用 gpt-5 的价格。
    vendor 非空时只查该供应商价目，避免按模型改路由后误用全局默认价。
    """
    model = normalize_model_name(model)
    if not model:
        return None
    vendor = (vendor or "").strip().lower()
    if vendor:
        live_price = lookup_vendor_price(model, vendor=vendor, service_tier=service_tier)
        if live_price:
            return live_price
        if vendor == "teamo":
            aliased = TEAMO_MODEL_ALIASES.get(model, model)
            if service_tier in {"fast", "priority"} and aliased in TEAMO_FAST_MODEL_MONEY_DICT:
                return TEAMO_FAST_MODEL_MONEY_DICT[aliased]
            return TEAMO_MODEL_MONEY_DICT.get(aliased)
        if vendor not in {"teamo", "yunxin"}:
            return _lookup_static_price(model, MODEL_MAP_MONEY_DICT)
        return None
    if teamo:
        model = TEAMO_MODEL_ALIASES.get(model, model)
        # 当前活跃供应商价目表优先；fast 档有独立行则用独立行。
        live_price = lookup_vendor_price(model, vendor="teamo", service_tier=service_tier)
        if live_price:
            return live_price
        if service_tier in {"fast", "priority"} and model in TEAMO_FAST_MODEL_MONEY_DICT:
            return TEAMO_FAST_MODEL_MONEY_DICT[model]
        return TEAMO_MODEL_MONEY_DICT.get(model)
    live_price = lookup_vendor_price(model, service_tier=service_tier)
    if live_price:
        return live_price
    return _lookup_static_price(model, MODEL_MAP_MONEY_DICT)


def apply_upstream_auth(headers: dict, *, api_key: str = "", teamo: Optional[bool] = None) -> dict:
    """把用户自己的 key 换成转发到号商上游的密钥。

    api_key 有值时用该供应商自己的 key；否则回落到全局 CLAUDE_GPT_KEY。
    """
    key = (api_key or "").strip() or CLAUDE_GPT_KEY
    headers["authorization"] = f"Bearer {key}"
    use_teamo = get_runtime_upstream().get("teamo") if teamo is None else teamo
    vendor_key = bool((api_key or "").strip())
    if use_teamo or vendor_key:
        headers["x-api-key"] = key
    elif "x-api-key" in headers:
        headers["x-api-key"] = key
    if "api-key" in headers or vendor_key:
        headers["api-key"] = key
    return headers


async def check_user_message(headers: dict):
    raw_key = extract_user_key(headers)
    if not raw_key:
        check_user_log.info("user key extracted: <empty>")
        return None, "not exist"

    now = time.monotonic()
    cached = _auth_cache.get(raw_key)
    if cached and cached[0] > now:
        status = cached[1]
        if status == "ok":
            return apply_upstream_auth(headers), cached[2] or raw_key
        return None, status

    check_user_log.info("user key extracted: %s", raw_key[:7] + "…" if raw_key.startswith("sk-") else raw_key)
    async with AsyncSessionLocal() as db:
        user = await lookup_user_by_credential(db, raw_key)

    if not user:
        _cache_auth(raw_key, "not exist", _AUTH_NEG_TTL)
        return None, "not exist"
    telephone = billing_key_for_user(user)
    if not telephone:
        _cache_auth(raw_key, "not exist", _AUTH_NEG_TTL)
        return None, "not exist"
    if user.token is None or user.token <= 0:
        _cache_auth(raw_key, "no money", _AUTH_NEG_TTL, telephone)
        return None, "no money"

    _cache_auth(raw_key, "ok", _AUTH_OK_TTL, telephone)
    return apply_upstream_auth(headers), telephone


def resolve_official_prices(raw: dict) -> dict:
    """官方价以价目表为准。没写 cache_read 才用 CACHE_READ_MULTIPLIER 兜底；没写 cache_write 不收写入费。"""
    input_price = float(raw.get("input") or 0)
    output_price = raw.get("output")
    output_price = 0.0 if output_price is None else float(output_price)
    cache_read = raw.get("cache_read")
    cache_write = raw.get("cache_write")
    if cache_read is None:
        cache_read = input_price * CACHE_READ_MULTIPLIER
    if cache_write is None:
        cache_write = input_price * CACHE_WRITE_MULTIPLIER
    return {
        "input": input_price,
        "output": output_price,
        "cache_read": float(cache_read or 0),
        "cache_write": float(cache_write or 0),
        "audio_input": float(raw.get("audio_input") or 0),
        "audio_output": float(raw.get("audio_output") or 0),
    }


def _positive_price(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def has_original_prices(raw: Optional[dict]) -> bool:
    """模型是否配置了原价（任一正数即生效）。"""
    if not isinstance(raw, dict):
        return False
    return any(
        _positive_price(raw.get(key)) is not None
        for key in (
            "original_input",
            "original_output",
            "original_cache_read",
            "original_cache_write",
            "original_text_input",
            "original_image_input",
        )
    )


def resolve_billing_and_cost_prices(raw: dict) -> tuple[dict, dict]:
    """返回 (计费美元价, 成本美元价)。

    设置了原价时：1:1 按原价计美元，人民币扣费只乘汇率（不加价、不叠用户倍率/折扣）；
    成本价仍用价目表 input/output，用于 cost_cny 与利润。
    未设置原价时两者相同（沿用成本价 × 加价 × 费率）。
    """
    raw = raw or {}
    cost = resolve_official_prices(raw)
    if not has_original_prices(raw):
        return cost, cost
    orig_input = _positive_price(raw.get("original_input"))
    orig_output = _positive_price(raw.get("original_output"))
    orig_cache_read = _positive_price(raw.get("original_cache_read"))
    orig_cache_write = _positive_price(raw.get("original_cache_write"))
    billing_raw = {
        "input": orig_input if orig_input is not None else cost["input"],
        "output": orig_output if orig_output is not None else cost["output"],
        "audio_input": raw.get("audio_input"),
        "audio_output": raw.get("audio_output"),
    }
    scale = None
    if orig_input is not None and cost["input"] > 0:
        scale = orig_input / cost["input"]

    def _pick_cache(explicit, cost_value: float):
        if explicit is not None:
            return explicit
        if scale is not None:
            return float(cost_value or 0) * scale
        if orig_input is not None:
            return None  # 交给 resolve_official_prices 按原价 input 推导
        return cost_value

    billing_raw["cache_read"] = _pick_cache(orig_cache_read, cost["cache_read"])
    billing_raw["cache_write"] = _pick_cache(orig_cache_write, cost["cache_write"])
    return resolve_official_prices(billing_raw), cost


def calc_token_usd_cost(
    input_tokens: int,
    output_tokens: int,
    input_price: float,
    output_price: float,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_price: float = 0,
    cache_write_price: float = 0,
    audio_input_tokens: int = 0,
    audio_output_tokens: int = 0,
    audio_input_price: float = 0,
    audio_output_price: float = 0,
) -> float:
    return (
        input_tokens / 1_000_000 * input_price
        + output_tokens / 1_000_000 * output_price
        + cache_read_tokens / 1_000_000 * cache_read_price
        + cache_write_tokens / 1_000_000 * cache_write_price
        + audio_input_tokens / 1_000_000 * audio_input_price
        + audio_output_tokens / 1_000_000 * audio_output_price
    )


def resolve_service_tier(body: dict) -> str:
    """返回请求实际使用的计费模式。

    客户没传 service_tier 时：Grok 4.6 默认走 xAI Fast（priority，2x），
    其它模型仍按 Standard，避免少收 Fast 费用。
    """
    body = body or {}
    tier = body.get("service_tier") or body.get("processing_mode")
    if tier not in (None, ""):
        tier = str(tier).strip().lower()
        if tier in SERVICE_TIER_MULTIPLIERS:
            return tier
    if is_grok_46_family((body or {}).get("model") or ""):
        return "priority"
    return "standard"


def apply_service_tier_prices(prices: dict, service_tier: str) -> dict:
    """将 Standard 官方价换算为请求模式价格。"""
    tier = service_tier if service_tier in SERVICE_TIER_MULTIPLIERS else "standard"
    multiplier = SERVICE_TIER_MULTIPLIERS[tier]
    return {
        key: value * multiplier if isinstance(value, (int, float)) else value
        for key, value in prices.items()
    }


def resolve_reasoning_effort(body: dict) -> str:
    """读取 Responses/Chat Completions 常见的推理强度字段。

    Grok 4.6 官方默认就是 high；客户只选 gr-4.6 没带 effort 时按 high 记录。
    """
    body = body or {}
    reasoning = body.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    effort = effort or body.get("reasoning_effort")
    if effort not in (None, ""):
        return str(effort).strip().lower()
    if is_grok_46_family(body.get("model") or ""):
        return "high"
    return "default"


def prompt_token_count(usage: dict) -> int:
    """长上下文阈值用的 prompt 长度：未缓存输入 + 缓存读取 + 缓存写入。"""
    usage = usage or {}
    return sum(
        max(0, int(usage.get(key) or 0))
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )


def _match_long_context_name(name: str):
    best = None
    best_len = -1
    for prefix, spec in LONG_CONTEXT_RULES:
        prefix = prefix.lower()
        if not (name == prefix or name.startswith(prefix + "-") or name.startswith(prefix + ".")):
            continue
        if len(prefix) > best_len:
            best = spec
            best_len = len(prefix)
    return best, best_len


def match_long_context_rule(model: str):
    """最长前缀命中官方长短上下文规则。明确排除的前缀返回 None。"""
    name = normalize_model_name(model).lower()
    if not name:
        return None
    best, best_len = _match_long_context_name(name)
    if best_len >= 0:
        return best
    aliased = _hyphen_version_name(name)
    if aliased != name:
        best, best_len = _match_long_context_name(aliased)
        if best_len >= 0:
            return best
    return None


def _rule_allows_tier(spec, service_tier: str) -> bool:
    tiers = spec[4]
    if not tiers:
        return True
    return (service_tier or "standard").strip().lower() in tiers


def _scale_long_context_prices(prices: dict, input_mul: float, output_mul: float) -> dict:
    scaled = {}
    for key, value in prices.items():
        if not isinstance(value, (int, float)):
            scaled[key] = value
        elif key == "output":
            scaled[key] = value * output_mul
        else:
            scaled[key] = value * input_mul
    return scaled


def resolve_context_billing(model: str, prices: dict, usage: dict, service_tier: str = "standard") -> tuple:
    """按实际 prompt token 决定本笔用短上下文还是长上下文单价。

    返回 (prices, context_tier)。context_tier 为 short / long；该模型没有差价时为 None。
    没公布长上下文价的处理模式（例如 GPT-5.5 Fast）保持短上下文单价。
    """
    spec = match_long_context_rule(model)
    if not spec:
        return prices, None
    threshold, op, input_mul, output_mul, _tiers = spec
    prompt_tokens = prompt_token_count(usage)
    crossed = prompt_tokens >= threshold if op == "ge" else prompt_tokens > threshold
    if not crossed or not _rule_allows_tier(spec, service_tier):
        return prices, "short"
    return _scale_long_context_prices(prices, input_mul, output_mul), "long"


def long_context_quote(model: str, prices: dict, service_tier: str = "standard"):
    """给价目行附上长上下文单价。价格已是当前倍率/利润后的短上下文价，这里只乘官方倍数。"""
    spec = match_long_context_rule(model)
    if not spec or not _rule_allows_tier(spec, service_tier):
        return None
    threshold, op, input_mul, output_mul, _tiers = spec

    def scaled(key, factor):
        return float(prices.get(key) or 0) * factor

    return {
        "threshold": int(threshold),
        "op": op,
        "input_multiplier": input_mul,
        "output_multiplier": output_mul,
        "input": scaled("input", input_mul),
        "output": scaled("output", output_mul),
        "cache_read": scaled("cache_read", input_mul),
        "cache_write": scaled("cache_write", input_mul),
    }


def apply_long_context_prices(model: str, prices: dict, usage: dict, service_tier: str = "standard") -> tuple:
    """按官方规则切换长上下文价格。返回 (prices, 是否长上下文)。"""
    scaled, context_tier = resolve_context_billing(
        model, prices, usage, service_tier=service_tier,
    )
    return scaled, context_tier == "long"


def calc_charge(
    input_tokens: int,
    output_tokens: int,
    input_price: float,
    output_price: float,
    money_rate: float = 1,
    discount: float = 1,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_price: float = 0,
    cache_write_price: float = 0,
    audio_input_tokens: int = 0,
    audio_output_tokens: int = 0,
    audio_input_price: float = 0,
    audio_output_price: float = 0,
    profit_multiplier: float = PROFIT_MULTIPLIER,
    usd_cny_rate: float = DOR2CHY,
    cost_usd_cny_rate: Optional[float] = None,
) -> dict:
    usd_cost = calc_token_usd_cost(
        input_tokens,
        output_tokens,
        input_price,
        output_price,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_read_price=cache_read_price,
        cache_write_price=cache_write_price,
        audio_input_tokens=audio_input_tokens,
        audio_output_tokens=audio_output_tokens,
        audio_input_price=audio_input_price,
        audio_output_price=audio_output_price,
    )
    cost_rate = float(cost_usd_cny_rate or usd_cny_rate)
    cost_cny = usd_cost * cost_rate
    charged = usd_cost * profit_multiplier * usd_cny_rate * (money_rate or 1) * (discount or 1)
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_write_tokens": int(cache_write_tokens or 0),
        "audio_input_tokens": int(audio_input_tokens or 0),
        "audio_output_tokens": int(audio_output_tokens or 0),
        "input_price": float(input_price or 0),
        "output_price": float(output_price or 0),
        "audio_input_price": float(audio_input_price or 0),
        "audio_output_price": float(audio_output_price or 0),
        "cache_read_price": float(cache_read_price or 0),
        "cache_write_price": float(cache_write_price or 0),
        "usd_cost": usd_cost,
        "cost_cny": cost_cny,
        "charged": charged,
        "profit": charged - cost_cny,
        "multiplier": profit_multiplier,
        "usd_cny_rate": usd_cny_rate,
        "cost_usd_cny_rate": cost_rate,
        "cache_read_multiplier": CACHE_READ_MULTIPLIER,
        "cache_write_multiplier": CACHE_WRITE_MULTIPLIER,
    }


async def compute_user_token(
    token_dict: dict,
    body: dict,
    user_id: str,
    source="openai",
    req_id: str = "-",
    use_teamo_prices: Optional[bool] = None,
    vendor: str = "",
    started: Optional[float] = None,
    first_byte_at: Optional[float] = None,
):
    if not user_id or user_id in {"not exist", "no money"}:
        return None
    if not token_dict or ("input_tokens" not in token_dict and "audio_input_tokens" not in token_dict) or ("output_tokens" not in token_dict and "audio_output_tokens" not in token_dict):
        return None

    model = (body or {}).get("model") or ""
    service_tier = resolve_service_tier(body)
    vendor = (vendor or "").strip().lower()
    if vendor:
        use_teamo_prices = vendor == "teamo"
    else:
        use_teamo_prices = get_runtime_upstream().get("teamo") if use_teamo_prices is None else use_teamo_prices
    model_price_raw = lookup_model_price(
        model, teamo=use_teamo_prices, service_tier=service_tier, vendor=vendor,
    )
    if not model_price_raw:
        billing_log.warning(
            "skip req=%s user=%s source=%s model=%s reason=unknown_price in=%s out=%s",
            req_id,
            user_id,
            source,
            model or "-",
            token_dict.get("input_tokens"),
            token_dict.get("output_tokens"),
        )
        return None

    is_embedding = normalize_model_name(model).startswith("text-embedding-")
    is_gpt_audio = normalize_model_name(model).startswith("gpt-audio")
    reasoning_effort = resolve_reasoning_effort(body)
    billing_prices, cost_prices = resolve_billing_and_cost_prices(model_price_raw)
    use_original = has_original_prices(model_price_raw)
    # gpt-audio modality tokens are charged separately; remaining text tokens
    # continue using the ordinary input/output rates from the model price row.
    # 长短上下文按本笔 prompt token 切换，倍数作用在已含供应商倍率的短上下文价上。
    billing_prices, context_tier = resolve_context_billing(
        model, billing_prices, token_dict, service_tier=service_tier,
    )
    cost_prices, _ = resolve_context_billing(
        model, cost_prices, token_dict, service_tier=service_tier,
    )
    long_context = context_tier == "long"
    # 官网单独列出的 Fast 档优先使用其真实报价；其他 GPT Fast 档按文档的 2x。
    if use_teamo_prices:
        if service_tier in {"fast", "priority"} and normalize_model_name(model) not in TEAMO_FAST_MODEL_MONEY_DICT:
            billing_prices = apply_service_tier_prices(billing_prices, service_tier)
            cost_prices = apply_service_tier_prices(cost_prices, service_tier)
    else:
        billing_prices = apply_service_tier_prices(billing_prices, service_tier)
        cost_prices = apply_service_tier_prices(cost_prices, service_tier)
        prices = billing_prices
        cache_read_tokens = int(token_dict.get("cache_read_tokens") or 0)
    cache_write_tokens = int(token_dict.get("cache_write_tokens") or 0)
    if use_teamo_prices:
        prices = billing_prices
        cache_read_tokens = int(token_dict.get("cache_read_tokens") or 0)
    # 用户专属价：录入的 USD 价即应付价，人民币只乘汇率，不叠倍率/折扣。
    user_price = match_user_price(model, user_key=user_id, service_tier=service_tier)
    if user_price:
        prices = resolve_official_prices({
            "input": user_price["input"],
            "output": user_price["output"],
            "cache_read": user_price["cache_read"],
            "cache_write": user_price["cache_write"],
        })
        prices, context_tier = resolve_context_billing(
            model, prices, token_dict, service_tier=service_tier,
        )
        long_context = context_tier == "long"

    summary = None
    charge_money_rate = 1
    charge_discount = 1

    async def write():
        nonlocal summary, charge_money_rate, charge_discount
        async with AsyncSessionLocal() as db:
            user = await resolve_billing_user(db, user_id)
            if not user:
                billing_log.warning(
                    "skip req=%s user=%s source=%s model=%s reason=user_missing",
                    req_id,
                    user_id,
                    source,
                    model or "-",
                )
                return False
            money_rate = user.money_rate or 1
            discount = user.discount or 1
            from utils.pricing import get_pricing_config
            pricing_config = await get_pricing_config(db)
            profit_multiplier = vendor_profit_multiplier(vendor, pricing_config.profit_multiplier)
            cost_rate = vendor_cost_usd_cny_rate(vendor, pricing_config.usd_cny_rate)
            # 原价 / 用户专属价：1:1 美元计价，人民币只乘汇率；普通模式仍叠加价与用户费率
            flat_price = use_original or bool(user_price)
            charge_multiplier = 1.0 if flat_price else profit_multiplier
            charge_money_rate = 1.0 if flat_price else money_rate
            charge_discount = 1.0 if flat_price else discount
            summary = calc_charge(
                token_dict["input_tokens"],
                token_dict["output_tokens"],
                prices["input"],
                prices["output"],
                money_rate=charge_money_rate,
                discount=charge_discount,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cache_read_price=prices["cache_read"],
                cache_write_price=prices["cache_write"],
                audio_input_tokens=int(token_dict.get("audio_input_tokens") or 0),
                audio_output_tokens=int(token_dict.get("audio_output_tokens") or 0),
                audio_input_price=prices.get("audio_input", 0.0),
                audio_output_price=prices.get("audio_output", 0.0),
                profit_multiplier=charge_multiplier,
                usd_cny_rate=pricing_config.usd_cny_rate,
                cost_usd_cny_rate=cost_rate,
            )
            if flat_price:
                # 成本/利润仍按上游成本价 × 汇率（与对用户扣费无关）
                actual_cost_usd = calc_token_usd_cost(
                    summary["input_tokens"],
                    summary["output_tokens"],
                    cost_prices["input"],
                    cost_prices["output"],
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                    cache_read_price=cost_prices["cache_read"],
                    cache_write_price=cost_prices["cache_write"],
                    audio_input_tokens=summary["audio_input_tokens"],
                    audio_output_tokens=summary["audio_output_tokens"],
                    audio_input_price=cost_prices.get("audio_input", 0.0),
                    audio_output_price=cost_prices.get("audio_output", 0.0),
                )
                summary["cost_cny"] = actual_cost_usd * (cost_rate or pricing_config.usd_cny_rate)
                summary["profit"] = summary["charged"] - summary["cost_cny"]
                summary["billing_basis"] = "user_price" if user_price else "original"
                summary["multiplier"] = 1.0
            else:
                summary["billing_basis"] = "cost"
            summary["model"] = model
            if is_embedding:
                summary["kind"] = "embedding"
            if is_gpt_audio:
                summary["kind"] = "audio"
            summary["service_tier"] = service_tier
            summary["service_tier_multiplier"] = (
                1.0 if use_teamo_prices and normalize_model_name(model) in TEAMO_FAST_MODEL_MONEY_DICT
                and service_tier in {"fast", "priority"}
                else SERVICE_TIER_MULTIPLIERS[service_tier]
            )
            summary["reasoning_effort"] = reasoning_effort
            summary["long_context"] = long_context
            summary["context_tier"] = context_tier
            summary["audio_input_tokens"] = int(token_dict.get("audio_input_tokens") or 0)
            summary["audio_output_tokens"] = int(token_dict.get("audio_output_tokens") or 0)
            remain_before = user.token or 0
            user.token = remain_before - summary["charged"]
            summary["remain"] = user.token
            user.input_token = (user.input_token or 0) + (
                token_dict.get("input_tokens", 0) + cache_read_tokens + cache_write_tokens
                + int(token_dict.get("audio_input_tokens") or 0)
            )
            user.output_token = (user.output_token or 0) + (
                token_dict.get("output_tokens", 0) + int(token_dict.get("audio_output_tokens") or 0)
            )
            await apply_billed_usage(db, req_id, summary)
            await db.commit()
            if summary["remain"] <= 0:
                invalidate_user_auth(user_id)
            # 日志放在写库任务内：客户端断开后调用方会抛取消，放在外面会跳过这行，
            # 数据库已扣费但 billing.log 查不到记录。
            _log_billed(req_id, user_id, source, model, context_tier, summary,
                        charge_money_rate, charge_discount, started, first_byte_at)
            return True

    if not await finish_writes(write):
        return None

    return summary


def _log_billed(req_id, user_id, source, model, context_tier, summary,
                charge_money_rate, charge_discount, started, first_byte_at):
    ttfb_ms, done_ms = billing_timing(started, first_byte_at)
    billing_log.info(
        "ok req=%s user=%s source=%s model=%s context=%s in=%s cache_read=%s cache_write=%s out=%s spent=%.6f cost=%.6f profit=%.6f remain=%.6f usd=%.6f multiplier=%s cache_read_x=%s cache_write_x=%s fx=%s cost_fx=%s money_rate=%s discount=%s ttfb_ms=%s done_ms=%s",
        req_id,
        user_id,
        source,
        model,
        context_tier or "-",
        summary["input_tokens"],
        summary["cache_read_tokens"],
        summary["cache_write_tokens"],
        summary["output_tokens"],
        summary["charged"],
        summary.get("cost_cny") or 0.0,
        summary["profit"],
        summary["remain"],
        summary["usd_cost"],
        summary["multiplier"],
        CACHE_READ_MULTIPLIER,
        CACHE_WRITE_MULTIPLIER,
        summary["usd_cny_rate"],
        summary.get("cost_usd_cny_rate") or summary["usd_cny_rate"],
        charge_money_rate,
        charge_discount,
        ttfb_ms,
        done_ms,
    )
