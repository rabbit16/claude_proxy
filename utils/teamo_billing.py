"""TeamoRouter 按请求账单扣费。

模型调用返回 request_id 后，查 GET /v1/billing/requests/{request_id}，
用对方的 amount（USD）再乘 profit_multiplier × money_rate × discount。
只用于 teamo，其它供应商仍按 token 价目。
文档：https://teamorouter.cn/zh/docs/open-api
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from settings.database import AsyncSessionLocal
from settings.macro import DOR2CHY, TEAMO_HTTP_PROXY, TEAMO_UPSTREAM_BASE, _load_teamo_api_key
from utils.api_key_auth import resolve_billing_user
from utils.check_user import billing_timing, invalidate_user_auth
from utils.get_log import billing_audit_log, billing_log, process_log
from utils.request_usage import apply_billed_usage
from utils.task_guard import finish_writes

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HEADER_KEYS = (
    "x-request-id",
    "x-teamo-request-id",
    "x-teamo-requestid",
    "request-id",
)
_RETRY_STATUSES = {404, 409, 425, 429, 500, 502, 503, 504}
_RETRY_SLEEPS = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def is_teamo_request_id(value: Any) -> bool:
    text = _norm_id(value)
    if not text:
        return False
    if _UUID_RE.match(text) or _HEX32_RE.match(text):
        return True
    lower = text.lower()
    return lower.startswith(("req_", "teamo_", "tr_"))


def _request_id_score(value: Any, *, from_header: bool = False, field: str = "") -> int:
    text = _norm_id(value)
    if not text:
        return 0
    score = 0
    if from_header:
        score += 100
    if field == "request_id":
        score += 50
    if _UUID_RE.match(text):
        version = text[14].lower()
        if version == "4":
            score += 40
        elif version in "1235":
            score += 30
        else:
            score += 8
    elif text.lower().startswith(("req_", "teamo_", "tr_")):
        score += 25
    elif _HEX32_RE.match(text):
        score += 20
    else:
        score += 1
    return score


def better_teamo_request_id(current: str, candidate: str) -> str:
    if _request_id_score(candidate) > _request_id_score(current):
        return candidate
    return current or candidate


def _prefer_request_id(current: str, candidate: str, **kwargs) -> str:
    if not candidate:
        return current
    if not current:
        return candidate
    if _request_id_score(candidate, **kwargs) > _request_id_score(current):
        return candidate
    return current


def _ids_from_payload(payload: Any) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    found = []
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    for field, value in (
        ("request_id", payload.get("request_id")),
        ("request_id", message.get("request_id")),
        ("request_id", response.get("request_id")),
        ("id", payload.get("id")),
        ("id", message.get("id")),
        ("id", response.get("id")),
    ):
        text = _norm_id(value)
        if text:
            found.append((field, text))
    return found


def extract_teamo_request_id(payload: Any = None, headers: Optional[dict] = None) -> str:
    """从上游响应头/JSON 取出 Teamo 账单用的 request_id。优先响应头和 UUID v4。"""
    best = ""
    for key, value in (headers or {}).items():
        if str(key).lower() not in _HEADER_KEYS:
            continue
        text = _norm_id(value).split(",")[0].strip()
        if text:
            best = _prefer_request_id(best, text, from_header=True, field="request_id")
    for field, text in _ids_from_payload(payload):
        best = _prefer_request_id(best, text, field=field)
    return best


def teamo_billing_bases(vendor_base_url: str = "") -> list[str]:
    bases = []
    for raw in (
        vendor_base_url,
        TEAMO_UPSTREAM_BASE,
        "https://api.teamorouter.cn",
        "https://teamorouter.cn",
    ):
        base = (raw or "").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        if base and base not in bases:
            bases.append(base)
    return bases


def parse_teamo_bill(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    amount = payload.get("amount") if isinstance(payload.get("amount"), dict) else {}
    raw_value = amount.get("value")
    try:
        usd_cost = float(raw_value)
    except (TypeError, ValueError):
        return None
    if usd_cost < 0:
        return None
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    from utils.common_utils import extract_usage
    parsed_usage = extract_usage({
        "object": "chat.completion",
        "usage": {"input_tokens": 0, "output_tokens": 0, **usage},
    })
    # 官方账单的 input_tokens 口径随模型而异：
    #   Anthropic：input_tokens 不含 cache，且 total = input + cache_read + cache_write + output
    #   OpenAI/DeepSeek：input_tokens 已含 cache_read，total = input + output
    # 用 total_tokens 判断是否已含，避免把 Anthropic 的输入再扣一次。
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read = int(parsed_usage.get("cache_read_tokens") or 0)
    cache_write = int(parsed_usage.get("cache_write_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = usage.get("total_tokens")
    try:
        total = int(total_tokens) if total_tokens is not None else None
    except (TypeError, ValueError):
        total = None
    if total is not None and cache_read > 0:
        # total 把 cache 单列时，input 本身就不含缓存。
        includes_cache = total < input_tokens + cache_read
        if includes_cache:
            input_tokens = max(0, input_tokens - cache_read)
    return {
        "request_id": _norm_id(payload.get("request_id")),
        "model": _norm_id(payload.get("model")),
        "usd_cost": usd_cost,
        "currency": _norm_id(amount.get("currency") or "USD") or "USD",
        "input_tokens": input_tokens,
        "output_tokens": int(parsed_usage.get("output_tokens") or output_tokens or 0),
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "raw": payload,
    }


def calc_teamo_pass_through(
    usd_cost: float,
    *,
    profit_multiplier: float,
    usd_cny_rate: float,
    money_rate: float = 1,
    discount: float = 1,
    cost_usd_cny_rate: Optional[float] = None,
) -> dict:
    usd_cost = float(usd_cost or 0)
    profit_multiplier = float(profit_multiplier or 0)
    usd_cny_rate = float(usd_cny_rate or DOR2CHY)
    cost_rate = float(cost_usd_cny_rate or usd_cny_rate)
    money_rate = float(money_rate or 1)
    discount = float(discount or 1)
    cost_cny = usd_cost * cost_rate
    charged = usd_cost * profit_multiplier * usd_cny_rate * money_rate * discount
    return {
        "usd_cost": usd_cost,
        "cost_cny": cost_cny,
        "charged": charged,
        "profit": charged - cost_cny,
        "multiplier": profit_multiplier,
        "usd_cny_rate": usd_cny_rate,
        "cost_usd_cny_rate": cost_rate,
        "billing_source": "teamo_request",
    }


async def fetch_teamo_request_bill(
    request_id: str,
    *,
    client,
    vendor_base_url: str = "",
) -> Optional[dict]:
    request_id = _norm_id(request_id)
    if not request_id:
        return None
    api_key = _teamo_request_api_key()
    if not api_key:
        billing_log.warning("teamo bill skipped request_id=%s reason=missing_api_key", request_id)
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    last_error = ""
    for base in teamo_billing_bases(vendor_base_url):
        url = f"{base}/v1/billing/requests/{quote(request_id, safe='')}"
        for attempt, sleep_for in enumerate((0.0,) + _RETRY_SLEEPS, start=1):
            if sleep_for:
                await asyncio.sleep(sleep_for)
            try:
                resp = await client.get(url, headers=headers, timeout=15.0)
            except Exception as exc:
                last_error = repr(exc)
                process_log.warning(
                    "teamo bill request failed request_id=%s base=%s attempt=%s err=%s",
                    request_id, base, attempt, exc,
                )
                continue
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except Exception:
                    last_error = "invalid_json"
                    continue
                parsed = parse_teamo_bill(payload)
                if parsed:
                    parsed["billing_url"] = url
                    return parsed
                last_error = "unusable_payload"
                continue
            last_error = f"http_{resp.status_code}"
            if resp.status_code in _RETRY_STATUSES and attempt < 1 + len(_RETRY_SLEEPS):
                continue
            if resp.status_code in {401, 403}:
                billing_log.warning(
                    "teamo bill auth failed request_id=%s status=%s",
                    request_id, resp.status_code,
                )
                return None
            break
    billing_log.warning(
        "teamo bill unavailable request_id=%s last=%s",
        request_id, last_error or "-",
    )
    return None


async def charge_from_teamo_bill(
    bill: dict,
    *,
    user_id: str,
    body: Optional[dict],
    usage: Optional[dict],
    req_id: str,
    source: str,
    kind: str = "chat",
    vendor: str = "",
    client=None,
    vendor_base_url: str = "",
    started: Optional[float] = None,
    first_byte_at: Optional[float] = None,
) -> Optional[dict]:
    if not user_id or user_id in {"not exist", "no money"}:
        return None
    if not bill:
        return None
    body = body or {}
    usage = usage or {}
    summary = None
    money_rate = 1
    discount = 1

    async def write():
        nonlocal summary, money_rate, discount
        async with AsyncSessionLocal() as db:
            user = await resolve_billing_user(db, user_id)
            if not user:
                billing_log.warning(
                    "skip req=%s user=%s source=%s reason=user_missing teamo_request=%s",
                    req_id, user_id, source, bill.get("request_id") or "-",
                )
                return False
            from utils.pricing import get_pricing_config
            from utils.vendor_catalog import vendor_profit_multiplier, vendor_cost_usd_cny_rate
            from utils.user_price import match_user_price
            pricing_config = await get_pricing_config(db)
            money_rate = user.money_rate or 1
            discount = user.discount or 1
            cost_rate = vendor_cost_usd_cny_rate(vendor, pricing_config.usd_cny_rate)
            summary = calc_teamo_pass_through(
                bill["usd_cost"],
                profit_multiplier=vendor_profit_multiplier(vendor, pricing_config.profit_multiplier),
                usd_cny_rate=pricing_config.usd_cny_rate,
                money_rate=money_rate,
                discount=discount,
                cost_usd_cny_rate=cost_rate,
            )
            # 用户专属价：官方账单 USD 价即应付价，人民币只乘汇率，不叠倍率/折扣。
            model_name = body.get("model") or bill.get("model") or ""
            user_price = match_user_price(model_name, user_key=user_id)
            if user_price:
                summary = calc_teamo_pass_through(
                    bill["usd_cost"],
                    profit_multiplier=1.0,
                    usd_cny_rate=pricing_config.usd_cny_rate,
                    money_rate=1.0,
                    discount=1.0,
                    cost_usd_cny_rate=cost_rate,
                )
                summary["billing_basis"] = "user_price"
            input_tokens = int(bill.get("input_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(bill.get("output_tokens") or usage.get("output_tokens") or 0)
            cache_read = int(bill.get("cache_read_tokens") or usage.get("cache_read_tokens") or 0)
            cache_write = int(bill.get("cache_write_tokens") or usage.get("cache_write_tokens") or 0)
            summary.update({
                "model": body.get("model") or bill.get("model") or "",
                "kind": kind,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "audio_input_tokens": int(usage.get("audio_input_tokens") or 0),
                "audio_output_tokens": int(usage.get("audio_output_tokens") or 0),
                "teamo_request_id": bill.get("request_id") or "",
                "teamo_amount": bill.get("usd_cost"),
                "teamo_currency": bill.get("currency") or "USD",
            })
            remain_before = user.token or 0
            user.token = remain_before - summary["charged"]
            summary["remain"] = user.token
            user.input_token = (user.input_token or 0) + input_tokens + cache_read + cache_write
            user.output_token = (user.output_token or 0) + output_tokens
            await apply_billed_usage(db, req_id, summary)
            await db.commit()
            if summary["remain"] <= 0:
                invalidate_user_auth(user_id)
            # 日志放进写库任务内：调用方在客户端断开时会抛取消，
            # 放在写库后面会跳过这行，出现已扣费但 billing.log 无记录。
            _log_billed(req_id, user_id, source, summary, money_rate, discount,
                        started, first_byte_at)
            return True

    if not await finish_writes(write):
        return None

    try:
        audit_teamo_charge(
            req_id=req_id,
            user_id=user_id,
            source=source,
            kind=kind,
            model=summary.get("model") or "",
            bill=bill,
            usage=usage,
            body=body,
            vendor=vendor,
        )
    except Exception:
        process_log.exception("teamo billing audit failed req=%s", req_id)
    try:
        await verify_teamo_official_after_charge(
            bill,
            req_id=req_id,
            user_id=user_id,
            source=source,
            model=summary.get("model") or "",
            client=client,
            vendor_base_url=vendor_base_url,
        )
    except Exception:
        process_log.exception("teamo official reconcile failed req=%s", req_id)
    return summary


def _log_billed(req_id, user_id, source, summary, money_rate, discount,
                started, first_byte_at):
    ttfb_ms, done_ms = billing_timing(started, first_byte_at)
    billing_log.info(
        "ok req=%s user=%s source=%s model=%s in=%s cache_read=%s cache_write=%s out=%s teamo_request=%s usd=%.6f cost=%.6f spent=%.6f profit=%.6f remain=%.6f multiplier=%s fx=%s cost_fx=%s money_rate=%s discount=%s ttfb_ms=%s done_ms=%s",
        req_id,
        user_id,
        source,
        summary.get("model") or "-",
        summary.get("input_tokens") or 0,
        summary.get("cache_read_tokens") or 0,
        summary.get("cache_write_tokens") or 0,
        summary.get("output_tokens") or 0,
        summary.get("teamo_request_id") or "-",
        summary["usd_cost"],
        summary.get("cost_cny") or 0.0,
        summary["charged"],
        summary["profit"],
        summary["remain"],
        summary["multiplier"],
        summary["usd_cny_rate"],
        summary.get("cost_usd_cny_rate") or summary["usd_cny_rate"],
        money_rate,
        discount,
        ttfb_ms,
        done_ms,
    )


_BALANCE_TTL = 45.0
_OVERCHARGE_RATIO = 2.0
_UNDERCHARGE_RATIO = 0.4
_HIGH_AMOUNT_USD = 8.0
_balance_cache: dict = {}
_balance_lock = asyncio.Lock()


def parse_teamo_balance(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    balance = payload.get("balance") if isinstance(payload.get("balance"), dict) else {}
    raw = balance.get("value")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return {
        "ok": True,
        "vendor": "teamo",
        "value": value,
        "currency": (balance.get("currency") or "USD").strip() or "USD",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _teamo_request_api_key() -> str:
    """优先用供应商设置里的 teamo api_key，没有再回落全局密钥。"""
    try:
        from utils.vendor_catalog import vendor_api_key
        key = vendor_api_key("teamo")
        if key:
            return key
    except Exception:
        pass
    return _load_teamo_api_key()


def cached_teamo_balance() -> dict:
    return dict(_balance_cache) if _balance_cache else {
        "ok": False,
        "vendor": "teamo",
        "value": None,
        "currency": "USD",
        "fetched_at": None,
        "error": "not_fetched",
    }


async def fetch_teamo_balance(*, client=None, vendor_base_url: str = "", force: bool = False) -> dict:
    now = time.monotonic()
    cached = cached_teamo_balance()
    if not force and cached.get("ok") and now - float(_balance_cache.get("_mono") or 0) < _BALANCE_TTL:
        return cached
    api_key = _teamo_request_api_key()
    if not api_key:
        out = {"ok": False, "vendor": "teamo", "value": None, "currency": "USD", "error": "missing_api_key", "_mono": now}
        _balance_cache.update(out)
        return cached_teamo_balance()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    own_client = client is None
    if own_client:
        import httpx
        kwargs = {"timeout": 15.0, "trust_env": False}
        if TEAMO_HTTP_PROXY:
            kwargs["proxy"] = TEAMO_HTTP_PROXY
        client = httpx.AsyncClient(**kwargs)
    last_error = "unavailable"
    try:
        async with _balance_lock:
            if not force and _balance_cache.get("ok") and now - float(_balance_cache.get("_mono") or 0) < _BALANCE_TTL:
                return cached_teamo_balance()
            for base in teamo_billing_bases(vendor_base_url):
                url = f"{base}/v1/billing/balance"
                try:
                    resp = await client.get(url, headers=headers, timeout=15.0)
                except Exception as exc:
                    last_error = repr(exc)
                    continue
                if resp.status_code != 200:
                    last_error = f"http_{resp.status_code}"
                    if resp.status_code in {401, 403}:
                        break
                    continue
                try:
                    parsed = parse_teamo_balance(resp.json())
                except Exception:
                    last_error = "invalid_json"
                    continue
                if not parsed:
                    last_error = "unusable_payload"
                    continue
                parsed["_mono"] = time.monotonic()
                _balance_cache.clear()
                _balance_cache.update(parsed)
                return cached_teamo_balance()
    finally:
        if own_client:
            await client.aclose()
    out = {
        "ok": False,
        "vendor": "teamo",
        "value": _balance_cache.get("value"),
        "currency": _balance_cache.get("currency") or "USD",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "error": last_error,
        "_mono": time.monotonic(),
    }
    _balance_cache.update(out)
    return cached_teamo_balance()


def estimate_teamo_catalog_usd(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    vendor: str = "teamo",
    service_tier: str = "standard",
    kind: str = "chat",
) -> Optional[float]:
    from utils.check_user import lookup_model_price, resolve_official_prices
    if kind == "image":
        from utils.image_billing import lookup_image_price
        prices = lookup_image_price(model, teamo=True, vendor=vendor or "teamo")
        if not prices:
            return None
        text_price = float(prices.get("text_input") or prices.get("input") or 0)
        image_in = float(prices.get("image_input") or 0)
        out_price = float(prices.get("output") or 0)
        return (
            input_tokens / 1_000_000 * text_price
            + cache_read_tokens / 1_000_000 * image_in
            + output_tokens / 1_000_000 * out_price
        )
    prices = lookup_model_price(model, teamo=True, service_tier=service_tier, vendor=vendor or "teamo")
    if not prices:
        return None
    official = resolve_official_prices(prices)
    return (
        input_tokens / 1_000_000 * official["input"]
        + output_tokens / 1_000_000 * official["output"]
        + cache_read_tokens / 1_000_000 * official["cache_read"]
        + cache_write_tokens / 1_000_000 * official["cache_write"]
    )


def audit_teamo_charge(
    *,
    req_id: str,
    user_id: str,
    source: str,
    kind: str,
    model: str,
    bill: dict,
    usage: Optional[dict] = None,
    body: Optional[dict] = None,
    vendor: str = "teamo",
) -> list[str]:
    """对照价目估算核对 Teamo 实扣，不合理写入 logs/billing_audit.log。"""
    from utils.check_user import resolve_service_tier
    usage = usage or {}
    body = body or {}
    input_tokens = int(bill.get("input_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(bill.get("output_tokens") or usage.get("output_tokens") or 0)
    cache_read = int(bill.get("cache_read_tokens") or usage.get("cache_read_tokens") or 0)
    cache_write = int(bill.get("cache_write_tokens") or usage.get("cache_write_tokens") or 0)
    usd_cost = float(bill.get("usd_cost") or 0)
    service_tier = resolve_service_tier(body)
    expected = estimate_teamo_catalog_usd(
        model=model or bill.get("model") or "",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        vendor=vendor or "teamo",
        service_tier=service_tier,
        kind=kind,
    )
    issues = []
    if usd_cost < 0:
        issues.append("negative_amount")
    if usd_cost >= _HIGH_AMOUNT_USD:
        issues.append(f"amount_high={usd_cost:.6f}")
    if expected is None:
        if usd_cost >= 0.01:
            issues.append("no_catalog_price")
    elif expected < 1e-8:
        if usd_cost >= 0.01:
            issues.append("expected_zero_but_charged")
    else:
        ratio = usd_cost / expected
        if ratio >= _OVERCHARGE_RATIO:
            issues.append(f"overcharge_x{ratio:.2f}")
        elif ratio <= _UNDERCHARGE_RATIO and usd_cost >= 0.001:
            issues.append(f"undercharge_x{ratio:.2f}")
    our_in = int(usage.get("input_tokens") or 0)
    our_out = int(usage.get("output_tokens") or 0)
    bill_in = int(bill.get("input_tokens") or 0)
    bill_out = int(bill.get("output_tokens") or 0)
    if our_in or our_out:
        if bill_in and abs(bill_in - our_in) > max(200, int(our_in * 0.25)):
            issues.append("input_tokens_mismatch")
        if bill_out and abs(bill_out - our_out) > max(200, int(our_out * 0.25)):
            issues.append("output_tokens_mismatch")
    over_usd, over_pct = _over_stats(usd_cost, expected)
    if not issues:
        return []
    billing_audit_log.warning(
        "unreasonable req=%s user=%s source=%s kind=%s model=%s teamo_request=%s "
        "charged_usd=%.6f expected_usd=%s over_usd=%s over_pct=%s issues=%s "
        "in=%s out=%s cache_r=%s cache_w=%s tier=%s",
        req_id,
        user_id,
        source,
        kind,
        model or "-",
        bill.get("request_id") or "-",
        usd_cost,
        f"{expected:.6f}" if expected is not None else "-",
        _fmt_usd(over_usd),
        _fmt_pct(over_pct),
        ",".join(issues),
        input_tokens,
        output_tokens,
        cache_read,
        cache_write,
        service_tier,
    )
    return issues


_AMOUNT_ABS = 1e-6
_BALANCE_ABS = 0.0005
_BALANCE_REL = 0.02


def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{float(value):.6f}"


def _over_stats(actual: Optional[float], baseline: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if actual is None or baseline is None:
        return None, None
    over_usd = float(actual) - float(baseline)
    if abs(float(baseline)) < 1e-12:
        return over_usd, None
    return over_usd, over_usd / float(baseline) * 100.0


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:+.2f}%"


def compare_teamo_official(
    *,
    charged_usd: float,
    official_bill: Optional[dict] = None,
    balance_before: Optional[dict] = None,
    balance_after: Optional[dict] = None,
) -> dict:
    """对照官网请求账单金额、扣完后余额是否和本次实扣一致。"""
    charged_usd = float(charged_usd or 0)
    issues: list[str] = []
    official_usd = None
    if not official_bill:
        issues.append("official_request_unavailable")
    else:
        official_usd = float(official_bill.get("usd_cost") or 0)
        if abs(official_usd - charged_usd) > max(_AMOUNT_ABS, abs(charged_usd) * 1e-6):
            issues.append("request_amount_mismatch")

    before_val = None
    after_val = None
    expected_remain = None
    if balance_before and balance_before.get("ok") and balance_before.get("value") is not None:
        before_val = float(balance_before["value"])
        expected_remain = before_val - charged_usd
    if balance_after and balance_after.get("ok") and balance_after.get("value") is not None:
        after_val = float(balance_after["value"])
    else:
        issues.append("official_balance_unavailable")

    if expected_remain is not None and after_val is not None:
        gap = after_val - expected_remain
        tolerance = max(_BALANCE_ABS, abs(charged_usd) * _BALANCE_REL)
        if abs(gap) > tolerance:
            # 缓存可能已含本笔扣费，官网余额几乎没再变。
            if abs(charged_usd) > tolerance and abs(after_val - before_val) <= tolerance:
                issues.append("balance_already_applied")
            else:
                issues.append("balance_mismatch")

    actual_deducted = None
    if before_val is not None and after_val is not None:
        actual_deducted = before_val - after_val
    elif official_usd is not None:
        actual_deducted = official_usd
    over_usd, over_pct = _over_stats(actual_deducted, charged_usd)

    hard = [item for item in issues if item != "balance_already_applied"]
    return {
        "match": not hard,
        "issues": issues,
        "charged_usd": charged_usd,
        "official_usd": official_usd,
        "actual_deducted": actual_deducted,
        "over_usd": over_usd,
        "over_pct": over_pct,
        "balance_before": before_val,
        "expected_remain": expected_remain,
        "official_remain": after_val,
    }


def log_teamo_official_compare(
    result: dict,
    *,
    req_id: str,
    user_id: str,
    source: str,
    model: str,
    request_id: str,
) -> None:
    issues = result.get("issues") or []
    line = (
        "compare req=%s user=%s source=%s model=%s teamo_request=%s "
        "charged_usd=%.6f official_usd=%s actual_deducted=%s over_usd=%s over_pct=%s "
        "balance_before=%s expected_remain=%s official_remain=%s match=%s issues=%s"
    )
    args = (
        req_id,
        user_id,
        source,
        model or "-",
        request_id or "-",
        float(result.get("charged_usd") or 0),
        _fmt_usd(result.get("official_usd")),
        _fmt_usd(result.get("actual_deducted")),
        _fmt_usd(result.get("over_usd")),
        _fmt_pct(result.get("over_pct")),
        _fmt_usd(result.get("balance_before")),
        _fmt_usd(result.get("expected_remain")),
        _fmt_usd(result.get("official_remain")),
        "1" if result.get("match") else "0",
        ",".join(issues) if issues else "ok",
    )
    if result.get("match") and not issues:
        billing_audit_log.info(line, *args)
    else:
        billing_audit_log.warning(line, *args)


def log_teamo_official_unavailable(
    *,
    req_id: str,
    user_id: str,
    source: str,
    model: str = "",
    request_id: str = "",
    reason: str,
    charged_usd: Optional[float] = None,
) -> None:
    billing_audit_log.warning(
        "compare req=%s user=%s source=%s model=%s teamo_request=%s "
        "charged_usd=%s official_usd=- actual_deducted=- over_usd=- over_pct=- "
        "balance_before=- expected_remain=- official_remain=- match=0 issues=%s",
        req_id,
        user_id,
        source,
        model or "-",
        request_id or "-",
        _fmt_usd(charged_usd) if charged_usd is not None else "-",
        reason or "official_request_unavailable",
    )


async def verify_teamo_official_after_charge(
    bill: dict,
    *,
    req_id: str,
    user_id: str,
    source: str,
    model: str,
    client=None,
    vendor_base_url: str = "",
) -> dict:
    """扣完用户款后回查官网请求账单和余额，并写入 billing_audit.log。"""
    before = cached_teamo_balance()
    official_bill = None
    request_id = _norm_id((bill or {}).get("request_id"))
    own_client = client is None
    if own_client:
        import httpx
        kwargs = {"timeout": 15.0, "trust_env": False}
        if TEAMO_HTTP_PROXY:
            kwargs["proxy"] = TEAMO_HTTP_PROXY
        client = httpx.AsyncClient(**kwargs)
    try:
        if request_id:
            official_bill = await fetch_teamo_request_bill(
                request_id,
                client=client,
                vendor_base_url=vendor_base_url,
            )
        after = await fetch_teamo_balance(
            client=client,
            vendor_base_url=vendor_base_url,
            force=True,
        )
    finally:
        if own_client:
            await client.aclose()
    result = compare_teamo_official(
        charged_usd=(bill or {}).get("usd_cost") or 0,
        official_bill=official_bill,
        balance_before=before if before.get("ok") else None,
        balance_after=after,
    )
    log_teamo_official_compare(
        result,
        req_id=req_id,
        user_id=user_id,
        source=source,
        model=model,
        request_id=request_id,
    )
    return result
