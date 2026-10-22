import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, AsyncExitStack
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, text, case, or_
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.middleware.cors import CORSMiddleware

from settings.capacity import CAPACITY
from settings.macro import (
    CLAUDE_GPT_KEY, DOR2CHY, LEGACY_UPSTREAM_BASE, PROFIT_MULTIPLIER, TEAMO_HTTP_PROXY, TEAMO_UPSTREAM, UPSTREAM_BASE, VENDOR_UPSTREAM,
)
from utils.agent_task import (
    bind_agent_task,
    explain_task,
    extract_session_key,
    schedule_agent_task,
)
from utils.anthropic_bridge import (
    OpenAIToAnthropicStream,
    openai_completion_to_anthropic,
    prepare_anthropic_upstream,
)
from utils.openai_responses_bridge import (
    ResponsesToChatStream,
    needs_gpt6_responses_bridge,
    prepare_gpt6_responses_upstream,
    responses_to_chat_completion,
)
from utils.check_user import (
    apply_upstream_auth,
    check_user_message,
    compute_user_token,
    has_original_prices,
    long_context_quote,
    lookup_model_price,
    resolve_billing_and_cost_prices,
    resolve_service_tier,
)
from utils.common_utils import (
    AUTH_FAIL_REASONS,
    StreamUsageCollector,
    auth_fail_message,
    auth_fail_status,
    build_error_payload,
    common_message_yield,
    detect_source,
    extract_completion_meta,
    extract_embedding_usage,
    extract_gemini_model_from_path,
    replace_gemini_model_in_path,
    format_access_usage,
    is_audio_path,
    extract_usage,
    include_usage_for_chat_stream,
    is_billable_path,
    is_count_tokens_path,
    is_client_disconnect,
    is_audio_chat_completion,
    is_embedding_path,
    is_gemini_native_path,
    is_image_path,
    is_models_path,
    strip_openai_model_prefix,
    is_stream_terminator,
    rewrite_models_payload,
    strip_openai_prefix_from_request,
    apply_grok_46_defaults_to_request,
    apply_upstream_model_to_request,
    map_model_to_upstream,
    ensure_chat_tools_reasoning_none,
    ensure_sse_event_name,
    normalize_max_tokens_for_upstream,
    normalize_upstream_path,
    parse_request_payload,
    parse_sse_block,
    responses_completion_status,
    sanitize_headers,
    sanitize_openai_chat_request,
    sanitize_stateless_responses_input,
    summarize_request_keys,
    split_sse_events_bytes,
)
from utils.cursor_adapter import (
    dump_cursor_request_debug,
    is_cursor_client,
    sanitize_cursor_openai_request,
)
from utils.image_billing import compute_image_charge, extract_image_usage, lookup_image_price
from settings.database import AsyncSessionLocal, async_engine
from settings.models import (
    AgentTask,
    Bill,
    ModelAlias,
    ModelProtocol,
    ModelRewrite,
    ModelRoute,
    PricingSettings,
    PromptInjection,
    RequestUsage,
    User,
    UpstreamVendor,
    UpstreamVendorKey,
    UserApiKey,
    VendorModelPrice,
)
from utils.pricing import ensure_pricing_settings, get_pricing_config, update_pricing_settings
from utils.vendor_catalog import (
    activate_vendor,
    active_vendor_code,
    apply_catalog_rate_prices,
    create_or_update_vendor,
    create_vendor_key,
    delete_vendor_key,
    delete_vendor_model_price,
    ensure_vendor_catalog,
    ensure_vendor_schema_columns,
    get_admin_vendor_model,
    get_runtime_upstream,
    get_vendor_catalog,
    list_admin_vendor_models,
    list_official_catalog,
    list_vendor_keys,
    list_vendors,
    migrate_vendor_keys,
    pricing_supervisor,
    refresh_vendor_prices,
    reload_cache_from_db,
    resolve_vendor,
    scale_catalog_rows,
    update_vendor_key,
    upsert_single_vendor_model,
    upsert_vendor_prices,
    vendor_api_key,
    vendor_profit_multiplier,
    vendor_resolved_api_key,
    PRICING_MODE_CATALOG_RATE,
)
from utils.api_key_auth import lookup_user_by_credential, phone_blocked_for_model
from utils.check_user import extract_user_key
from utils.model_route import (
    delete_route,
    list_routes,
    overlay_catalog_rows,
    reload_routes,
    replace_route_api_keys,
    resolve_runtime_for_model,
    upsert_route,
)
from utils.model_rewrite import (
    apply_model_rewrite,
    delete_rewrite,
    list_rewrites,
    reload_rewrites,
    upsert_rewrite,
)
from utils.model_alias import (
    apply_alias_to_request,
    apply_aliases_to_catalog,
    apply_aliases_to_models_payload,
    delete_alias,
    list_aliases,
    reload_aliases,
    upsert_alias,
)
from utils.model_protocol import (
    apply_protocol_to_models_payload,
    delete_protocol,
    list_protocols,
    match_model_protocol,
    reload_protocols,
    upsert_protocol,
)
from utils.protocol_adapt import (
    ProtocolStreamBridge,
    convert_upstream_error,
    convert_upstream_response,
    prepare_protocol_upstream,
)
from utils.prompt_injection import (
    apply_prompt_injection,
    delete_injection,
    list_injections,
    reload_injections,
    upsert_injection,
)
from utils.teamo_billing import (
    charge_from_teamo_bill,
    extract_teamo_request_id,
    fetch_teamo_balance,
    fetch_teamo_request_bill,
    log_teamo_official_unavailable,
)
from utils.request_usage import (
    LATENCY_ALERT_MS,
    LATENCY_LOOKBACK,
    LATENCY_WINDOW,
    backfill_usage_balances,
    ensure_usage_schema_columns,
    finish_request_usage,
    latency_models_from_samples,
    retarget_request_usage,
    start_request_usage,
)
from utils.unbilled_billing import unbilled_supervisor, usage_has_billable_tokens, usage_is_billed
from utils.get_log import access_log, deepseek_slow_log, process_log, setup_standalone_logging
from utils.headroom_compress import get_stats, maybe_compress_request, warmup_headroom


HTTP_MAX_CONNECTIONS = CAPACITY["http_max_connections"]
HTTP_MAX_KEEPALIVE = CAPACITY["http_max_keepalive"]
UPSTREAM_CONCURRENCY = CAPACITY["upstream_concurrency"]
COMPRESS_THREADS = CAPACITY["compress_threads"]
UVICORN_WORKERS = CAPACITY["workers"]
LIMIT_CONCURRENCY = CAPACITY["limit_concurrency"]

# 供应商偶发 502 / 连接抖动：对 agent 屏蔽瞬时失败，固定重试。
UPSTREAM_RETRY_ATTEMPTS = 5
UPSTREAM_RETRY_SLEEP = 1.0
UPSTREAM_RETRY_STATUSES = {502}
# 读超时只切断长时间没有任何字节的连接。正常流式输出会不断重置这只计时器。
UPSTREAM_READ_TIMEOUT = 300.0
UPSTREAM_POOL_TIMEOUT = 15.0
UPSTREAM_CLOSE_TIMEOUT = 5.0
# DeepSeek 首字超过该阈值单独记录到 deepseek_slow.log，便于事后分析慢请求。
DEEPSEEK_SLOW_TTFB_MS = 10_000


def format_upstream_exc(exc: BaseException) -> str:
    """httpx 的超时异常经常没有 message，日志里要带上类型名。"""
    text = str(exc).strip()
    name = type(exc).__name__
    return f"{name}: {text}" if text else name


def should_retry_stream_transport(exc: BaseException, attempt: int, client_events: int) -> bool:
    """已经向客户端输出过内容时不能重试。连接池耗尽时重试也拿不到连接，只会把请求挂在 pending。"""
    if client_events > 0:
        return False
    if isinstance(exc, httpx.PoolTimeout):
        return False
    return attempt < UPSTREAM_RETRY_ATTEMPTS


async def release_upstream_stream(stream_cm, resp, *, timeout: float = UPSTREAM_CLOSE_TIMEOUT) -> None:
    """归还一次上游流。关闭卡住时直接拆 TCP，避免对端已断开的套接字停在 CLOSE-WAIT 里占满连接池。"""

    async def _close() -> None:
        try:
            await stream_cm.__aexit__(None, None, None)
            return
        except Exception:
            pass
        if resp is not None:
            try:
                await resp.aclose()
            except Exception:
                pass

    try:
        await asyncio.wait_for(_close(), timeout)
    except Exception:
        network = None
        if resp is not None:
            network = (getattr(resp, "extensions", None) or {}).get("network_stream")
        closer = getattr(network, "aclose", None) or getattr(network, "close", None)
        if closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            process_log.warning("force close upstream socket failed", exc_info=True)


async def _request_upstream_with_retry(
    client: httpx.AsyncClient,
    *, 
    method: str,
    url: str,
    headers: dict,
    content: bytes,
    req_id: str,
) -> httpx.Response:
    """非流式上游请求：502 或传输异常时重试，耗尽后再把结果交给调用方。"""
    last_resp: Optional[httpx.Response] = None
    for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
        try:
            last_resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=content,
                )
        except httpx.HTTPError as exc:
            process_log.warning(
                "upstream request error req=%s attempt=%s/%s err=%s",
                req_id,
                attempt,
                UPSTREAM_RETRY_ATTEMPTS,
                exc,
            )
            if attempt < UPSTREAM_RETRY_ATTEMPTS:
                await asyncio.sleep(UPSTREAM_RETRY_SLEEP)
                continue
            raise

        if last_resp.status_code not in UPSTREAM_RETRY_STATUSES:
            if attempt > 1:
                process_log.info(
                    "upstream retry ok req=%s attempt=%s status=%s",
                    req_id,
                    attempt,
                    last_resp.status_code,
                )
            return last_resp

        process_log.warning(
            "upstream retryable status req=%s status=%s attempt=%s/%s sleep=%ss",
            req_id,
            last_resp.status_code,
            attempt,
            UPSTREAM_RETRY_ATTEMPTS,
            UPSTREAM_RETRY_SLEEP,
        )
        if attempt < UPSTREAM_RETRY_ATTEMPTS:
            await asyncio.sleep(UPSTREAM_RETRY_SLEEP)

    assert last_resp is not None
    return last_resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_standalone_logging()
    pricing_stop = asyncio.Event()
    pricing_task = None
    unbilled_task = None
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=COMPRESS_THREADS, thread_name_prefix="compress")
    loop.set_default_executor(executor)
    timeout = httpx.Timeout(
        connect=10.0,
        read=UPSTREAM_READ_TIMEOUT,
        write=60.0,
        pool=UPSTREAM_POOL_TIMEOUT,
    )
    limits = httpx.Limits(
        max_connections=HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_MAX_KEEPALIVE,
        keepalive_expiry=30.0,
    )
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(httpx.AsyncClient(
            timeout=timeout, limits=limits, trust_env=False,
        ))
        app.state.http = client
        if TEAMO_HTTP_PROXY:
            app.state.http_teamo = await stack.enter_async_context(httpx.AsyncClient(
                timeout=timeout, limits=limits, trust_env=False, proxy=TEAMO_HTTP_PROXY,
            ))
        else:
            app.state.http_teamo = client
        app.state.upstream_sema = asyncio.Semaphore(UPSTREAM_CONCURRENCY)
        app.state.bg_billing_tasks = set()
        async with async_engine.begin() as conn:
            locked = await conn.scalar(text("SELECT GET_LOCK('claude_proxy_request_usage_ddl', 10)"))
            if locked != 1:
                raise RuntimeError("could not acquire request usage schema lock")
            try:
                await conn.run_sync(RequestUsage.__table__.create, checkfirst=True)
                await conn.run_sync(AgentTask.__table__.create, checkfirst=True)
                await ensure_usage_schema_columns(conn)
                await conn.run_sync(PricingSettings.__table__.create, checkfirst=True)
                await conn.run_sync(UpstreamVendor.__table__.create, checkfirst=True)
                await conn.run_sync(UpstreamVendorKey.__table__.create, checkfirst=True)
                await conn.run_sync(VendorModelPrice.__table__.create, checkfirst=True)
                await conn.run_sync(UserApiKey.__table__.create, checkfirst=True)
                await conn.run_sync(ModelRoute.__table__.create, checkfirst=True)
                await conn.run_sync(ModelProtocol.__table__.create, checkfirst=True)
                await conn.run_sync(ModelAlias.__table__.create, checkfirst=True)
                await conn.run_sync(ModelRewrite.__table__.create, checkfirst=True)
                await conn.run_sync(PromptInjection.__table__.create, checkfirst=True)
                await ensure_vendor_schema_columns(conn)
                await conn.execute(text(
                    "INSERT IGNORE INTO tb_pricing_settings "
                    "(id, usd_cny_rate, profit_multiplier) VALUES (1, :rate, :multiplier)"
                ), {"rate": DOR2CHY, "multiplier": PROFIT_MULTIPLIER})
            finally:
                await conn.execute(text("SELECT RELEASE_LOCK('claude_proxy_request_usage_ddl')"))
        app.state.balance_backfill_task = asyncio.create_task(backfill_usage_balances())
        await ensure_pricing_settings()
        await ensure_vendor_catalog()
        await migrate_vendor_keys()
        await reload_cache_from_db()
        await reload_routes()
        await reload_protocols()
        await reload_aliases()
        await reload_rewrites()
        await reload_injections()
        await refresh_vendor_prices(force=True)
        pricing_task = asyncio.create_task(pricing_supervisor(pricing_stop))
        unbilled_task = asyncio.create_task(unbilled_supervisor(pricing_stop))
        await asyncio.to_thread(warmup_headroom)
        runtime = get_runtime_upstream()
        process_log.info(
            "proxy started profile=%s cpus=%s mem_mb=%s workers=%s upstream=%s http_max=%s compress=%s db_pool=%s+%s "
            "vendor=%s upstream_base=%s",
            CAPACITY["profile"],
            CAPACITY["cpus"],
            CAPACITY["mem_mb"],
            UVICORN_WORKERS,
            UPSTREAM_CONCURRENCY,
            HTTP_MAX_CONNECTIONS,
            COMPRESS_THREADS,
            CAPACITY["pool_size"],
            CAPACITY["pool_overflow"],
            runtime["code"],
            runtime["base_url"],
        )
        yield
        process_log.info("proxy stopping")
        pricing_stop.set()
        pending_billing = list(getattr(app.state, "bg_billing_tasks", set()) or [])
        if pending_billing:
            await asyncio.gather(*pending_billing, return_exceptions=True)
        background_tasks = [task for task in (pricing_task, unbilled_task) if task]
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
    executor.shutdown(wait=False)


app = FastAPI(debug=False, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _parse_json_body(body: bytes):
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode(errors="ignore"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _header_value(headers: dict, name: str) -> str:
    target = name.lower()
    for key, value in (headers or {}).items():
        if key.lower() == target:
            return value or ""
    return ""


def _extract_upstream_error_message(err_text: str, status_code: int) -> str:
    try:
        err_obj = json.loads(err_text or "{}")
    except Exception:
        err_obj = {}
    if isinstance(err_obj, dict):
        err = err_obj.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err.get("message"))
        if err_obj.get("message"):
            return str(err_obj.get("message"))
    text = (err_text or "").strip()
    return text[:500] if text else f"upstream {status_code}"


def _stream_error_chunks(source: str, message: str, status_code: int = 502):
    """按客户端协议吐出可识别的流式错误，避免空 SSE 被 Claude Code 报成 Stream ended。"""
    msg = (message or f"upstream {status_code}").strip()
    if source == "anthropic":
        payload = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": msg,
            },
        }
        yield (
            "event: error\ndata: "
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n"
        ).encode("utf-8")
        return
    yield (
        "data: "
        + json.dumps(
            {
                "error": {
                    "message": msg,
                    "type": "upstream_error",
                    "code": status_code,
                }
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode("utf-8")
    yield b"data: [DONE]\n\n"


def _is_gpt_audio_model(model: str) -> bool:
    normalized = strip_openai_model_prefix(model or "").lower()
    return normalized == "gpt-audio" or normalized.startswith("gpt-audio-")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _elapsed_ms(started: float, ended: Optional[float] = None) -> int:
    end = time.perf_counter() if ended is None else ended
    return int((end - started) * 1000)


def _log_access(
    request: Request,
    *,
    req_id: str,
    user_id: str,
    source: str,
    model: str,
    stream: bool,
    status: int,
    started: float,
    extra: str = "",
    base_url: str = "",
    ttfb_ms: Optional[int] = None,
    done_ms: Optional[int] = None,
    usage: Optional[dict] = None,
    billing: Optional[dict] = None,
    finish: str = "",
    result: str = "",
):
    parts = [
        f"req={req_id}",
        f"ip={_client_ip(request)}",
        f"user={user_id or '-'}",
        f"{request.method} {request.url.path}",
        f"source={source}",
        f"model={model or '-'}",
        f"stream={int(bool(stream))}",
        f"status={status}",
        f"ms={_elapsed_ms(started)}",
    ]
    if base_url:
        parts.append(f"base_url={base_url}")
    if ttfb_ms is not None:
        parts.append(f"ttfb_ms={ttfb_ms}")
    if done_ms is not None:
        parts.append(f"done_ms={done_ms}")
    token_part = format_access_usage(usage, billing)
    if token_part:
        parts.append(token_part)
    if finish:
        parts.append(f"finish={finish}")
    if result and "result=" not in (extra or ""):
        parts.append(f"result={result}")
    if request.url.query:
        parts.append(f"query={request.url.query}")
    if extra:
        parts.append(extra)
    access_log.info(" ".join(parts))
    schedule_agent_task(req_id=req_id, finish=finish, result=result, http_status=status)


def is_deepseek_model(model: str) -> bool:
    """模型名是否属于 DeepSeek 家族。

    DeepSeek 专属逻辑必须同时满足「deepseek 模型 + deepseek 供应商」，避免
    别的模型被路由到 deepseek 供应商时也套用这些规则。
    """
    name = strip_openai_model_prefix(model or "").strip().lower()
    if name.startswith("ft:"):
        parts = name.split(":")
        if len(parts) >= 2 and parts[1]:
            name = parts[1]
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.startswith("deepseek")


def _log_deepseek_slow(
    *,
    request: Request,
    req_id: str,
    user_id: str,
    source: str,
    model: str,
    upstream_model: str,
    upstream_base: str,
    target_url: str,
    stage: str,
    started: float,
    first_event_at: float,
    headers_at: Optional[float],
    body_len: int,
    heartbeats: int,
    headers: dict,
    parsed_body: dict,
):
    """DeepSeek 上游首字超过阈值时单独留档，便于事后按供应商分析慢请求。"""
    ttfb_ms = _elapsed_ms(started, first_event_at)
    if ttfb_ms < DEEPSEEK_SLOW_TTFB_MS:
        return
    deepseek_slow_log.info(
        "slow_ttfb req=%s ip=%s user=%s source=%s model=%s upstream_model=%s path=%s "
        "base_url=%s target_url=%s stage=%s ttfb_ms=%s headers_ms=%s "
        "body_bytes=%s heartbeats=%s beta=%s keys=%s session=%s",
        req_id,
        _client_ip(request),
        user_id or "-",
        source,
        model or "-",
        upstream_model or "-",
        request.url.path,
        upstream_base,
        target_url,
        stage,
        ttfb_ms,
        _elapsed_ms(started, headers_at) if headers_at is not None else "-",
        body_len,
        heartbeats,
        (_header_value(headers, "anthropic-beta") or "-")[:160],
        summarize_request_keys(parsed_body),
        extract_session_key(parsed_body) or "-",
    )


def _auth_error_response(source: str, reason: str, want_stream: bool):
    message = auth_fail_message(reason)
    status = auth_fail_status(reason)
    payload = build_error_payload(source, message, reason)
    if want_stream:
        return StreamingResponse(
            common_message_yield(message, source=source),
            status_code=status,
            media_type="text/event-stream",
        )
    return JSONResponse(status_code=status, content=payload)


async def _charge_if_needed(
    usage: dict,
    body: dict,
    user_id: str,
    source: str,
    path: str,
    req_id: str,
    status_code: int = 200,
    use_teamo_prices: Optional[bool] = None,
    vendor: str = "",
    upstream_request_id: str = "",
    http_client=None,
    vendor_base_url: str = "",
    started: Optional[float] = None,
    first_byte_at: Optional[float] = None,
    skip_upstream_bill: bool = False,
):
    if status_code >= 400:
        return None
    if not user_id or user_id in AUTH_FAIL_REASONS:
        return None
    if not is_billable_path(path):
        return None
    teamo_direct = bool(use_teamo_prices) or (vendor or "").strip().lower() == "teamo"
    teamo_fallback = ""
    try:
        # 客户端已断开时不再等上游官方账单，直接用当前供应商价目。
        if (not skip_upstream_bill) and teamo_direct and upstream_request_id and http_client is not None:
            bill = await fetch_teamo_request_bill(
                upstream_request_id,
                client=http_client,
                vendor_base_url=vendor_base_url,
            )
            if bill:
                kind = "image" if (is_image_path(path) or is_gemini_native_path(path)) else "chat"
                summary = await charge_from_teamo_bill(
                    bill,
                    user_id=user_id,
                    body=body,
                    usage=usage,
                    req_id=req_id,
                    source=source,
                    kind=kind,
                    vendor=vendor,
                    client=http_client,
                    vendor_base_url=vendor_base_url,
                    started=started,
                    first_byte_at=first_byte_at,
                )
                if summary:
                    return summary
            teamo_fallback = "official_request_unavailable"
            process_log.warning(
                "teamo request bill fallback req=%s teamo_request=%s",
                req_id, upstream_request_id,
            )
        elif teamo_direct and not upstream_request_id:
            teamo_fallback = "missing_request_id"
            process_log.warning("teamo request id missing req=%s path=%s, fallback token billing", req_id, path)

        def _note_teamo_fallback(summary: Optional[dict] = None) -> None:
            if not teamo_fallback:
                return
            reason = teamo_fallback
            if summary:
                reason = f"{reason},fallback_token"
            log_teamo_official_unavailable(
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=(body or {}).get("model") or (summary or {}).get("model") or "",
                request_id=upstream_request_id,
                reason=reason,
                charged_usd=(summary or {}).get("usd_cost"),
            )

        if is_image_path(path) or is_gemini_native_path(path):
            if not usage:
                _note_teamo_fallback()
                return None
            summary = await compute_image_charge(
                usage=usage,
                body=body,
                user_id=user_id,
                source=source,
                req_id=req_id,
                use_teamo_prices=bool(use_teamo_prices),
                vendor=vendor,
                started=started,
                first_byte_at=first_byte_at,
            )
            if summary:
                _note_teamo_fallback(summary)
                process_log.info(
                    "billing done req=%s user=%s source=%s model=%s billing=%s tier=%s text=%s image_in=%s out=%s n=%s spent=%.6f profit=%.6f",
                    req_id,
                    user_id,
                    source,
                    summary.get("model"),
                    summary.get("billing") or "token",
                    summary.get("size_tier") or "-",
                    summary.get("text_tokens"),
                    summary.get("image_input_tokens"),
                    summary.get("output_tokens"),
                    summary.get("n"),
                    summary.get("charged") or 0,
                    summary.get("profit") or 0,
                )
                return summary
            _note_teamo_fallback()
            process_log.warning("billing skipped req=%s user=%s source=%s usage=%s", req_id, user_id, source, usage)
            return None

        if not usage:
            _note_teamo_fallback()
            return None
        summary = await compute_user_token(
            token_dict=usage,
            user_id=user_id,
            body=body,
            source=source,
            req_id=req_id,
            use_teamo_prices=use_teamo_prices,
            vendor=vendor,
            started=started,
            first_byte_at=first_byte_at,
        )
        if summary:
            _note_teamo_fallback(summary)
            process_log.info(
                "billing done req=%s user=%s source=%s in=%s cache_read=%s cache_write=%s out=%s spent=%.6f profit=%.6f",
                req_id,
                user_id,
                source,
                summary.get("input_tokens"),
                summary.get("cache_read_tokens") or 0,
                summary.get("cache_write_tokens") or 0,
                summary.get("output_tokens"),
                summary.get("charged") or 0,
                summary.get("profit") or 0,
            )
            return summary
        _note_teamo_fallback()
        process_log.warning("billing skipped req=%s user=%s source=%s usage=%s", req_id, user_id, source, usage)
    except Exception:
        process_log.exception("charge failed req=%s user=%s path=%s", req_id, user_id, path)
    return None


def _merge_compression(billing: Optional[dict], compression: Optional[dict]) -> Optional[dict]:
    if not billing or not compression:
        return billing
    billing["headroom_tokens_before"] = compression.get("tokens_before")
    billing["headroom_tokens_after"] = compression.get("tokens_after")
    billing["headroom_tokens_saved"] = compression.get("tokens_saved")
    billing["headroom_ratio"] = compression.get("compression_ratio")
    return billing


@app.middleware("http")
async def log_and_forward_requests(request: Request, call_next):
    started = time.perf_counter()
    req_id = uuid.uuid4().hex[:12]

    path = request.url.path
    if path in {
        "/docs", "/redoc", "/openapi.json", "/health", "/headroom/stats",
        "/v1/usage", "/v1/usage/summary", "/v1/usage/latency",
        "/v1/account/balance", "/v1/account/spending",
        "/v1/tasks", "/v1/tasks/status", "/v1/tasks/reevaluate",
        "/v1/tasks/complete-all", "/v1/tasks/delete", "/v1/tasks/delete-done",
        "/v1/vendors", "/v1/pricing/models",
    } or path.startswith("/v1/admin/"):
        response = await call_next(request)
        _log_access(
            request,
            req_id=req_id,
            user_id="-",
            source="-",
            model="-",
            stream=False,
            status=response.status_code,
            started=started,
        )
        return response

    try:
        body = await request.body()
    except Exception:
        process_log.exception("req=%s failed to read request body", req_id)
        body = b""

    headers = sanitize_headers(dict(request.headers))
    parsed_body = parse_request_payload(dict(request.headers), body)
    if not parsed_body:
        parsed_body = _parse_json_body(body)
    body, parsed_body = strip_openai_prefix_from_request(body, parsed_body)
    if (not parsed_body.get("model")) and is_gemini_native_path(request.url.path):
        gemini_model = extract_gemini_model_from_path(request.url.path)
        if gemini_model:
            parsed_body["model"] = gemini_model
    body, parsed_body, alias_rule = apply_alias_to_request(body, parsed_body)
    source = detect_source(request.url.path, headers)
    model = parsed_body.get("model") or "-"
    client_model = (alias_rule or {}).get("alias") or model
    want_stream = bool(parsed_body.get("stream")) or "text/event-stream" in (
        headers.get("accept") or ""
    )
    # 计数接口固定非流式，避免客户端 Accept 把头带成 SSE 后把 JSON 当流处理。
    count_tokens_route = is_count_tokens_path(request.url.path)
    if count_tokens_route:
        want_stream = False
    user_id = "-"
    await start_request_usage(req_id, extract_user_key(dict(request.headers)), client_model, request.url.path, source)

    process_log.info(
        "incoming req=%s %s %s source=%s model=%s alias=%s stream=%s ip=%s body_bytes=%s beta=%s",
        req_id,
        request.method,
        request.url,
        source,
        model,
        (alias_rule or {}).get("alias") or "-",
        int(want_stream),
        _client_ip(request),
        len(body or b""),
        (_header_value(headers, "anthropic-beta") or "-")[:160],
    )

    rewrite_rule = None
    if request.method not in {"OPTIONS", "HEAD"}:
        raw_key = extract_user_key(dict(request.headers))
        if is_billable_path(request.url.path, "" if model == "-" else model):
            blocked = False
            try:
                async with AsyncSessionLocal() as db:
                    blocked = await phone_blocked_for_model(db, raw_key)
            except Exception:
                process_log.exception("phone model gate failed req=%s", req_id)
                blocked = bool(raw_key) and not str(raw_key).startswith("sk-")
            if blocked:
                message = "手机号不能用来请求模型，请改用 sk- 密钥"
                process_log.warning("phone model blocked req=%s", req_id)
                _log_access(
                    request,
                    req_id=req_id,
                    user_id=raw_key or "-",
                    source=source,
                    model=model,
                    stream=want_stream,
                    status=403,
                    started=started,
                    extra="reason=phone_model_blocked",
                )
                await finish_request_usage(req_id, 403)
                if want_stream:
                    return StreamingResponse(
                        common_message_yield(message, source=source),
                        status_code=403,
                        media_type="text/event-stream",
                    )
                return JSONResponse(
                    status_code=403,
                    content=build_error_payload(source, message, "phone_model_blocked"),
                )
        try:
            headers, user_id = await check_user_message(headers)
            process_log.info("auth req=%s user=%s source=%s", req_id, user_id, source)
        except Exception:
            process_log.exception("check_user_message failed req=%s", req_id)
            headers, user_id = None, "not exist"

        if not headers:
            status = auth_fail_status(user_id or "not exist")
            process_log.warning("auth rejected req=%s user=%s status=%s", req_id, user_id, status)
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id or "-",
                source=source,
                model=model,
                stream=want_stream,
                status=status,
                started=started,
                extra=f"reason={user_id}",
            )
            await finish_request_usage(req_id, status)
            return _auth_error_response(source, user_id or "not exist", want_stream)

        if model and model != "-":
            body, parsed_body, rewrite_rule = apply_model_rewrite(
                body, parsed_body, user_key=str(user_id or ""),
            )
            if rewrite_rule:
                rewritten = (parsed_body.get("model") or "").strip() or model
                process_log.info(
                    "model rewrite req=%s from=%s to=%s rule=%s",
                    req_id,
                    rewrite_rule.get("from_model") or model,
                    rewritten,
                    rewrite_rule.get("model") or "-",
                )
                model = rewritten
                await retarget_request_usage(req_id, model)

    audio_route = is_audio_path(request.url.path)
    audio_model = _is_gpt_audio_model(model)
    # GPT-Audio 始终走原有 LiteLLM（4001）和本地标准价目表，不受上游模式影响。
    legacy_audio_request = audio_route or audio_model
    runtime = (
        get_runtime_upstream(vendor="legacy")
        if legacy_audio_request
        else resolve_runtime_for_model(model, user_key=user_id)
    )
    runtime_teamo = bool(runtime.get("teamo"))
    runtime_yunxin = bool(runtime.get("yunxin"))
    runtime_vendor = (runtime.get("code") or "").strip().lower()

    if runtime_teamo and is_billable_path(request.url.path, model) and not legacy_audio_request:
        if not (vendor_resolved_api_key(runtime_vendor) or CLAUDE_GPT_KEY):
            await finish_request_usage(req_id, 503)
            return JSONResponse(status_code=503, content={"error": {"message": "TEAMO_API_KEY is not configured"}})
        image_route = is_image_path(request.url.path) or is_gemini_native_path(request.url.path)
        if is_audio_path(request.url.path):
            await finish_request_usage(req_id, 400)
            return JSONResponse(status_code=400, content={"error": {"message": "TeamoRouter model price is not published; billing is unavailable for this model"}})
        if image_route:
            if not lookup_image_price(model, teamo=True, vendor=runtime_vendor):
                await finish_request_usage(req_id, 400)
                return JSONResponse(status_code=400, content={"error": {"message": "TeamoRouter image model price is not published; billing is unavailable for this model"}})
        elif not lookup_model_price(model, teamo=True, service_tier=resolve_service_tier(parsed_body), vendor=runtime_vendor):
            await finish_request_usage(req_id, 400)
            return JSONResponse(status_code=400, content={"error": {"message": "TeamoRouter model price is not published; billing is unavailable for this model"}})
        # 官网的长上下文档另行计价；静态价目表仅记录基础档，长上下文费用需另行核对。
    elif legacy_audio_request:
        # /v1/audio/* 与 gpt-audio Chat Completions 都走 LiteLLM (4001)。
        if not lookup_model_price(model):
            await finish_request_usage(req_id, 400)
            return JSONResponse(status_code=400, content={"error": {"message": "Audio model price is not configured"}})

    # Cursor 专用适配；Claude Code / Codex 保持原样。
    cursor_client = source == "openai" and is_cursor_client(headers, parsed_body)
    if cursor_client:
        compression = None
        body, parsed_body = sanitize_cursor_openai_request(body, parsed_body)
        dump_cursor_request_debug(req_id, parsed_body, body)
    else:
        body, parsed_body, compression = await maybe_compress_request(
            method=request.method,
            path=request.url.path,
            source=source,
            body=body,
            parsed_body=parsed_body,
            model=model if model != "-" else "",
            req_id=req_id,
        )
    body, parsed_body = apply_grok_46_defaults_to_request(body, parsed_body)
    model = parsed_body.get("model") or model
    if not is_audio_path(request.url.path) and _is_gpt_audio_model(model):
        model = strip_openai_model_prefix(model)
        parsed_body["model"] = model
    # 音频接口可能是 multipart/form-data；去 provider 前缀后再识别定价型号。
    if is_audio_path(request.url.path):
        model = strip_openai_model_prefix(model)
        parsed_body["model"] = model
        if not _is_gpt_audio_model(model):
            await finish_request_usage(req_id, 400)
            return JSONResponse(status_code=400, content={"error": {"message": "Only gpt-audio models are enabled for audio endpoints"}})
    if not legacy_audio_request:
        runtime = resolve_runtime_for_model(model, user_key=user_id)
        runtime_teamo = bool(runtime.get("teamo"))
        runtime_yunxin = bool(runtime.get("yunxin"))
        runtime_vendor = (runtime.get("code") or "").strip().lower()
    injection_ids: list[int] = []
    if (
        not legacy_audio_request
        and not is_audio_path(request.url.path)
        and not is_image_path(request.url.path)
        and not is_gemini_native_path(request.url.path)
    ):
        body, parsed_body, injection_ids = apply_prompt_injection(
            body,
            parsed_body,
            model=model if model != "-" else "",
            client_model=client_model if client_model != "-" else "",
            source=source,
        )
    # 仅号商模式：标准名 → -hc。本地 LiteLLM 模式不覆盖模型。
    # count_tokens 固定交给 Teamo，模型名按 Teamo 别名映射，不套用云信 -hc。
    body, parsed_body = apply_upstream_model_to_request(
        body,
        parsed_body,
        teamo=True if count_tokens_route else runtime_teamo,
        yunxin=False if count_tokens_route else runtime_yunxin,
    )
    # Teamo/号商 chat：非 Cursor 也要清洗（stream_options / max_completion_tokens 等易 422）
    if (
        source == "openai"
        and (runtime_teamo or runtime_yunxin)
        and not cursor_client
        and not is_audio_path(request.url.path)
        and not is_image_path(request.url.path)
        and not is_gemini_native_path(request.url.path)
    ):
        body, parsed_body = sanitize_openai_chat_request(body, parsed_body)
    # LiteLLM→官方 OpenAI：gpt-5/6、o 系列把 max_tokens 改成 max_completion_tokens
    # Teamo/号商：把 max_completion_tokens 收回 max_tokens
    if source == "openai":
        body, parsed_body = normalize_max_tokens_for_upstream(body, parsed_body)
    upstream_model = (
        map_model_to_upstream(
            model,
            teamo=True if count_tokens_route else runtime_teamo,
            yunxin=False if count_tokens_route else runtime_yunxin,
        )
        if model and model != "-" else model
    )
    # audio_model 在上面校验了自有费率，故可安全旁路 Teamo；直接交由原始 LiteLLM (4001)。
    # 只移除普通 OpenAI 前缀，保持带日期的上游 model id 不变。
    if audio_model and not runtime_teamo:
        legacy_audio_request = True
    # 协议规则指定上游协议。客户端格式不同就先转换，响应再转回去。
    # 没有规则时，仍保留号商 Anthropic→OpenAI、以及 GPT-6 tools→Responses。
    protocol_rule = None
    if (
        not legacy_audio_request
        and not is_audio_path(request.url.path)
        and not is_image_path(request.url.path)
        and not is_gemini_native_path(request.url.path)
        and model
        and model != "-"
        and source in {"openai", "anthropic", "responses"}
    ):
        protocol_rule = match_model_protocol(model)
    target_proto = (protocol_rule or {}).get("protocol") or ""
    protocol_adapt = bool(target_proto and target_proto != source)
    if protocol_adapt:
        anthropic_bridge = source == "anthropic" and target_proto == "openai"
        responses_bridge = source == "openai" and target_proto == "responses"
    elif protocol_rule:
        anthropic_bridge = False
        responses_bridge = False
    else:
        anthropic_bridge = runtime_yunxin and source == "anthropic"
        responses_bridge = (
            (not runtime_yunxin and not runtime_teamo)
            and source == "openai"
            and needs_gpt6_responses_bridge(parsed_body)
        )
    protocol_response = protocol_adapt and not anthropic_bridge and not responses_bridge
    # 仍走 chat/completions 时，gpt-5/6 + tools 必须 reasoning_effort=none
    if source == "openai" and not responses_bridge and not runtime_teamo and not _is_gpt_audio_model(model):
        body, parsed_body = ensure_chat_tools_reasoning_none(body, parsed_body)
    if source == "openai" and not responses_bridge and not runtime_yunxin and not _is_gpt_audio_model(model):
        body, parsed_body = include_usage_for_chat_stream(body, parsed_body)
    if count_tokens_route:
        # 保持 Anthropic count_tokens 正文，不按模型协议桥到 chat/completions。
        anthropic_bridge = False
        responses_bridge = False
        protocol_adapt = False
        protocol_response = False
        upstream_path = "/v1/messages/count_tokens"
    elif protocol_adapt:
        body, parsed_body, headers, upstream_path = prepare_protocol_upstream(
            parsed_body, headers, source, target_proto
        )
    elif anthropic_bridge:
        body, _openai_parsed, headers, bridged_path = prepare_anthropic_upstream(
            body, parsed_body, headers
        )
        upstream_path = bridged_path
    elif responses_bridge:
        body, _responses_parsed, headers, bridged_path = prepare_gpt6_responses_upstream(
            body, parsed_body, headers
        )
        upstream_path = bridged_path
    else:
        upstream_path = normalize_upstream_path(request.url.path)
        if is_gemini_native_path(upstream_path):
            upstream_path = replace_gemini_model_in_path(
                upstream_path, model if model != "-" else "",
            )
    # legacy（LiteLLM → 官方 OpenAI）在 store=false 时不持久化 item。客户端从别的
    # 供应商切过来时，历史里会带官方不存在的 id，回传直接 404。仅对该供应商清理，
    # 其它供应商保持原样（Teamo 等对未知 id 宽容）。
    if (
        runtime_vendor == "legacy"
        and source == "responses"
        and not legacy_audio_request
        and not count_tokens_route
        and not protocol_adapt
        and not anthropic_bridge
        and not responses_bridge
        and not is_audio_path(request.url.path)
        and not is_image_path(request.url.path)
        and not is_gemini_native_path(request.url.path)
    ):
        body, parsed_body = sanitize_stateless_responses_input(body, parsed_body)
    count_upstream = get_runtime_upstream(vendor="teamo") if count_tokens_route else None
    count_key = (vendor_api_key("teamo") or CLAUDE_GPT_KEY or "").strip() if count_tokens_route else ""
    if count_tokens_route and (not count_key or not (count_upstream.get("base_url") or "").strip()):
        await finish_request_usage(req_id, 503)
        _log_access(
            request,
            req_id=req_id,
            user_id=user_id,
            source=source,
            model=model,
            stream=False,
            status=503,
            started=started,
            extra="result=count_tokens_unconfigured",
        )
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {"type": "api_error", "message": "count_tokens upstream is not configured"},
            },
        )
    if count_tokens_route:
        headers = apply_upstream_auth(headers, api_key=count_key, teamo=True)
    else:
        headers = apply_upstream_auth(
            headers,
            api_key=(runtime.get("route_api_key") or "").strip() or vendor_resolved_api_key(runtime.get("code") or ""),
            teamo=bool(runtime.get("teamo")),
        )
    # DeepSeek 会按客户端声明的 Accept-Encoding 返回 br/zstd；服务端 httpx 缺少对应
    # 解码器时会静默返回压缩字节，SSE 被当成乱码。仅「deepseek 模型 + deepseek
    # 供应商」去掉该头，交由 httpx 按自身解码能力协商；其他组合保持客户端原始行为。
    if runtime_vendor == "deepseek" and is_deepseek_model(model) and not count_tokens_route:
        headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
    compression_extra = ""
    if compression and compression.get("tokens_saved"):
        compression_extra = (
            f"headroom_saved={compression['tokens_saved']} "
            f"ratio={compression.get('compression_ratio') or 0:.2f}"
        )
    if cursor_client:
        compression_extra = (compression_extra + " cursor=1").strip()
    if responses_bridge:
        compression_extra = (compression_extra + " resp_bridge=1").strip()
    if protocol_adapt:
        compression_extra = (compression_extra + f" proto={source}->{target_proto}").strip()
    if count_tokens_route:
        compression_extra = (compression_extra + " vendor=teamo count_tokens=1").strip()
        routed_from = (runtime.get("code") or "").strip()
        if routed_from and routed_from != "teamo":
            compression_extra += f" from_vendor={routed_from}"
    else:
        compression_extra = (compression_extra + f" vendor={runtime['code']}").strip()
        if runtime.get("routed"):
            compression_extra = (compression_extra + f" route={runtime.get('route_model')}").strip()
        if (runtime.get("route_api_key") or "").strip():
            compression_extra = (compression_extra + " route_key=1").strip()
    if injection_ids:
        compression_extra = (compression_extra + " inject=" + ",".join(str(item) for item in injection_ids)).strip()
    if rewrite_rule:
        compression_extra = (
            compression_extra
            + f" rewrite={rewrite_rule.get('from_model') or '-'}->{model}"
        ).strip()

    legacy_audio_request = audio_route or _is_gpt_audio_model(model)
    if count_tokens_route:
        upstream_base = (count_upstream.get("base_url") or "").rstrip("/")
    else:
        upstream_base = (
            LEGACY_UPSTREAM_BASE if legacy_audio_request
            else runtime["base_url"]
        )
    # DeepSeek 提供 Anthropic 兼容端点，挂在 /anthropic 前缀下。上游最终走
    # Anthropic 协议时直连该端点，避免打到只认 /v1/messages 之外路径的根地址。
    # 必须同时是「deepseek 模型 + deepseek 供应商」；其他组合（含把别的模型
    # 路由到 deepseek 供应商）保持原样，不追加前缀。
    deepseek_anthropic_route = bool(
        runtime_vendor == "deepseek"
        and is_deepseek_model(model)
        and upstream_path == "/v1/messages"
        and not count_tokens_route
        and not legacy_audio_request
        and not is_image_path(request.url.path)
        and not is_gemini_native_path(request.url.path)
    )
    # 单独留一个请求 base：计费仍使用供应商根地址，只有转发目标加 /anthropic。
    # 若供应商地址本身就配成 .../anthropic，不重复拼接。
    request_base = upstream_base
    if deepseek_anthropic_route and not upstream_base.rstrip("/").endswith("/anthropic"):
        request_base = f"{upstream_base.rstrip('/')}/anthropic"
    bind_agent_task(
        account=str(user_id or ""),
        model="" if model == "-" else str(model),
        source=source,
        path=request.url.path,
        session_key=extract_session_key(parsed_body),
    )
    target_url = f"{request_base}{upstream_path}"
    # Anthropic/Responses 桥接后不要把原 query 原样带给目标端点
    if request.url.query and not anthropic_bridge and not responses_bridge and not protocol_adapt:
        target_url += f"?{request.url.query}"
    process_log.info(
        "forward req=%s user=%s url=%s source=%s model=%s upstream_model=%s bridge=%s resp_bridge=%s cursor=%s keys=%s",
        req_id,
        user_id,
        target_url,
        source,
        model,
        upstream_model,
        int(anthropic_bridge),
        int(responses_bridge),
        int(cursor_client),
        summarize_request_keys(parsed_body),
    )

    client: httpx.AsyncClient = (
        request.app.state.http_teamo if (runtime_teamo or count_tokens_route) else request.app.state.http
    )
    async def stream_response():
        collector = StreamUsageCollector()
        billed = False
        billing = None
        client_gone = False
        billing_handed_off = False
        saw_terminator = False
        terminator_names: list[str] = []
        heartbeats = 0
        sse_buf = b""
        upstream_status = 0
        first_sse_at: Optional[float] = None
        upstream_headers_at: Optional[float] = None
        first_upstream_event_at: Optional[float] = None
        deepseek_slow_logged = False
        deepseek_ttfb_evaluated = False
        def make_bridge_stream():
            client_model = model if model != "-" else ""
            if anthropic_bridge:
                return OpenAIToAnthropicStream(client_model=client_model)
            if responses_bridge:
                return ResponsesToChatStream(client_model=client_model)
            if protocol_response:
                return ProtocolStreamBridge(target_proto, source, client_model)
            return None

        bridge_stream = make_bridge_stream()

        def mark_first_sse():
            nonlocal first_sse_at
            if first_sse_at is None:
                first_sse_at = time.perf_counter()

        def current_ttfb_ms():
            if first_sse_at is None:
                return None
            return _elapsed_ms(started, first_sse_at)

        def note_deepseek_slow_ttfb(stage: str, observed_at: Optional[float] = None):
            """DeepSeek 上游首字超过阈值时单独留档，便于事后按供应商分析。

            observed_at 是判定用的时间点：正常是首个上游 SSE 事件；若上游始终
            没有事件（404 / 空流 / 客户端先断开），用当前时间兜底，同样能留档。
            每个请求只判定一次：首事件一旦到达就定格，避免把总耗时误当首字耗时。
            """
            nonlocal deepseek_slow_logged, deepseek_ttfb_evaluated
            if (
                deepseek_ttfb_evaluated
                or runtime_vendor != "deepseek"
                or not is_deepseek_model(model)
            ):
                return
            at = observed_at if observed_at is not None else first_upstream_event_at
            if at is None:
                return
            deepseek_ttfb_evaluated = True
            if _elapsed_ms(started, at) < DEEPSEEK_SLOW_TTFB_MS:
                return
            deepseek_slow_logged = True
            _log_deepseek_slow(
                request=request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                upstream_model=upstream_model,
                upstream_base=request_base,
                target_url=target_url,
                stage=stage,
                started=started,
                first_event_at=at,
                headers_at=upstream_headers_at,
                body_len=len(body or b""),
                heartbeats=heartbeats,
                headers=headers,
                parsed_body=parsed_body,
            )

        def note_terminator(event_text: str, obj: Optional[dict] = None) -> str:
            nonlocal saw_terminator
            saw_terminator = True
            name = "[DONE]"
            if obj and obj.get("type"):
                name = str(obj.get("type"))
            else:
                for line in event_text.splitlines():
                    if line.strip() in {"data: [DONE]", "data:[DONE]"}:
                        name = "[DONE]"
                        break
            status = responses_completion_status(obj)
            if status:
                name = f"{name}:{status}"
            terminator_names.append(name)
            return name

        async def emit_downstream(event_text: str, obj: Optional[dict] = None, usage: Optional[dict] = None):
            """立刻下发事件；Responses 补齐 event: 行，并在 completed 后补 [DONE]。"""
            nonlocal client_events
            text = event_text
            if source == "responses":
                text = ensure_sse_event_name(text, obj)
            client_events += 1
            mark_first_sse()
            if is_stream_terminator(text, obj):
                note_terminator(text, obj)
                status = responses_completion_status(obj)
                if status and status != "completed":
                    process_log.warning(
                        "responses terminal not completed req=%s status=%s event_bytes=%s",
                        req_id,
                        status,
                        len(text),
                    )
                elif obj and obj.get("type") == "response.completed":
                    process_log.info(
                        "responses completed forwarded req=%s status=%s event_bytes=%s has_event_line=%s",
                        req_id,
                        status or "-",
                        len(text),
                        int(any(line.startswith("event:") for line in text.splitlines())),
                    )
            if not client_gone:
                yield text.encode("utf-8")
            # 部分 Responses 客户端在 response.completed 后仍等 [DONE]
            if (
                source == "responses"
                and obj
                and obj.get("type") == "response.completed"
                and not any(name == "[DONE]" or name.startswith("[DONE]") for name in terminator_names)
            ):
                done = "data: [DONE]\n\n"
                note_terminator(done, None)
                client_events += 1
                if not client_gone:
                    yield done.encode("utf-8")

        async def drain_upstream_for_billing(
            resp, leftover: bytes = b"", *, terminal_forwarded: bool = False, vendor_prices_only: bool = False,
        ):
            """页面已停，后台继续读上游到结束，再按 usage 扣费。断连时只用当前供应商价目。"""
            nonlocal sse_buf, upstream_bytes
            sse_buf = leftover or b""
            try:
                if not terminal_forwarded:
                    async for chunk in _iter_upstream_with_heartbeat(resp):
                        if chunk is None:
                            continue
                        upstream_bytes += len(chunk or b"")
                        sse_buf += chunk or b""
                        events, sse_buf = split_sse_events_bytes(sse_buf)
                        for event_raw in events:
                            try:
                                event_text = event_raw.decode("utf-8")
                            except UnicodeDecodeError:
                                event_text = event_raw.decode("utf-8", errors="replace")
                            obj, _usage = handle_event(event_text)
                            if (
                                source == "responses"
                                and obj
                                and obj.get("type") in {"response.completed", "response.incomplete"}
                            ):
                                terminal_forwarded = True
                                break
                        if terminal_forwarded:
                            break
                if sse_buf.strip():
                    try:
                        leftover_text = sse_buf.decode("utf-8")
                    except UnicodeDecodeError:
                        leftover_text = sse_buf.decode("utf-8", errors="replace")
                    handle_event(leftover_text)
                leftover_usage = collector.flush() or collector.usage
                if vendor_prices_only:
                    if usage_has_billable_tokens(leftover_usage):
                        await charge_once(leftover_usage, vendor_prices_only=True)
                elif leftover_usage or (runtime_teamo and collector.request_id):
                    await charge_once(leftover_usage or {})
                elif is_billable_path(request.url.path):
                    process_log.warning("no usage after client stop req=%s user=%s", req_id, user_id)
            except Exception:
                process_log.exception("drain upstream for billing failed req=%s", req_id)
                leftover_usage = collector.flush() or collector.usage
                try:
                    if vendor_prices_only:
                        if usage_has_billable_tokens(leftover_usage):
                            await charge_once(leftover_usage, vendor_prices_only=True)
                    elif leftover_usage or (runtime_teamo and collector.request_id):
                        await charge_once(leftover_usage or {})
                except Exception:
                    process_log.exception("fallback charge after client stop failed req=%s", req_id)

        last_finish = ""
        last_result = ""
        client_events = 0
        upstream_bytes = 0

        def note_completion(obj: Optional[dict]):
            nonlocal last_finish, last_result
            meta = extract_completion_meta(obj)
            if meta.get("finish"):
                last_finish = meta["finish"]
            if meta.get("result"):
                last_result = meta["result"]

        def access_stream(*, status: int, extra: str = ""):
            done_at = time.perf_counter()
            # 上游始终没吐事件（含 404 / 空流 / 客户端先走）时，用结束时刻兜底判定。
            note_deepseek_slow_ttfb("stream_end", done_at)
            parts = []
            inferred = last_result
            if not inferred:
                joined = ",".join(terminator_names)
                if any(name.startswith("response.incomplete") or ":incomplete" in name for name in terminator_names):
                    inferred = "incomplete"
                elif any("completed" in name or name in {"[DONE]", "message_stop"} for name in terminator_names):
                    inferred = "completed"
                elif extra.startswith("result="):
                    inferred = ""
                elif joined:
                    inferred = "ended"
            if extra:
                parts.append(extra)
            if heartbeats:
                parts.append(f"heartbeats={heartbeats}")
            if terminator_names:
                parts.append("term=" + ",".join(terminator_names[-3:]))
            parts.append(f"events={client_events}")
            if upstream_bytes:
                parts.append(f"bytes={upstream_bytes}")
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                stream=True,
                status=status,
                started=started,
                base_url=upstream_base,
                ttfb_ms=_elapsed_ms(started, first_sse_at) if first_sse_at is not None else None,
                done_ms=_elapsed_ms(started, done_at),
                extra=" ".join(parts),
                usage=collector.usage,
                billing=billing,
                finish=last_finish,
                result=inferred,
            )

        async def _iter_upstream_with_heartbeat(resp):
            """读取上游 SSE；空闲时发注释心跳，避免中间层误判连接超时。"""
            iterator = resp.aiter_bytes().__aiter__()
            pending = None
            try:
                while True:
                    if pending is None:
                        pending = asyncio.create_task(iterator.__anext__())
                    try:
                        chunk = await asyncio.wait_for(
                            asyncio.shield(pending), timeout=15.0
                        )
                    except asyncio.TimeoutError:
                        # SSE comment 对协议客户端不可见，但可保持代理/网关连接活跃。
                        yield None
                        continue
                    except StopAsyncIteration:
                        break
                    pending = None
                    yield chunk
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

        async def charge_once(usage: dict, *, vendor_prices_only: bool = False):
            nonlocal billed, billing
            if billed or billing is not None:
                return
            if vendor_prices_only:
                if not usage_has_billable_tokens(usage):
                    return
                if await usage_is_billed(req_id):
                    billed = True
                    return
            elif not usage and not (runtime_teamo and collector.request_id):
                return
            # 扣成之后才标记，避免客户端断开取消协程后，补扣被跳过。
            summary = await _charge_if_needed(
                usage or {},
                parsed_body,
                user_id,
                source,
                request.url.path,
                req_id,
                status_code=upstream_status or 200,
                use_teamo_prices=not legacy_audio_request and runtime_teamo,
                vendor="" if legacy_audio_request else runtime_vendor,
                upstream_request_id="" if vendor_prices_only else collector.request_id,
                http_client=None if vendor_prices_only else client,
                vendor_base_url="" if legacy_audio_request else upstream_base,
                started=started,
                first_byte_at=first_sse_at,
                skip_upstream_bill=vendor_prices_only,
            )
            if summary is None:
                return
            billed = True
            billing = summary
            max_idx = collector.max_block_index
            if bridge_stream is not None:
                max_idx = max(max_idx, bridge_stream.max_block_index)
            billing["next_block_index"] = max_idx + 1
            billing.setdefault("model", model)
            _merge_compression(billing, compression)

        def schedule_billing_drain(resp, leftover: bytes = b"", *, terminal_forwarded: bool = False):
            """下游已停：脱离请求取消域，按当前供应商价目把已有 usage 扣掉。"""
            nonlocal billing_handed_off, client_gone, billed
            if billing_handed_off:
                return
            if billing is None:
                billed = False
            billing_handed_off = True
            client_gone = True
            process_log.info(
                "client disconnected, continue billing in background req=%s billed=%s bytes=%s events=%s",
                req_id,
                int(billed),
                upstream_bytes,
                client_events,
            )
            tasks = getattr(request.app.state, "bg_billing_tasks", None)
            if tasks is None:
                request.app.state.bg_billing_tasks = set()
                tasks = request.app.state.bg_billing_tasks

            async def _run():
                import anyio
                with anyio.CancelScope(shield=True):
                    try:
                        # 用量已经齐了就不要再读上游，避免卡在官方账单上被取消。
                        if billing is None and not usage_has_billable_tokens(collector.usage) and resp is not None:
                            await drain_upstream_for_billing(
                                resp,
                                leftover=leftover,
                                terminal_forwarded=terminal_forwarded,
                                vendor_prices_only=True,
                            )
                        if billing is None:
                            leftover_usage = collector.flush() or collector.usage
                            if usage_has_billable_tokens(leftover_usage):
                                await charge_once(leftover_usage, vendor_prices_only=True)
                        access_stream(status=upstream_status or 200, extra="result=client_stop")
                    except Exception:
                        process_log.exception("background billing after client stop failed req=%s", req_id)
                    finally:
                        await release_upstream_stream(stream_cm, resp)
                        await finish_request_usage(
                            req_id, upstream_status or 200, collector.usage, ttfb_ms=current_ttfb_ms(),
                        )

            task = asyncio.create_task(_run())
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def handle_event(event_text: str):
            nonlocal first_upstream_event_at
            if first_upstream_event_at is None:
                first_upstream_event_at = time.perf_counter()
                note_deepseek_slow_ttfb("first_upstream_event")
            obj = parse_sse_block(event_text)
            if obj:
                collector.note_block_index(obj)
                collector.note_request_id(obj)
            usage = extract_usage(obj) if obj else {}
            if not usage and obj and is_embedding_path(request.url.path):
                usage = extract_embedding_usage(obj)
            if not usage and obj:
                usage = extract_image_usage(obj)
            if usage:
                collector.usage = usage
            if obj:
                note_completion(obj)
            return obj, usage

        try:
            for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                retry_stream = False
                client_events = 0
                upstream_bytes = 0
                terminal_forwarded = False
                try:
                    # 上游首包可能很慢（本例曾 58s）。建连等待期间先给客户端心跳，避免 60s 客户端超时。
                    stream_cm = client.stream(
                        method=request.method,
                        url=target_url,
                        headers=headers,
                        content=body,
                    )
                    enter_task = asyncio.create_task(stream_cm.__aenter__())
                    resp = None
                    stream_released = False

                    async def release_once():
                        nonlocal stream_released
                        if stream_released or billing_handed_off:
                            return
                        stream_released = True
                        await release_upstream_stream(stream_cm, resp)

                    try:
                        while True:
                            try:
                                resp = await asyncio.wait_for(
                                    asyncio.shield(enter_task), timeout=10.0
                                )
                                break
                            except asyncio.TimeoutError:
                                heartbeats += 1
                                mark_first_sse()
                                yield b": keep-alive\n\n"
                        assert resp is not None
                        upstream_headers_at = time.perf_counter()
                        try:
                            collector.note_request_id(headers=dict(resp.headers))
                            upstream_status = resp.status_code
                            content_type = (resp.headers.get("content-type") or "").lower()
                            process_log.info(
                                "upstream stream req=%s status=%s attempt=%s ctype=%s",
                                req_id,
                                resp.status_code,
                                attempt,
                                content_type or "-",
                            )
                            if (
                                resp.status_code in UPSTREAM_RETRY_STATUSES
                                and attempt < UPSTREAM_RETRY_ATTEMPTS
                            ):
                                err_body = await resp.aread()
                                process_log.warning(
                                    "upstream retryable status req=%s status=%s attempt=%s/%s sleep=%ss body=%s",
                                    req_id,
                                    resp.status_code,
                                    attempt,
                                    UPSTREAM_RETRY_ATTEMPTS,
                                    UPSTREAM_RETRY_SLEEP,
                                    (err_body or b"")[:300].decode(errors="ignore").replace("\n", " "),
                                )
                                retry_stream = True
                            else:
                                if attempt > 1 and resp.status_code not in UPSTREAM_RETRY_STATUSES:
                                    process_log.info(
                                        "upstream stream retry ok req=%s attempt=%s status=%s",
                                        req_id,
                                        attempt,
                                        resp.status_code,
                                    )
                                # 上游非 2xx：按客户端协议吐错误，避免 Claude Code 收到空流
                                if resp.status_code >= 400:
                                    await finish_request_usage(req_id, resp.status_code)
                                    err_body = await resp.aread()
                                    err_text = (err_body or b"").decode(errors="ignore")
                                    process_log.warning(
                                        "upstream error body req=%s status=%s body=%s",
                                        req_id,
                                        resp.status_code,
                                        err_text[:800].replace("\n", " "),
                                    )
                                    err_msg = _extract_upstream_error_message(err_text, resp.status_code)
                                    for chunk in _stream_error_chunks(source, err_msg, resp.status_code):
                                        client_events += 1
                                        mark_first_sse()
                                        yield chunk
                                    access_stream(status=resp.status_code, extra="result=upstream_error")
                                    return

                                # 声明 stream 却回 JSON：常见于供应商不支持 beta/1m 时直接落错误体
                                if "text/event-stream" not in content_type and "json" in content_type:
                                    raw = await resp.aread()
                                    upstream_bytes = len(raw or b"")
                                    err_text = (raw or b"").decode(errors="ignore")
                                    err_msg = _extract_upstream_error_message(err_text, resp.status_code)
                                    process_log.warning(
                                        "upstream non-sse body on stream req=%s status=%s body=%s",
                                        req_id,
                                        resp.status_code,
                                        err_text[:800].replace("\n", " "),
                                    )
                                    if attempt < UPSTREAM_RETRY_ATTEMPTS:
                                        retry_stream = True
                                    else:
                                        for chunk in _stream_error_chunks(source, err_msg, resp.status_code or 502):
                                            mark_first_sse()
                                            yield chunk
                                        access_stream(status=resp.status_code or 502, extra="result=upstream_non_sse")
                                        return
                                else:
                                    sse_buf = b""
                                    terminal_forwarded = False
                                    async for chunk in _iter_upstream_with_heartbeat(resp):
                                        if chunk is None:
                                            # 必须是真实换行。字面 \\n 会把下一帧 data: 粘进注释行。
                                            # 仅在半包缓冲为空时发送，避免插入完整事件中间。
                                            if sse_buf:
                                                continue
                                            heartbeats += 1
                                            mark_first_sse()
                                            yield b": keep-alive\n\n"
                                            continue
                                        upstream_bytes += len(chunk or b"")
                                        sse_buf += chunk or b""
                                        events, sse_buf = split_sse_events_bytes(sse_buf)
                                        for event_raw in events:
                                            try:
                                                event_text = event_raw.decode("utf-8")
                                            except UnicodeDecodeError:
                                                event_text = event_raw.decode("utf-8", errors="replace")
                                                process_log.warning(
                                                    "sse event utf8 replace req=%s bytes=%s",
                                                    req_id,
                                                    len(event_raw),
                                                )
                                            obj, usage = handle_event(event_text)
                                            if bridge_stream is not None:
                                                for converted in bridge_stream.feed_openai_event(obj):
                                                    conv_text = converted.decode("utf-8")
                                                    conv_obj = parse_sse_block(conv_text)
                                                    if conv_obj:
                                                        collector.note_block_index(conv_obj)
                                                        conv_usage = extract_usage(conv_obj)
                                                        if conv_usage:
                                                            usage = conv_usage
                                                            collector.usage = conv_usage
                                                    async for out in emit_downstream(conv_text, conv_obj, None):
                                                        yield out
                                                continue
                                            async for out in emit_downstream(event_text, obj, usage):
                                                yield out
                                            if (
                                                source == "responses"
                                                and obj
                                                and obj.get("type") in {"response.completed", "response.incomplete"}
                                            ):
                                                terminal_forwarded = True
                                                break
                                        if terminal_forwarded:
                                            # 结束帧已下发：立刻停读上游，避免再等 EOF / 再发心跳。
                                            break
                                    leftover = sse_buf
                                    sse_buf = b""
                                    if leftover.strip() and not terminal_forwarded:
                                        try:
                                            leftover_text = leftover.decode("utf-8")
                                        except UnicodeDecodeError:
                                            leftover_text = leftover.decode("utf-8", errors="replace")
                                        obj, usage = handle_event(leftover_text)
                                        if bridge_stream is not None:
                                            for converted in bridge_stream.feed_openai_event(obj):
                                                conv_text = converted.decode("utf-8")
                                                conv_obj = parse_sse_block(conv_text)
                                                if conv_obj:
                                                    collector.note_block_index(conv_obj)
                                                    conv_usage = extract_usage(conv_obj)
                                                    if conv_usage:
                                                        usage = conv_usage
                                                        collector.usage = conv_usage
                                                async for out in emit_downstream(conv_text, conv_obj, None):
                                                    yield out
                                        else:
                                            async for out in emit_downstream(leftover_text, obj, usage):
                                                yield out
                                    # 只有上游明确发送完成事件后，桥接器才能正常 finish。
                                    if (
                                        bridge_stream is not None
                                        and not bridge_stream._closed
                                        and saw_terminator
                                    ):
                                        for converted in bridge_stream.finish():
                                            conv_text = converted.decode("utf-8")
                                            conv_obj = parse_sse_block(conv_text)
                                            conv_usage = None
                                            if conv_obj:
                                                collector.note_block_index(conv_obj)
                                                conv_usage = extract_usage(conv_obj)
                                                if conv_usage:
                                                    collector.usage = conv_usage
                                            async for out in emit_downstream(conv_text, conv_obj, conv_usage):
                                                yield out

                                    # Gemini / Images 的正常结束常常就是上游关闭连接，
                                    # 不能再追加一条错误事件，否则客户端会把成功结果当成失败。
                                    if (
                                        client_events > 0
                                        and not saw_terminator
                                        and source in {"gemini", "images"}
                                    ):
                                        process_log.info(
                                            "stream eof passthrough req=%s source=%s status=%s bytes=%s events=%s",
                                            req_id,
                                            source,
                                            resp.status_code,
                                            upstream_bytes,
                                            client_events,
                                        )
                                        saw_terminator = True

                                    # 上游 200 但未发送终止事件：不能把半截流当成成功。
                                    if client_events > 0 and not saw_terminator:
                                        incomplete_msg = (
                                            "upstream stream ended before completion "
                                            f"(status={resp.status_code}, bytes={upstream_bytes}); "
                                            "partial response was preserved; retry with continue"
                                        )
                                        process_log.warning(
                                            "upstream incomplete stream req=%s status=%s bytes=%s events=%s attempt=%s/%s",
                                            req_id,
                                            resp.status_code,
                                            upstream_bytes,
                                            client_events,
                                            attempt,
                                            UPSTREAM_RETRY_ATTEMPTS,
                                        )
                                        for chunk in _stream_error_chunks(source, incomplete_msg, 502):
                                            mark_first_sse()
                                            yield chunk
                                        if not billed:
                                            leftover_usage = collector.flush() or collector.usage
                                            if leftover_usage:
                                                await charge_once(leftover_usage)
                                        access_stream(status=502, extra="result=incomplete_stream")
                                        return

                                    # 上游 200 但零事件：1M/长上下文时供应商常见空关连接
                                    if client_events == 0 and not saw_terminator:
                                        process_log.warning(
                                            "upstream empty stream req=%s status=%s bytes=%s attempt=%s/%s",
                                            req_id,
                                            resp.status_code,
                                            upstream_bytes,
                                            attempt,
                                            UPSTREAM_RETRY_ATTEMPTS,
                                        )
                                        if attempt < UPSTREAM_RETRY_ATTEMPTS:
                                            retry_stream = True
                                        else:
                                            empty_msg = (
                                                "upstream stream ended without events "
                                                f"(status={resp.status_code}, bytes={upstream_bytes}); "
                                                "often caused by unstable long-context/1M upstream"
                                            )
                                            for chunk in _stream_error_chunks(source, empty_msg, 502):
                                                mark_first_sse()
                                                yield chunk
                                            access_stream(status=502, extra="result=empty_stream")
                                            return

                                    if not billed:
                                        leftover_usage = collector.flush() or collector.usage
                                        if leftover_usage or (runtime_teamo and collector.request_id):
                                            await charge_once(leftover_usage or {})
                                        elif is_billable_path(request.url.path):
                                            process_log.warning("no usage in stream req=%s user=%s", req_id, user_id)
                                    if billing:
                                        max_idx = collector.max_block_index
                                        if bridge_stream is not None:
                                            max_idx = max(max_idx, bridge_stream.max_block_index)
                                        billing["next_block_index"] = max(max_idx + 1, 1)
                                    process_log.info(
                                        "stream closed req=%s status=%s bytes=%s events=%s heartbeats=%s terminators=%s",
                                        req_id,
                                        resp.status_code,
                                        upstream_bytes,
                                        client_events,
                                        heartbeats,
                                        ",".join(terminator_names) or "-",
                                    )
                                    if not retry_stream:
                                        break
                        except BaseException as exc:
                            if is_client_disconnect(exc):
                                schedule_billing_drain(
                                    resp, leftover=sse_buf, terminal_forwarded=terminal_forwarded
                                )
                                return
                            raise
                        finally:
                            await release_once()
                    except BaseException as exc:
                        if is_client_disconnect(exc) and resp is not None:
                            schedule_billing_drain(
                                resp, leftover=sse_buf, terminal_forwarded=terminal_forwarded
                            )
                            return
                        if not enter_task.done():
                            enter_task.cancel()
                            await asyncio.gather(enter_task, return_exceptions=True)
                        await release_once()
                        if is_client_disconnect(exc):
                            client_gone = True
                            access_stream(status=499, extra="result=client_stop")
                            return
                        raise
                except httpx.HTTPError as exc:
                    process_log.warning(
                        "upstream stream error req=%s attempt=%s/%s bytes=%s events=%s err=%s",
                        req_id,
                        attempt,
                        UPSTREAM_RETRY_ATTEMPTS,
                        upstream_bytes,
                        client_events,
                        format_upstream_exc(exc),
                    )
                    # 已经向客户端输出过内容时，不能重试整个请求：重试可能重复文本，
                    # 更严重的是可能重复执行 agent 的工具调用。明确结束为不完整流，
                    # 让客户端基于已有上下文 continue。
                    if client_events > 0:
                        incomplete_msg = (
                            "upstream stream interrupted before completion "
                            f"(bytes={upstream_bytes}, events={client_events}); "
                            "partial response was preserved; retry with continue"
                        )
                        for chunk in _stream_error_chunks(source, incomplete_msg, 502):
                            mark_first_sse()
                            yield chunk
                        if not billed:
                            leftover_usage = collector.flush() or collector.usage
                            if leftover_usage:
                                await charge_once(leftover_usage)
                        access_stream(status=502, extra="result=incomplete_stream_error")
                        return
                    if not should_retry_stream_transport(exc, attempt, client_events):
                        raise
                    retry_stream = True

                if retry_stream:
                    # 重置桥接器，避免半包状态污染下一轮
                    bridge_stream = make_bridge_stream()
                    sse_buf = b""
                    saw_terminator = False
                    terminator_names = []
                    heartbeats = 0
                    upstream_headers_at = None
                    first_upstream_event_at = None
                    await asyncio.sleep(UPSTREAM_RETRY_SLEEP)
                    continue
                break
        except Exception as exc:
            await finish_request_usage(req_id, 502, collector.usage, ttfb_ms=current_ttfb_ms())
            process_log.exception("stream error req=%s user=%s", req_id, user_id)
            # 不要裸 raise：Claude Code 会看到空流并报 Stream ended without receiving any events
            for chunk in _stream_error_chunks(
                source,
                f"upstream stream failed: {exc}",
                upstream_status or 502,
            ):
                mark_first_sse()
                yield chunk
            access_stream(status=upstream_status or 502, extra="result=stream_error")
            return
        finally:
            if not billing_handed_off:
                await finish_request_usage(
                    req_id, upstream_status or 502, collector.usage, ttfb_ms=current_ttfb_ms(),
                )
        if upstream_status < 400:
            access_stream(status=upstream_status or 200, extra=compression_extra)

    if want_stream:
        process_log.info("handle stream req=%s source=%s", req_id, source)
        return StreamingResponse(
            stream_response(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    try:
        resp = await _request_upstream_with_retry(
            client,
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
            req_id=req_id,
        )
        process_log.info("upstream req=%s status=%s", req_id, resp.status_code)
        upstream_at = time.perf_counter()
        if runtime_vendor == "deepseek" and is_deepseek_model(model):
            # 非流式同样记录慢首包，判定点是上游返回整个响应体的时刻。
            _log_deepseek_slow(
                request=request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                upstream_model=upstream_model,
                upstream_base=request_base,
                target_url=target_url,
                stage="non_stream",
                started=started,
                first_event_at=upstream_at,
                headers_at=upstream_at,
                body_len=len(body or b""),
                heartbeats=0,
                headers=headers,
                parsed_body=parsed_body,
            )
        parsed_resp = _parse_json_body(resp.content)
        if resp.status_code >= 400:
            process_log.warning(
                "upstream error body req=%s status=%s body=%s keys=%s",
                req_id,
                resp.status_code,
                (resp.content or b"")[:800].decode(errors="ignore").replace("\n", " "),
                summarize_request_keys(parsed_body),
            )
        if anthropic_bridge and resp.status_code >= 400:
            await finish_request_usage(req_id, resp.status_code)
            err = (parsed_resp or {}).get("error") if isinstance(parsed_resp, dict) else None
            if not isinstance(err, dict):
                err = {
                    "type": "api_error",
                    "message": (resp.content or b"upstream error").decode(errors="ignore")[:500],
                }
            anthropic_err = {
                "type": "error",
                "error": {
                    "type": err.get("type") or err.get("code") or "api_error",
                    "message": err.get("message") or "upstream error",
                },
            }
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                stream=False,
                status=resp.status_code,
                started=started,
                base_url=upstream_base,
                done_ms=_elapsed_ms(started),
                extra="result=upstream_error",
                usage=extract_usage(parsed_resp) if parsed_resp else None,
                result="error",
            )
            return JSONResponse(status_code=resp.status_code, content=anthropic_err)

        if anthropic_bridge and resp.status_code < 400 and parsed_resp:
            # 先按 OpenAI usage 计费，再把响应还原成 Anthropic
            usage = extract_usage(parsed_resp)
            billing = await _charge_if_needed(
                usage,
                parsed_body,
                user_id,
                source,
                request.url.path,
                req_id,
                status_code=resp.status_code,
                use_teamo_prices=not legacy_audio_request and runtime_teamo,
                vendor="" if legacy_audio_request else runtime_vendor,
                upstream_request_id=extract_teamo_request_id(parsed_resp, dict(resp.headers)),
                http_client=client,
                vendor_base_url="" if legacy_audio_request else upstream_base,
                started=started,
                first_byte_at=upstream_at,
            )
            await finish_request_usage(req_id, resp.status_code, usage)
            parsed_resp = openai_completion_to_anthropic(
                parsed_resp, client_model=model if model != "-" else ""
            )
            out_content = json.dumps(parsed_resp, ensure_ascii=False).encode("utf-8")
            if billing:
                billing.setdefault("model", model)
                _merge_compression(billing, compression)
            meta = extract_completion_meta(parsed_resp)
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                stream=False,
                status=resp.status_code,
                started=started,
                base_url=upstream_base,
                done_ms=_elapsed_ms(started),
                extra=compression_extra,
                usage=usage,
                billing=billing,
                finish=meta.get("finish") or "",
                result=meta.get("result") or "completed",
            )
            return Response(
                content=out_content,
                status_code=resp.status_code,
                headers=sanitize_headers(dict(resp.headers)),
                media_type="application/json",
            )

        if protocol_response and isinstance(parsed_resp, dict):
            if resp.status_code >= 400:
                await finish_request_usage(req_id, resp.status_code)
                _log_access(
                    request,
                    req_id=req_id,
                    user_id=user_id,
                    source=source,
                    model=model,
                    stream=False,
                    status=resp.status_code,
                    started=started,
                    base_url=upstream_base,
                    done_ms=_elapsed_ms(started),
                    extra="result=upstream_error",
                    usage=extract_usage(parsed_resp),
                    result="error",
                )
                return JSONResponse(
                    status_code=resp.status_code,
                    content=convert_upstream_error(parsed_resp, source),
                )
            usage = extract_usage(parsed_resp)
            billing = await _charge_if_needed(
                usage,
                parsed_body,
                user_id,
                source,
                request.url.path,
                req_id,
                status_code=resp.status_code,
                use_teamo_prices=not legacy_audio_request and runtime_teamo,
                vendor="" if legacy_audio_request else runtime_vendor,
                upstream_request_id=extract_teamo_request_id(parsed_resp, dict(resp.headers)),
                http_client=client,
                vendor_base_url="" if legacy_audio_request else upstream_base,
                started=started,
                first_byte_at=upstream_at,
            )
            await finish_request_usage(req_id, resp.status_code, usage)
            parsed_resp = convert_upstream_response(
                parsed_resp,
                target_proto,
                source,
                model if model != "-" else "",
            )
            out_content = json.dumps(parsed_resp, ensure_ascii=False).encode("utf-8")
            if billing:
                billing.setdefault("model", model)
                _merge_compression(billing, compression)
            meta = extract_completion_meta(parsed_resp)
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                stream=False,
                status=resp.status_code,
                started=started,
                base_url=upstream_base,
                done_ms=_elapsed_ms(started),
                extra=compression_extra,
                usage=usage,
                billing=billing,
                finish=meta.get("finish") or "",
                result=meta.get("result") or "completed",
            )
            return Response(
                content=out_content,
                status_code=resp.status_code,
                headers=sanitize_headers(dict(resp.headers)),
                media_type="application/json",
            )

        if responses_bridge and resp.status_code < 400 and parsed_resp:
            usage = extract_usage(parsed_resp)
            billing = await _charge_if_needed(
                usage,
                parsed_body,
                user_id,
                source,
                request.url.path,
                req_id,
                status_code=resp.status_code,
                use_teamo_prices=not legacy_audio_request and runtime_teamo,
                vendor="" if legacy_audio_request else runtime_vendor,
                upstream_request_id=extract_teamo_request_id(parsed_resp, dict(resp.headers)),
                http_client=client,
                vendor_base_url="" if legacy_audio_request else upstream_base,
                started=started,
                first_byte_at=upstream_at,
            )
            await finish_request_usage(req_id, resp.status_code, usage)
            parsed_resp = responses_to_chat_completion(
                parsed_resp, client_model=model if model != "-" else ""
            )
            out_content = json.dumps(parsed_resp, ensure_ascii=False).encode("utf-8")
            if billing:
                billing.setdefault("model", model)
                _merge_compression(billing, compression)
            meta = extract_completion_meta(parsed_resp)
            _log_access(
                request,
                req_id=req_id,
                user_id=user_id,
                source=source,
                model=model,
                stream=False,
                status=resp.status_code,
                started=started,
                base_url=upstream_base,
                done_ms=_elapsed_ms(started),
                extra=compression_extra,
                usage=usage,
                billing=billing,
                finish=meta.get("finish") or "",
                result=meta.get("result") or "completed",
            )
            return Response(
                content=out_content,
                status_code=resp.status_code,
                headers=sanitize_headers(dict(resp.headers)),
                media_type="application/json",
            )

        if is_image_path(request.url.path) or is_gemini_native_path(request.url.path):
            usage = extract_image_usage(parsed_resp)
        elif is_embedding_path(request.url.path):
            usage = extract_embedding_usage(parsed_resp)
        else:
            usage = extract_usage(parsed_resp)
        billing = await _charge_if_needed(
            usage,
            parsed_body,
            user_id,
            source,
            request.url.path,
            req_id,
            status_code=resp.status_code,
            use_teamo_prices=not legacy_audio_request and runtime_teamo,
            vendor="" if legacy_audio_request else runtime_vendor,
            upstream_request_id=extract_teamo_request_id(parsed_resp, dict(resp.headers)),
            http_client=client,
            vendor_base_url="" if legacy_audio_request else upstream_base,
            started=started,
            first_byte_at=upstream_at,
        )
        await finish_request_usage(req_id, resp.status_code, usage)
        out_content = resp.content
        if is_models_path(request.url.path) and parsed_resp:
            parsed_resp = apply_aliases_to_models_payload(
                apply_protocol_to_models_payload(rewrite_models_payload(parsed_resp))
            )
            out_content = json.dumps(parsed_resp, ensure_ascii=False).encode("utf-8")
        if billing:
            billing.setdefault("model", model)
            _merge_compression(billing, compression)
        meta = extract_completion_meta(parsed_resp)
        _log_access(
            request,
            req_id=req_id,
            user_id=user_id,
            source=source,
            model=model,
            stream=False,
            status=resp.status_code,
            started=started,
            base_url=upstream_base,
            done_ms=_elapsed_ms(started),
            extra=compression_extra,
            usage=usage,
            billing=billing,
            finish=meta.get("finish") or "",
            result=meta.get("result") or ("error" if resp.status_code >= 400 else "completed"),
        )
        return Response(
            content=out_content,
            status_code=resp.status_code,
            headers=sanitize_headers(dict(resp.headers)),
        )
    except Exception:
        await finish_request_usage(req_id, 502)
        process_log.exception("forward error req=%s user=%s", req_id, user_id)
        _log_access(
            request,
            req_id=req_id,
            user_id=user_id,
            source=source,
            model=model,
            stream=False,
            status=502,
            started=started,
            base_url=upstream_base,
            done_ms=_elapsed_ms(started),
            extra="result=upstream_failed",
        )
        return Response(content=b"Upstream request failed", status_code=502)


class PricingSettingsUpdate(BaseModel):
    usd_cny_rate: float = Field(gt=0, le=100)
    profit_multiplier: float = Field(gt=0, le=100)


class VendorUpsert(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    pricing_url: Optional[str] = Field(default=None, max_length=512)
    pricing_mode: str = Field(default="manual", max_length=32)  # manual | catalog_rate
    cost_rate: Optional[float] = Field(default=None, gt=0, le=100)
    catalog_policy: Optional[dict] = None  # 对外价格档位 + 分类型倍率/展示
    profit_multiplier: Optional[float] = Field(default=None, gt=0, le=100)
    api_key: Optional[str] = Field(default=None, max_length=512)
    is_active: bool = True
    is_default: bool = False
    sort_order: int = 100
    reapply_catalog: bool = False  # catalog_rate 下强制按最新官方目录×倍率重写价目


class VendorModelPriceItem(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    service_tier: str = Field(default="standard", max_length=32)
    kind: str = Field(default="chat", max_length=32)
    display_name: Optional[str] = None
    owned_by: Optional[str] = None
    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cache_read: float = Field(default=0, ge=0)
    cache_write: float = Field(default=0, ge=0)
    text_input: float = Field(default=0, ge=0)
    image_input: float = Field(default=0, ge=0)
    # 可选原价：设置后 1:1 按原价计美元，人民币只乘汇率；不传则保留原值
    original_input: Optional[float] = Field(default=None, ge=0)
    original_output: Optional[float] = Field(default=None, ge=0)
    original_cache_read: Optional[float] = Field(default=None, ge=0)
    original_cache_write: Optional[float] = Field(default=None, ge=0)
    original_text_input: Optional[float] = Field(default=None, ge=0)
    original_image_input: Optional[float] = Field(default=None, ge=0)
    is_active: Optional[bool] = True


class VendorModelPriceUpsert(BaseModel):
    models: list[VendorModelPriceItem] = Field(min_length=1)


class ModelRouteUpsert(BaseModel):
    id: Optional[int] = None
    model: str = Field(min_length=1, max_length=255)
    vendor_code: str = Field(min_length=1, max_length=64)
    is_active: bool = True
    note: str = ""
    api_key: Optional[str] = Field(default=None, max_length=512)
    # 指定该 vendor 下的命名 Key；0 表示清除。与 api_key 互斥，设置了会清掉临时覆盖
    api_key_id: Optional[int] = None
    # 不传表示保持原名单；空列表表示全部用户
    user_ids: Optional[list[int]] = None


class ModelRouteKeyReplace(BaseModel):
    model: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=512)


class VendorKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=512)
    note: str = ""


class VendorKeyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=512)
    note: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None


class ModelRewriteUpsert(BaseModel):
    id: Optional[int] = None
    model: str = Field(min_length=1, max_length=255)
    target_model: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    note: str = ""
    # 不传表示保持原名单；空列表表示全部用户
    user_ids: Optional[list[int]] = None


class ModelProtocolUpsert(BaseModel):
    id: Optional[int] = None
    model: str = Field(min_length=1, max_length=255)
    protocol: str = Field(min_length=1, max_length=32)
    is_active: bool = True
    note: str = ""


class ModelAliasUpsert(BaseModel):
    id: Optional[int] = None
    alias: str = Field(min_length=1, max_length=255)
    target_model: str = Field(min_length=1, max_length=255)
    protocol: str = "auto"
    hide_target: bool = False
    is_active: bool = True
    note: str = ""


class PromptInjectionUpsert(BaseModel):
    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=80)
    scope: str = "category"
    target: str = ""
    content: str = Field(min_length=1, max_length=8000)
    position: str = "append"
    is_active: bool = True
    sort_order: int = Field(default=100, ge=0, le=10000)
    note: str = ""


class VendorSingleModelPriceUpdate(BaseModel):
    """单独改某个模型价；model 也可放路径参数里，body 里可省略。"""
    model: Optional[str] = Field(default=None, max_length=255)
    service_tier: str = Field(default="standard", max_length=32)
    kind: str = Field(default="chat", max_length=32)
    display_name: Optional[str] = None
    owned_by: Optional[str] = None
    input: float = Field(default=0, ge=0)
    output: float = Field(default=0, ge=0)
    cache_read: float = Field(default=0, ge=0)
    cache_write: float = Field(default=0, ge=0)
    text_input: float = Field(default=0, ge=0)
    image_input: float = Field(default=0, ge=0)
    # 可选原价（USD/1M）。>0 时 1:1 按原价计美元，人民币只乘汇率；传 0/null 清空
    original_input: Optional[float] = Field(default=None, ge=0)
    original_output: Optional[float] = Field(default=None, ge=0)
    original_cache_read: Optional[float] = Field(default=None, ge=0)
    original_cache_write: Optional[float] = Field(default=None, ge=0)
    original_text_input: Optional[float] = Field(default=None, ge=0)
    original_image_input: Optional[float] = Field(default=None, ge=0)
    is_active: Optional[bool] = True


async def _require_admin(request: Request):
    user, error = await _get_api_user(request)
    if error:
        return None, error
    if not user.is_staff:
        return None, JSONResponse(status_code=403, content={"error": "admin required"})
    return user, None


async def _get_api_user(request: Request):
    key = extract_user_key(dict(request.headers))
    if not key:
        return None, JSONResponse(status_code=401, content={"error": "missing API key"})
    async with AsyncSessionLocal() as db:
        user = await lookup_user_by_credential(db, key)
    if user is None:
        return None, JSONResponse(status_code=403, content={"error": "invalid API key"})
    return user, None


@app.get("/v1/vendors")
async def list_upstream_vendors(request: Request):
    """前端供应商切换器：返回可用 base_url / vendor 列表。"""
    user, error = await _get_api_user(request)
    if error:
        return error
    return {
        "current_vendor": active_vendor_code(),
        "vendors": list_vendors(),
    }


@app.get("/v1/pricing/models")
async def list_current_model_prices(
    request: Request,
    vendor: Optional[str] = Query(None, description="供应商 code，如 teamo / legacy / yunxin"),
    base_url: Optional[str] = Query(None, description="按上游 base_url 选择价目表"),
):
    """按供应商返回当前用户有效美元价 + 模型列表。"""
    user, error = await _get_api_user(request)
    if error:
        return error
    config = await get_pricing_config()
    user_factor = (user.money_rate or 1) * (user.discount or 1)
    # 价目一律美元；仅叠利润倍率与用户倍率，不再换算人民币。
    vendor_code = vendor if isinstance(vendor, str) else ""
    vendor_base = base_url if isinstance(base_url, str) else ""
    meta, catalog = get_vendor_catalog(vendor=vendor_code, base_url=vendor_base)
    if not meta:
        return JSONResponse(status_code=404, content={"error": "vendor not found"})
    if not vendor_code and not vendor_base:
        catalog, _route_overrides = overlay_catalog_rows(catalog, user_id=user.user_id)
        catalog = apply_aliases_to_catalog(catalog)
    else:
        catalog = apply_aliases_to_catalog(catalog)
    default_multiplier = vendor_profit_multiplier(meta["code"], config.profit_multiplier)

    def row_factor(row: dict) -> float:
        bill_vendor = row.get("bill_vendor") or row.get("route_vendor") or meta["code"]
        return vendor_profit_multiplier(bill_vendor, config.profit_multiplier) * user_factor

    models = []
    images = []
    for row in catalog:
        billing_factor = row_factor(row)
        if row.get("kind") == "image":
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            tiers = raw.get("per_image_tiers") if isinstance(raw.get("per_image_tiers"), dict) else None
            item = {
                "model": row["model"],
                "display_name": row.get("display_name") or row["model"],
                "owned_by": row.get("owned_by") or "",
                "service_tier": row.get("service_tier") or "standard",
                "billing": raw.get("billing") or ("per_image" if tiers else "token"),
                "unit": "USD per image" if tiers else "USD per 1M tokens",
                "text_input": float(row.get("text_input") or row.get("input") or 0) * billing_factor,
                "image_input": float(row.get("image_input") or 0) * billing_factor,
                "output": float(row.get("output") or 0) * billing_factor,
            }
            if tiers:
                item["per_image_tiers"] = {
                    str(k).lower(): float(v or 0) * billing_factor for k, v in tiers.items()
                }
            images.append(item)
            continue
        # 有原价：价目 1:1 展示原价美元（扣费只再乘汇率）；无原价：成本×加价×用户费率
        bill_prices, _cost_prices = resolve_billing_and_cost_prices(row)
        use_original = has_original_prices(row)
        price_factor = 1.0 if use_original else billing_factor
        models.append(_attach_long_context({
            "model": row["model"],
            "display_name": row.get("display_name") or row["model"],
            "owned_by": row.get("owned_by") or "",
            "service_tier": row.get("service_tier") or "standard",
            "unit": "USD per 1M tokens",
            "input": float(bill_prices.get("input") or 0) * price_factor,
            "output": float(bill_prices.get("output") or 0) * price_factor,
            "cache_read": float(bill_prices.get("cache_read") or 0) * price_factor,
            "cache_write": float(bill_prices.get("cache_write") or 0) * price_factor,
            "original_input": row.get("original_input"),
            "original_output": row.get("original_output"),
            "billing_basis": "original" if use_original else "cost",
        }))

    model_ids = sorted({item["model"] for item in models})
    return {
        "currency": "USD",
        "price_unit": "per 1M tokens",
        "vendor": {
            "code": meta["code"],
            "name": meta.get("name"),
            "base_url": meta.get("base_url"),
            "is_current": meta["code"] == active_vendor_code(),
            "last_synced_at": meta.get("last_synced_at"),
        },
        "usd_cny_rate": config.usd_cny_rate,
        "profit_multiplier": default_multiplier,
        "money_rate": user.money_rate or 1,
        "discount": user.discount or 1,
        "model_ids": model_ids,
        "models": models,
        "image_models": images,
    }


@app.get("/v1/admin/pricing")
async def read_global_pricing(request: Request):
    user, error = await _get_api_user(request)
    if error:
        return error
    if not user.is_staff:
        return JSONResponse(status_code=403, content={"error": "admin required"})
    config = await get_pricing_config()
    return {"usd_cny_rate": config.usd_cny_rate, "profit_multiplier": config.profit_multiplier}


@app.put("/v1/admin/pricing")
async def set_global_pricing(payload: PricingSettingsUpdate, request: Request):
    user, error = await _get_api_user(request)
    if error:
        return error
    if not user.is_staff:
        return JSONResponse(status_code=403, content={"error": "admin required"})
    async with AsyncSessionLocal() as db:
        config = await update_pricing_settings(
            db, usd_cny_rate=payload.usd_cny_rate,
            profit_multiplier=payload.profit_multiplier, updated_by=user.user_id,
        )
    return {"usd_cny_rate": config.usd_cny_rate, "profit_multiplier": config.profit_multiplier}


@app.get("/v1/admin/vendors")
async def admin_list_vendors(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    vendors = list_vendors(include_pricing_meta=True)
    if any(item.get("code") == "teamo" for item in vendors):
        balance = await fetch_teamo_balance(
            client=getattr(request.app.state, "http_teamo", None),
            vendor_base_url=next((item.get("base_url") or "" for item in vendors if item.get("code") == "teamo"), ""),
        )
        for item in vendors:
            if item.get("code") == "teamo":
                item["balance"] = {key: value for key, value in balance.items() if key != "_mono"}
    return {"current_vendor": active_vendor_code(), "vendors": vendors}


@app.put("/v1/admin/vendors")
async def admin_upsert_vendor(payload: VendorUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        vendor = await create_or_update_vendor(
            code=payload.code,
            name=payload.name,
            base_url=payload.base_url,
            pricing_url=payload.pricing_url,
            pricing_mode=payload.pricing_mode,
            cost_rate=payload.cost_rate,
            catalog_policy=payload.catalog_policy,
            profit_multiplier=payload.profit_multiplier,
            api_key=payload.api_key,
            is_active=payload.is_active,
            is_default=payload.is_default,
            sort_order=payload.sort_order,
            reapply_catalog=payload.reapply_catalog,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"vendor": vendor}


@app.get("/v1/admin/vendors/{code}/keys")
async def admin_list_vendor_keys(code: str, request: Request):
    """一个供应商下的多把命名 Key，只回掩码。"""
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        keys = await list_vendor_keys(code)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return {"keys": keys}


@app.post("/v1/admin/vendors/{code}/keys")
async def admin_create_vendor_key(code: str, payload: VendorKeyCreate, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        key = await create_vendor_key(
            code=code, name=payload.name, api_key=payload.api_key, note=payload.note,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "key": key, "keys": await list_vendor_keys(code)}


@app.put("/v1/admin/vendors/{code}/keys/{key_id}")
async def admin_update_vendor_key(code: str, key_id: int, payload: VendorKeyUpdate, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        key = await update_vendor_key(
            code=code, key_id=key_id, name=payload.name, api_key=payload.api_key,
            note=payload.note, is_active=payload.is_active, is_default=payload.is_default,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "key": key, "keys": await list_vendor_keys(code)}


@app.delete("/v1/admin/vendors/{code}/keys/{key_id}")
async def admin_delete_vendor_key(code: str, key_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        ok = await delete_vendor_key(code=code, key_id=key_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not ok:
        return JSONResponse(status_code=404, content={"error": "key not found"})
    return {"ok": True, "keys": await list_vendor_keys(code)}


@app.get("/v1/admin/pricing/catalog")
async def admin_list_official_catalog(
    request: Request,
    cost_rate: Optional[float] = Query(default=None, gt=0, le=100),
):
    """管理端：官方标准价目录；可选带 cost_rate 预览「目录 × 倍率」后的成本价。"""
    user, error = await _require_admin(request)
    if error:
        return error
    catalog = list_official_catalog()
    models = scale_catalog_rows(cost_rate, catalog=catalog) if cost_rate is not None else catalog
    models = [_attach_long_context(row) for row in models]
    return {
        "currency": "USD",
        "price_unit": "per 1M tokens",
        "source": "openai_official",
        "cost_rate": cost_rate,
        "count": len(models),
        "models": models,
    }


@app.post("/v1/admin/vendors/{code}/reapply-catalog")
async def admin_reapply_vendor_catalog(code: str, request: Request):
    """catalog_rate 供应商：按最新官方目录 × 已存 cost_rate 重写价目。"""
    user, error = await _require_admin(request)
    if error:
        return error
    meta = resolve_vendor(vendor=code.strip().lower())
    if not meta:
        return JSONResponse(status_code=404, content={"error": "vendor not found"})
    if (meta.get("pricing_mode") or "manual") != PRICING_MODE_CATALOG_RATE:
        return JSONResponse(status_code=400, content={"error": "vendor is not catalog_rate mode"})
    rate = meta.get("cost_rate")
    if rate is None or float(rate) <= 0:
        return JSONResponse(status_code=400, content={"error": "vendor cost_rate missing"})
    try:
        count = await apply_catalog_rate_prices(meta["code"], float(rate))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {
        "ok": True,
        "vendor": meta["code"],
        "cost_rate": float(rate),
        "count": count,
        "pricing_mode": PRICING_MODE_CATALOG_RATE,
    }


@app.get("/v1/admin/vendors/{code}/models")
async def admin_list_vendor_models(code: str, request: Request):
    """管理端：列出某供应商全部模型原始美元成本价。"""
    user, error = await _require_admin(request)
    if error:
        return error
    meta, rows = list_admin_vendor_models(code.strip().lower())
    if not meta:
        return JSONResponse(status_code=404, content={"error": "vendor not found"})
    return {
        "vendor": {
            "code": meta["code"],
            "name": meta.get("name"),
            "base_url": meta.get("base_url"),
            "pricing_mode": meta.get("pricing_mode") or "manual",
            "cost_rate": meta.get("cost_rate"),
            "is_default": meta.get("is_default"),
            "last_synced_at": meta.get("last_synced_at"),
        },
        "currency": "USD",
        "price_unit": "per 1M tokens",
        "models": [_attach_long_context(row) for row in rows],
    }


@app.get("/v1/admin/vendors/{code}/models/{model_id:path}")
async def admin_get_vendor_model(
    code: str,
    model_id: str,
    request: Request,
    service_tier: str = Query("standard"),
):
    """管理端：读单个模型+档位的美元成本价。"""
    user, error = await _require_admin(request)
    if error:
        return error
    found = get_admin_vendor_model(code.strip().lower(), model_id, service_tier=service_tier)
    if found is None:
        return JSONResponse(status_code=404, content={"error": "vendor not found"})
    if not found.get("price"):
        return JSONResponse(status_code=404, content={"error": "model price not found"})
    return {
        "vendor": {
            "code": found["vendor"]["code"],
            "name": found["vendor"].get("name"),
            "base_url": found["vendor"].get("base_url"),
        },
        "currency": "USD",
        "price_unit": "per 1M tokens",
        "price": found["price"],
    }


@app.put("/v1/admin/vendors/{code}/models/{model_id:path}")
async def admin_put_vendor_model(
    code: str,
    model_id: str,
    payload: VendorSingleModelPriceUpdate,
    request: Request,
):
    """管理端：单独新增/覆盖某个模型价格（USD / 1M tokens）。"""
    user, error = await _require_admin(request)
    if error:
        return error
    item = payload.model_dump(exclude_unset=True)
    item["model"] = (payload.model or model_id or "").strip()
    if not item["model"]:
        return JSONResponse(status_code=400, content={"error": "model is required"})
    try:
        result = await upsert_single_vendor_model(code.strip().lower(), item)
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return {
        "ok": True,
        "vendor": {
            "code": result["vendor"]["code"],
            "name": result["vendor"].get("name"),
            "base_url": result["vendor"].get("base_url"),
        },
        "currency": "USD",
        "price_unit": "per 1M tokens",
        "price": result["price"],
    }


@app.delete("/v1/admin/vendors/{code}/models/{model_id:path}")
async def admin_delete_vendor_model(
    code: str,
    model_id: str,
    request: Request,
    service_tier: str = Query("standard"),
):
    """管理端：软删除某个模型+档位价格。"""
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        result = await delete_vendor_model_price(
            code.strip().lower(), model_id, service_tier=service_tier,
        )
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return {"ok": True, **result}


@app.put("/v1/admin/vendors/{code}/models")
async def admin_upsert_vendor_models(code: str, payload: VendorModelPriceUpsert, request: Request):
    """管理端：批量写入模型价（可一次改多个）。"""
    user, error = await _require_admin(request)
    if error:
        return error
    rows = [item.model_dump(exclude_unset=True) for item in payload.models]
    try:
        count = await upsert_vendor_prices(code.strip().lower(), rows, source="manual")
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return {"vendor": code.strip().lower(), "upserted": count}


@app.post("/v1/admin/vendors/{code}/refresh")
async def admin_refresh_vendor_models(code: str, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    result = await refresh_vendor_prices(code.strip().lower(), force=True)
    status = 200 if result.get("ok") else 502
    return JSONResponse(status_code=status, content=result)


@app.get("/v1/admin/vendors/{code}/balance")
async def admin_vendor_balance(
    code: str,
    request: Request,
    refresh: bool = Query(False),
):
    user, error = await _require_admin(request)
    if error:
        return error
    code = (code or "").strip().lower()
    if code != "teamo":
        return JSONResponse(status_code=404, content={"error": "balance is only available for teamo"})
    meta = resolve_vendor(vendor="teamo") or {}
    balance = await fetch_teamo_balance(
        client=getattr(request.app.state, "http_teamo", None),
        vendor_base_url=meta.get("base_url") or "",
        force=refresh,
    )
    return {key: value for key, value in balance.items() if key != "_mono"}


@app.post("/v1/admin/vendors/{code}/activate")
async def admin_activate_vendor(code: str, request: Request):
    """热切换当前上游：改 DB is_default，立即刷新本进程缓存；其它 worker ≤30s 内跟上。"""
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        runtime = await activate_vendor(code.strip().lower())
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    return {"ok": True, "runtime": runtime, "vendors": list_vendors(include_pricing_meta=True)}


@app.get("/v1/admin/model-routes")
async def admin_list_model_routes(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"routes": list_routes(), "vendors": list_vendors(include_pricing_meta=True)}


@app.put("/v1/admin/model-routes")
async def admin_upsert_model_route(payload: ModelRouteUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        row = await upsert_route(
            model=payload.model,
            vendor_code=payload.vendor_code,
            is_active=payload.is_active,
            note=payload.note,
            route_id=payload.id,
            api_key=payload.api_key,
            api_key_id=payload.api_key_id,
            user_ids=payload.user_ids,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "route": row, "routes": list_routes()}


@app.post("/v1/admin/model-routes/replace-key")
async def admin_replace_model_route_keys(payload: ModelRouteKeyReplace, request: Request):
    """按模型检索，一次替换命中规则上的覆盖密钥。"""
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        result = await replace_route_api_keys(model=payload.model, api_key=payload.api_key)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, **result, "routes": list_routes()}


@app.delete("/v1/admin/model-routes/{route_id}")
async def admin_delete_model_route(route_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    ok = await delete_route(route_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "route not found"})
    return {"ok": True, "routes": list_routes()}


@app.get("/v1/admin/model-rewrites")
async def admin_list_model_rewrites(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"rewrites": list_rewrites()}


@app.put("/v1/admin/model-rewrites")
async def admin_upsert_model_rewrite(payload: ModelRewriteUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        row = await upsert_rewrite(
            model=payload.model,
            target_model=payload.target_model,
            is_active=payload.is_active,
            note=payload.note,
            rewrite_id=payload.id,
            user_ids=payload.user_ids,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "rewrite": row, "rewrites": list_rewrites()}


@app.delete("/v1/admin/model-rewrites/{rewrite_id}")
async def admin_delete_model_rewrite(rewrite_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    ok = await delete_rewrite(rewrite_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "rewrite not found"})
    return {"ok": True, "rewrites": list_rewrites()}


@app.get("/v1/admin/model-protocols")
async def admin_list_model_protocols(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"protocols": list_protocols()}


@app.put("/v1/admin/model-protocols")
async def admin_upsert_model_protocol(payload: ModelProtocolUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        row = await upsert_protocol(
            model=payload.model,
            protocol=payload.protocol,
            is_active=payload.is_active,
            note=payload.note,
            protocol_id=payload.id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "protocol": row, "protocols": list_protocols()}


@app.delete("/v1/admin/model-protocols/{protocol_id}")
async def admin_delete_model_protocol(protocol_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    ok = await delete_protocol(protocol_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "protocol not found"})
    return {"ok": True, "protocols": list_protocols()}


@app.get("/v1/admin/model-aliases")
async def admin_list_model_aliases(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"aliases": list_aliases()}


@app.put("/v1/admin/model-aliases")
async def admin_upsert_model_alias(payload: ModelAliasUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        row = await upsert_alias(
            alias=payload.alias,
            target_model=payload.target_model,
            protocol=payload.protocol,
            hide_target=payload.hide_target,
            is_active=payload.is_active,
            note=payload.note,
            alias_id=payload.id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "alias": row, "aliases": list_aliases()}


@app.delete("/v1/admin/model-aliases/{alias_id}")
async def admin_delete_model_alias(alias_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    ok = await delete_alias(alias_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "alias not found"})
    return {"ok": True, "aliases": list_aliases()}


@app.get("/v1/admin/prompt-injections")
async def admin_list_prompt_injections(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"injections": list_injections()}


@app.put("/v1/admin/prompt-injections")
async def admin_upsert_prompt_injection(payload: PromptInjectionUpsert, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    try:
        row = await upsert_injection(
            name=payload.name,
            content=payload.content,
            scope=payload.scope,
            target=payload.target,
            position=payload.position,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
            note=payload.note,
            injection_id=payload.id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"ok": True, "injection": row, "injections": list_injections()}


@app.delete("/v1/admin/prompt-injections/{injection_id}")
async def admin_delete_prompt_injection(injection_id: int, request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    ok = await delete_injection(injection_id)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "prompt injection not found"})
    return {"ok": True, "injections": list_injections()}


@app.get("/v1/admin/runtime")
async def admin_runtime_upstream(request: Request):
    user, error = await _require_admin(request)
    if error:
        return error
    return {"runtime": get_runtime_upstream(), "vendors": list_vendors(include_pricing_meta=True)}


USAGE_STATUSES = {"pending", "billed", "unbilled", "failed"}


def _parse_usage_time(value: Optional[str], name: str) -> Optional[datetime]:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {name}; expected ISO datetime") from exc
    # 数据库时间为无时区本地时间；拒绝带时区值，避免隐式时区转换造成边界误差。
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _usage_filters(
    *, scope_user_id: Optional[int], user_id: Optional[int], model: Optional[str],
    model_prefix: Optional[str], status: Optional[str], source: Optional[str],
    path: Optional[str], request_id: Optional[str], from_time: Optional[str], to_time: Optional[str],
):
    effective_model = model or None
    effective_prefix = None if effective_model else (model_prefix or None)
    start = _parse_usage_time(from_time, "from")
    end = _parse_usage_time(to_time, "to")
    if start is not None and end is not None:
        if start >= end:
            raise ValueError("from must be earlier than to")
        if end - start > timedelta(days=93):
            raise ValueError("time range cannot exceed 93 days")
    if status is not None and status not in USAGE_STATUSES:
        raise ValueError("invalid status")
    conditions = []
    if scope_user_id is not None:
        conditions.append(RequestUsage.user_id == scope_user_id)
    if effective_model:
        conditions.append(RequestUsage.model == effective_model)
    elif effective_prefix:
        conditions.append(RequestUsage.model.like(f"{effective_prefix}%"))
    if status:
        conditions.append(RequestUsage.status == status)
    if source:
        conditions.append(RequestUsage.source == source)
    if path:
        conditions.append(RequestUsage.path == path)
    if request_id:
        conditions.append(RequestUsage.request_id == request_id)
    if start is not None:
        conditions.append(RequestUsage.created_at >= start)
    if end is not None:
        conditions.append(RequestUsage.created_at < end)
    return conditions, {
        "user_id": user_id,
        "model": effective_model,
        "model_prefix": effective_prefix,
        "status": status,
        "source": source,
        "path": path,
        "request_id": request_id,
        "from": from_time or None,
        "to": to_time or None,
    }


async def _usage_viewer(db, request: Request, user_id: Optional[int]):
    key = extract_user_key(dict(request.headers))
    if not key:
        return None, JSONResponse(status_code=401, content={"error": "missing API key"})
    user = await lookup_user_by_credential(db, key)
    if user is None:
        return None, JSONResponse(status_code=403, content={"error": "invalid API key"})
    viewer_id, is_staff = user.user_id, bool(user.is_staff)
    if not is_staff and user_id is not None and user_id != viewer_id:
        return None, JSONResponse(status_code=403, content={"error": "can only query own usage"})
    return (type("Viewer", (), {"user_id": viewer_id, "is_staff": is_staff})(), user_id if is_staff else viewer_id), None


def _attach_long_context(row: dict) -> dict:
    """短上下文价已经含倍率。这里只附上官方倍数换算后的长上下文价，不改入库成本。"""
    if row.get("kind") == "image":
        return row
    quote = long_context_quote(
        row.get("model") or "",
        row,
        service_tier=row.get("service_tier") or "standard",
    )
    if quote:
        row["long_context"] = quote
    return row


def _usage_item(row, is_admin: bool) -> dict:
    item = {
        "request_id": row.request_id, "user_id": row.user_id,
        "model": row.model, "path": row.path, "source": row.source,
        "status": row.status, "http_status": row.http_status,
        "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "cache_read_tokens": row.cache_read_tokens, "cache_write_tokens": row.cache_write_tokens,
        "context_tier": row.context_tier,
        "charged_cny": float(row.charged_cny) if row.charged_cny is not None else None,
        "balance_cny": float(row.balance_cny) if row.balance_cny is not None else None,
        "cost_cny": float(row.cost_cny) if row.cost_cny is not None else None,
        "profit_cny": float(row.profit_cny) if row.profit_cny is not None else None,
        "ttfb_ms": row.ttfb_ms,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if not is_admin:
        item["cost_cny"] = item["profit_cny"] = None
    return item


@app.get("/v1/usage")
async def list_request_usage(
    request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    user_id: Optional[int] = Query(None), model: Optional[str] = Query(None),
    model_prefix: Optional[str] = Query(None), status: Optional[str] = Query(None),
    source: Optional[str] = Query(None), path: Optional[str] = Query(None),
    request_id: Optional[str] = Query(None), from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
):
    filters = {"user_id": user_id, "model": model, "model_prefix": model_prefix, "status": status,
               "source": source, "path": path, "request_id": request_id,
               "from_time": from_time, "to_time": to_time}
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, user_id)
            if error:
                return error
            viewer, scope_user_id = auth
            conditions, _ = _usage_filters(scope_user_id=scope_user_id, **filters)
            total = await db.scalar(select(func.count()).select_from(RequestUsage).where(*conditions))
            rows = (await db.execute(select(RequestUsage).where(*conditions).order_by(
                RequestUsage.created_at.desc(), RequestUsage.request_id.desc()).limit(limit).offset(offset))).scalars().all()
            return {"total": total, "limit": limit, "offset": offset,
                    "items": [_usage_item(row, bool(viewer.is_staff)) for row in rows]}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except Exception:
        process_log.exception("usage query failed")
        return JSONResponse(status_code=503, content={"error": "usage query unavailable"})


@app.get("/v1/usage/summary")
async def summarize_request_usage(
    request: Request, user_id: Optional[int] = Query(None), model: Optional[str] = Query(None),
    model_prefix: Optional[str] = Query(None), status: Optional[str] = Query(None),
    source: Optional[str] = Query(None), path: Optional[str] = Query(None),
    request_id: Optional[str] = Query(None), from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
):
    filters = {"user_id": user_id, "model": model, "model_prefix": model_prefix, "status": status,
               "source": source, "path": path, "request_id": request_id,
               "from_time": from_time, "to_time": to_time}
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, user_id)
            if error:
                return error
            viewer, scope_user_id = auth
            conditions, echoed = _usage_filters(scope_user_id=scope_user_id, **filters)
            if not viewer.is_staff:
                echoed["user_id"] = scope_user_id
            recharge_filters = [or_(Bill.is_delete.is_(False), Bill.is_delete.is_(None))]
            if scope_user_id is not None:
                recharge_filters.append(Bill.generate_by == scope_user_id)
            recharge_sum = await db.scalar(
                select(func.coalesce(func.sum(Bill.money), 0)).where(*recharge_filters)
            )
            row = (await db.execute(select(
                func.count().label("request_count"),
                func.sum(case((RequestUsage.status == "billed", 1), else_=0)).label("billed_count"),
                func.sum(case((RequestUsage.status == "pending", 1), else_=0)).label("pending_count"),
                func.sum(case((RequestUsage.status == "unbilled", 1), else_=0)).label("unbilled_count"),
                func.sum(case((RequestUsage.status == "failed", 1), else_=0)).label("failed_count"),
                func.sum(case((RequestUsage.status == "billed", RequestUsage.charged_cny), else_=0)).label("sum_charged_cny"),
                func.sum(case((RequestUsage.status == "billed", RequestUsage.cost_cny), else_=0)).label("sum_cost_cny"),
                func.sum(case((RequestUsage.status == "billed", RequestUsage.profit_cny), else_=0)).label("sum_profit_cny"),
                func.coalesce(func.sum(RequestUsage.input_tokens), 0).label("sum_input_tokens"),
                func.coalesce(func.sum(RequestUsage.output_tokens), 0).label("sum_output_tokens"),
                func.coalesce(func.sum(RequestUsage.cache_read_tokens), 0).label("sum_cache_read_tokens"),
                func.coalesce(func.sum(RequestUsage.cache_write_tokens), 0).label("sum_cache_write_tokens"),
            ).where(*conditions))).one()
            result = {"filters": echoed, "request_count": row.request_count or 0,
                "billed_count": row.billed_count or 0, "pending_count": row.pending_count or 0,
                "unbilled_count": row.unbilled_count or 0, "failed_count": row.failed_count or 0,
                "sum_charged_cny": float(row.sum_charged_cny or 0),
                "sum_recharge_cny": round(float(recharge_sum or 0), 2),
                "sum_cost_cny": float(row.sum_cost_cny or 0) if viewer.is_staff else None,
                "sum_profit_cny": float(row.sum_profit_cny or 0) if viewer.is_staff else None,
                "sum_input_tokens": row.sum_input_tokens or 0, "sum_output_tokens": row.sum_output_tokens or 0,
                "sum_cache_read_tokens": row.sum_cache_read_tokens or 0, "sum_cache_write_tokens": row.sum_cache_write_tokens or 0}
            return result
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except Exception:
        process_log.exception("usage summary query failed")
        return JSONResponse(status_code=503, content={"error": "usage query unavailable"})


@app.get("/v1/usage/latency")
async def summarize_request_latency(
    request: Request, user_id: Optional[int] = Query(None), model: Optional[str] = Query(None),
    model_prefix: Optional[str] = Query(None), status: Optional[str] = Query(None),
    source: Optional[str] = Query(None), path: Optional[str] = Query(None),
    request_id: Optional[str] = Query(None), from_time: Optional[str] = Query(None, alias="from"),
    to_time: Optional[str] = Query(None, alias="to"),
):
    """按模型统计最近 10 次请求的平均首字时延。满 10 次且平均超过 15 秒则 alert。"""
    filters = {"user_id": user_id, "model": model, "model_prefix": model_prefix, "status": status,
               "source": source, "path": path, "request_id": request_id,
               "from_time": from_time, "to_time": to_time}
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, user_id)
            if error:
                return error
            _viewer, scope_user_id = auth
            conditions, _echoed = _usage_filters(scope_user_id=scope_user_id, **filters)
            lookback_days = None
            if not from_time and not to_time:
                conditions = [*conditions, RequestUsage.created_at >= datetime.now() - LATENCY_LOOKBACK]
                lookback_days = LATENCY_LOOKBACK.days
            rows = (await db.execute(select(
                RequestUsage.model, RequestUsage.ttfb_ms,
            ).where(*conditions, RequestUsage.ttfb_ms.is_not(None)).order_by(
                RequestUsage.created_at.desc(), RequestUsage.request_id.desc(),
            ))).all()
            return {
                "window": LATENCY_WINDOW,
                "alert_ms": LATENCY_ALERT_MS,
                "lookback_days": lookback_days,
                "models": latency_models_from_samples(rows),
            }
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except Exception:
        process_log.exception("usage latency query failed")
        return JSONResponse(status_code=503, content={"error": "usage query unavailable"})


ACCOUNT_SPENDING_MAX_DAYS = 366


def _parse_day(value: Optional[str], name: str):
    text = (value or "").strip()
    if not text:
        raise ValueError(f"invalid {name}; expected YYYY-MM-DD")
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {name}; expected YYYY-MM-DD") from exc


@app.get("/v1/account/balance")
async def get_account_balance(request: Request):
    """用请求头里的 API Key 查询本人当前余额（人民币）。"""
    user, error = await _get_api_user(request)
    if error:
        return error
    try:
        async with AsyncSessionLocal() as db:
            balance = await db.scalar(select(User.token).where(User.user_id == user.user_id))
    except Exception:
        process_log.exception("account balance query failed")
        return JSONResponse(status_code=503, content={"error": "balance query unavailable"})
    return {
        "user_id": user.user_id,
        "balance_cny": round(float(balance or 0), 8),
        "currency": "CNY",
        "is_active": bool(user.is_activate),
    }


@app.get("/v1/account/spending")
async def get_account_spending(
    request: Request,
    date_from: Optional[str] = Query(None, alias="from", description="起始日期 YYYY-MM-DD，含当天"),
    date_to: Optional[str] = Query(None, alias="to", description="结束日期 YYYY-MM-DD，含当天"),
):
    """按日期范围统计本人花费（人民币），可按天汇总。只统计本人已计费请求。"""
    user, error = await _get_api_user(request)
    if error:
        return error
    try:
        today = datetime.now().date()
        to_day = _parse_day(date_to, "to") if date_to else today
        from_day = _parse_day(date_from, "from") if date_from else to_day - timedelta(days=29)
        if from_day > to_day:
            raise ValueError("from must be earlier than or equal to to")
        if (to_day - from_day).days + 1 > ACCOUNT_SPENDING_MAX_DAYS:
            raise ValueError(f"date range cannot exceed {ACCOUNT_SPENDING_MAX_DAYS} days")
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})

    start = datetime.combine(from_day, datetime.min.time())
    end = datetime.combine(to_day + timedelta(days=1), datetime.min.time())
    try:
        async with AsyncSessionLocal() as db:
            conditions = [
                RequestUsage.user_id == user.user_id,
                RequestUsage.status == "billed",
                RequestUsage.created_at >= start,
                RequestUsage.created_at < end,
            ]
            day_col = func.date(RequestUsage.created_at)
            rows = (await db.execute(select(
                day_col.label("day"),
                func.count().label("request_count"),
                func.coalesce(func.sum(RequestUsage.charged_cny), 0).label("charged_cny"),
                func.coalesce(func.sum(RequestUsage.cost_cny), 0).label("cost_cny"),
                func.coalesce(func.sum(RequestUsage.profit_cny), 0).label("profit_cny"),
                func.coalesce(func.sum(RequestUsage.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(RequestUsage.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.sum(RequestUsage.cache_read_tokens), 0).label("cache_read_tokens"),
                func.coalesce(func.sum(RequestUsage.cache_write_tokens), 0).label("cache_write_tokens"),
            ).where(*conditions).group_by(day_col).order_by(day_col))).all()
            recharge = await db.scalar(select(
                func.coalesce(func.sum(Bill.money), 0)
            ).where(
                Bill.generate_by == user.user_id,
                or_(Bill.is_delete.is_(False), Bill.is_delete.is_(None)),
                Bill.generate_time >= start,
                Bill.generate_time < end,
            ))
            balance = await db.scalar(select(User.token).where(User.user_id == user.user_id))
    except Exception:
        process_log.exception("account spending query failed")
        return JSONResponse(status_code=503, content={"error": "spending query unavailable"})

    is_staff = bool(user.is_staff)
    by_day = {
        str(row.day): {
            "date": str(row.day),
            "request_count": int(row.request_count or 0),
            "charged_cny": float(row.charged_cny or 0),
            "cost_cny": float(row.cost_cny or 0) if is_staff else None,
            "profit_cny": float(row.profit_cny or 0) if is_staff else None,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "cache_read_tokens": int(row.cache_read_tokens or 0),
            "cache_write_tokens": int(row.cache_write_tokens or 0),
        }
        for row in rows
    }
    days = []
    cursor = from_day
    while cursor <= to_day:
        key = cursor.isoformat()
        days.append(by_day.get(key) or {
            "date": key, "request_count": 0, "charged_cny": 0.0,
            "cost_cny": 0.0 if is_staff else None,
            "profit_cny": 0.0 if is_staff else None,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        })
        cursor += timedelta(days=1)

    totals = {
        "request_count": sum(item["request_count"] for item in days),
        "charged_cny": sum(item["charged_cny"] for item in days),
        "recharged_cny": round(float(recharge or 0), 8),
        "input_tokens": sum(item["input_tokens"] for item in days),
        "output_tokens": sum(item["output_tokens"] for item in days),
        "cache_read_tokens": sum(item["cache_read_tokens"] for item in days),
        "cache_write_tokens": sum(item["cache_write_tokens"] for item in days),
    }
    totals["charged_cny"] = round(totals["charged_cny"], 8)
    if is_staff:
        totals["cost_cny"] = round(sum(item["cost_cny"] or 0 for item in days), 8)
        totals["profit_cny"] = round(sum(item["profit_cny"] or 0 for item in days), 8)

    return {
        "user_id": user.user_id,
        "currency": "CNY",
        "from": from_day.isoformat(),
        "to": to_day.isoformat(),
        "totals": totals,
        "balance_cny": round(float(balance or 0), 8),
        "days": days,
    }


class AgentTaskStatusBody(BaseModel):
    id: int
    status: str


class AgentTaskIdBody(BaseModel):
    id: int


class AgentTaskBulkBody(BaseModel):
    account: Optional[str] = None


def _task_item(row: AgentTask) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "account": row.account,
        "model": row.model,
        "source": row.source,
        "path": row.path,
        "status": row.status,
        "status_source": row.status_source,
        "suggested_status": row.suggested_status,
        "finish": row.finish,
        "result": row.result,
        "decision": row.decision,
        "steps": row.steps,
        "last_request_id": row.last_request_id,
        "http_status": row.http_status,
        "started_at": row.started_at.isoformat(sep=" ") if row.started_at else None,
        "updated_at": row.updated_at.isoformat(sep=" ") if row.updated_at else None,
    }


def _task_filters(viewer, account: Optional[str], scope: str):
    conditions = [AgentTask.is_delete.is_(False)]
    if not viewer.is_staff:
        conditions.append(AgentTask.user_id == viewer.user_id)
    elif account:
        conditions.append(AgentTask.account == account.strip())
    if scope == "open":
        conditions.append(AgentTask.status.in_(("running", "failed")))
    elif scope in {"running", "done", "failed"}:
        conditions.append(AgentTask.status == scope)
    elif scope != "all":
        raise ValueError("invalid scope")
    return conditions


async def _load_own_task(db, viewer, task_id: int):
    conditions = [AgentTask.id == task_id, AgentTask.is_delete.is_(False)]
    if not viewer.is_staff:
        conditions.append(AgentTask.user_id == viewer.user_id)
    return (await db.execute(select(AgentTask).where(*conditions))).scalar_one_or_none()


@app.get("/v1/tasks")
async def list_agent_tasks(
    request: Request,
    scope: str = Query("open"),
    account: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            conditions = _task_filters(viewer, account, scope)
            base = [AgentTask.is_delete.is_(False)]
            if not viewer.is_staff:
                base.append(AgentTask.user_id == viewer.user_id)
            elif account:
                base.append(AgentTask.account == account.strip())
            counts = {}
            for name in ("running", "done", "failed"):
                counts[name] = await db.scalar(select(func.count()).select_from(AgentTask).where(
                    *base, AgentTask.status == name,
                )) or 0
            counts["open"] = counts["running"] + counts["failed"]
            counts["all"] = counts["running"] + counts["done"] + counts["failed"]
            total = await db.scalar(select(func.count()).select_from(AgentTask).where(*conditions)) or 0
            rows = (await db.execute(
                select(AgentTask).where(*conditions).order_by(AgentTask.updated_at.desc(), AgentTask.id.desc()).limit(limit).offset(offset)
            )).scalars().all()
            return {"total": total, "limit": limit, "offset": offset, "scope": scope, "counts": counts,
                    "items": [_task_item(row) for row in rows]}
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": str(exc)})
    except Exception:
        process_log.exception("agent task query failed")
        return JSONResponse(status_code=503, content={"error": "task query unavailable"})


@app.post("/v1/tasks/status")
async def set_agent_task_status(request: Request, body: AgentTaskStatusBody):
    if body.status not in {"running", "done", "failed"}:
        return JSONResponse(status_code=422, content={"error": "invalid status"})
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            row = await _load_own_task(db, viewer, body.id)
            if row is None:
                return JSONResponse(status_code=404, content={"error": "task not found"})
            row.status = body.status
            row.status_source = "manual"
            row.updated_at = datetime.now()
            await db.commit()
            await db.refresh(row)
            return {"item": _task_item(row)}
    except Exception:
        process_log.exception("agent task status failed")
        return JSONResponse(status_code=503, content={"error": "task update unavailable"})


@app.post("/v1/tasks/reevaluate")
async def reevaluate_agent_task(request: Request, body: AgentTaskIdBody):
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            row = await _load_own_task(db, viewer, body.id)
            if row is None:
                return JSONResponse(status_code=404, content={"error": "task not found"})
            row.status = row.suggested_status or "running"
            row.status_source = "log"
            row.decision = explain_task(row.status, row.finish, row.result)[:300]
            row.updated_at = datetime.now()
            await db.commit()
            await db.refresh(row)
            return {"item": _task_item(row)}
    except Exception:
        process_log.exception("agent task reevaluate failed")
        return JSONResponse(status_code=503, content={"error": "task update unavailable"})


@app.post("/v1/tasks/complete-all")
async def complete_all_agent_tasks(request: Request, body: AgentTaskBulkBody):
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            conditions = _task_filters(viewer, body.account, "running")
            rows = (await db.execute(select(AgentTask).where(*conditions))).scalars().all()
            now = datetime.now()
            for row in rows:
                row.status = "done"
                row.status_source = "manual"
                row.updated_at = now
            await db.commit()
            return {"updated": len(rows)}
    except Exception:
        process_log.exception("agent task complete-all failed")
        return JSONResponse(status_code=503, content={"error": "task update unavailable"})


@app.post("/v1/tasks/delete")
async def delete_agent_task(request: Request, body: AgentTaskIdBody):
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            row = await _load_own_task(db, viewer, body.id)
            if row is None:
                return JSONResponse(status_code=404, content={"error": "task not found"})
            row.is_delete = True
            row.updated_at = datetime.now()
            await db.commit()
            return {"deleted": 1}
    except Exception:
        process_log.exception("agent task delete failed")
        return JSONResponse(status_code=503, content={"error": "task update unavailable"})


@app.post("/v1/tasks/delete-done")
async def delete_done_agent_tasks(request: Request, body: AgentTaskBulkBody):
    try:
        async with AsyncSessionLocal() as db:
            auth, error = await _usage_viewer(db, request, None)
            if error:
                return error
            viewer, _scope_user_id = auth
            conditions = _task_filters(viewer, body.account, "done")
            rows = (await db.execute(select(AgentTask).where(*conditions))).scalars().all()
            now = datetime.now()
            for row in rows:
                row.is_delete = True
                row.updated_at = now
            await db.commit()
            return {"deleted": len(rows)}
    except Exception:
        process_log.exception("agent task delete-done failed")
        return JSONResponse(status_code=503, content={"error": "task update unavailable"})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/headroom/stats")
async def headroom_stats():
    """累计压缩前/后/节省 tokens。gunicorn 多 worker 时每个进程各自统计，重启清零。"""
    return get_stats()


if __name__ == "__main__":
    run_kwargs = {
        "host": "0.0.0.0",
        "port": 4000,
        "timeout_keep_alive": 75,
        "limit_concurrency": LIMIT_CONCURRENCY,
    }
    # uvicorn 多 worker 必须传 import string；单进程直接传 app，少一次 import。
    if UVICORN_WORKERS > 1:
        uvicorn.run("main:app", workers=UVICORN_WORKERS, **run_kwargs)
    else:
        uvicorn.run(app, **run_kwargs)
