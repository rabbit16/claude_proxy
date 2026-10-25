"""把已结束但未扣费的请求，按当前模型和供应商价目补扣。

用户自己断开连接时，流里往往已经有完整 usage，但扣费协程会被取消，
记录停在 unbilled。这里用当前路由命中的供应商价目（没有路由则用当前默认供应商）补扣。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, text, update

from settings.database import AsyncSessionLocal
from settings.models import RequestUsage, User
from utils.check_user import compute_user_token
from utils.common_utils import (
    is_audio_path,
    is_billable_path,
    is_gemini_native_path,
    is_image_path,
)
from utils.get_log import process_log
from utils.model_route import match_model_route
from utils.vendor_catalog import get_runtime_upstream

LOCK_NAME = "claude_proxy_unbilled_reconcile"
RECONCILE_INTERVAL = 60.0
RECONCILE_MIN_AGE = timedelta(seconds=90)
RECONCILE_BATCH = 40
STALE_CLAIM_AGE = timedelta(minutes=10)
STALE_PENDING_AGE = timedelta(days=1)
STALE_PENDING_HTTP_STATUS = 502
CLAIM_STATUS = "reconciling"

_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "audio_input_tokens",
    "audio_output_tokens",
)


def usage_has_billable_tokens(usage: Optional[dict]) -> bool:
    if not isinstance(usage, dict):
        return False
    for key in _TOKEN_KEYS:
        try:
            if int(usage.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def token_usage_dict(row) -> dict:
    return {
        "input_tokens": int(row.input_tokens or 0),
        "output_tokens": int(row.output_tokens or 0),
        "cache_read_tokens": int(row.cache_read_tokens or 0),
        "cache_write_tokens": int(row.cache_write_tokens or 0),
    }


def resolve_reconcile_vendor(model: str, user_id: Optional[int]) -> str:
    """当前对该用户生效的供应商。命中路由用路由厂商，否则用当前默认供应商。"""
    rule = match_model_route(model, user_id=user_id)
    if rule and rule.get("vendor_code"):
        runtime = get_runtime_upstream(vendor=rule["vendor_code"])
        code = (runtime.get("code") or "").strip().lower()
        if code:
            return code
    runtime = get_runtime_upstream()
    return (runtime.get("code") or "").strip().lower()


def is_token_reconcile_path(path: str, model: str = "") -> bool:
    """聊天 / Responses / Messages / Embeddings 能用已落库的 token 重算。图片和音频不行。"""
    if not is_billable_path(path or "", model or ""):
        return False
    if is_image_path(path or "") or is_audio_path(path or "") or is_gemini_native_path(path or ""):
        return False
    name = (model or "").strip().lower().rsplit("/", 1)[-1]
    if name.startswith("gpt-audio"):
        return False
    return True


def is_stale_pending(row, *, now: Optional[datetime] = None, max_age: timedelta = STALE_PENDING_AGE) -> bool:
    """进入后超过一天仍是处理中，视为上游没有正常结束。"""
    if (getattr(row, "status", "") or "") != "pending":
        return False
    started = getattr(row, "created_at", None) or getattr(row, "updated_at", None)
    if started is None:
        return False
    current = now or datetime.now()
    return started < current - max_age


async def fail_stale_pending(*, now: Optional[datetime] = None) -> int:
    """把超过一天的 pending 收成失败，HTTP 状态记 502。不扣费。"""
    cutoff = (now or datetime.now()) - STALE_PENDING_AGE
    async with AsyncSessionLocal() as db:
        result = await db.execute(update(RequestUsage).where(
            RequestUsage.status == "pending",
            RequestUsage.created_at < cutoff,
        ).values(status="failed", http_status=STALE_PENDING_HTTP_STATUS))
        await db.commit()
        count = int(result.rowcount or 0)
    if count:
        process_log.info(
            "stale pending closed as 502 count=%s older_than=%s",
            count,
            cutoff.isoformat(timespec="seconds"),
        )
    return count


def is_reconcile_candidate(row, *, now: Optional[datetime] = None, min_age: timedelta = RECONCILE_MIN_AGE) -> bool:
    """请求已成功结束、有 token、且已经静置一段时间，避免和还在跑的扣费抢。"""
    if (getattr(row, "status", "") or "") != "unbilled":
        return False
    http_status = getattr(row, "http_status", None)
    if http_status is None or int(http_status) >= 400:
        return False
    if not getattr(row, "user_id", None):
        return False
    model = (getattr(row, "model", "") or "").strip()
    if not model or model == "-":
        return False
    if not is_token_reconcile_path(getattr(row, "path", "") or "", model):
        return False
    if not usage_has_billable_tokens(token_usage_dict(row)):
        return False
    updated_at = getattr(row, "updated_at", None)
    if updated_at is not None:
        current = now or datetime.now()
        if updated_at > current - min_age:
            return False
    return True


async def usage_is_billed(req_id: str) -> bool:
    if not req_id:
        return False
    async with AsyncSessionLocal() as db:
        status = await db.scalar(
            select(RequestUsage.status).where(RequestUsage.request_id == req_id)
        )
    return status == "billed"


async def charge_usage_row(row, *, phone: str, vendor: str):
    """按当前供应商价目扣这一笔。phone 是鉴权用的用户键（手机号或匿名用户主键）。"""
    return await compute_user_token(
        token_dict=token_usage_dict(row),
        body={"model": row.model or ""},
        user_id=phone,
        source=row.source or "openai",
        req_id=row.request_id,
        vendor=vendor,
    )


async def _claim(req_id: str) -> bool:
    async with AsyncSessionLocal() as db:
        result = await db.execute(update(RequestUsage).where(
            RequestUsage.request_id == req_id,
            RequestUsage.status == "unbilled",
        ).values(status=CLAIM_STATUS))
        await db.commit()
        return result.rowcount == 1


async def _postpone(req_id: str) -> None:
    """推后下次扫描，避免图片等无法按 token 重算的行一直占着批次。"""
    async with AsyncSessionLocal() as db:
        await db.execute(update(RequestUsage).where(
            RequestUsage.request_id == req_id,
            RequestUsage.status == "unbilled",
        ).values(updated_at=datetime.now()))
        await db.commit()


async def _release_claim(req_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(update(RequestUsage).where(
            RequestUsage.request_id == req_id,
            RequestUsage.status == CLAIM_STATUS,
        ).values(status="unbilled", updated_at=datetime.now()))
        await db.commit()


async def _recover_stale_claims() -> None:
    cutoff = datetime.now() - STALE_CLAIM_AGE
    async with AsyncSessionLocal() as db:
        await db.execute(update(RequestUsage).where(
            RequestUsage.status == CLAIM_STATUS,
            RequestUsage.updated_at < cutoff,
        ).values(status="unbilled"))
        await db.commit()


async def _select_candidates() -> list:
    cutoff = datetime.now() - RECONCILE_MIN_AGE
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(RequestUsage).where(
                RequestUsage.status == "unbilled",
                RequestUsage.http_status.is_not(None),
                RequestUsage.http_status < 400,
                RequestUsage.user_id.is_not(None),
                RequestUsage.updated_at < cutoff,
                or_(
                    RequestUsage.input_tokens > 0,
                    RequestUsage.output_tokens > 0,
                    RequestUsage.cache_read_tokens > 0,
                    RequestUsage.cache_write_tokens > 0,
                ),
            ).order_by(RequestUsage.updated_at.asc()).limit(RECONCILE_BATCH)
        )).scalars().all()
    return list(rows)


async def _phones_for(user_ids: list) -> dict:
    """主键 -> 计费键。匿名用户没有手机号，回落到主键字符串。"""
    ids = []
    for item in user_ids:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ids.append(value)
    if not ids:
        return {}
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(User.user_id, User.telephone).where(User.user_id.in_(ids))
        )).all()
    keys = {}
    for user_id, telephone in rows:
        phone = (telephone or "").strip()
        keys[int(user_id)] = phone or str(int(user_id))
    return keys


async def reconcile_unbilled_once() -> dict:
    """抢到库锁的那个进程补扣一批。扣费规则与在线请求相同：当前模型 + 当前供应商价目。"""
    stats = {"locked": False, "scanned": 0, "charged": 0, "skipped": 0, "failed": 0, "expired_pending": 0}
    async with AsyncSessionLocal() as lock_db:
        locked = await lock_db.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME})
        if int(locked or 0) != 1:
            return stats
        stats["locked"] = True
        try:
            stats["expired_pending"] = await fail_stale_pending()
            await _recover_stale_claims()
            rows = await _select_candidates()
            stats["scanned"] = len(rows)
            phones = await _phones_for([row.user_id for row in rows])
            now = datetime.now()
            for row in rows:
                if not is_reconcile_candidate(row, now=now):
                    await _postpone(row.request_id)
                    stats["skipped"] += 1
                    continue
                phone = phones.get(int(row.user_id))
                if not phone:
                    await _postpone(row.request_id)
                    stats["skipped"] += 1
                    continue
                if not await _claim(row.request_id):
                    stats["skipped"] += 1
                    continue
                vendor = resolve_reconcile_vendor(row.model, int(row.user_id))
                summary = None
                try:
                    summary = await charge_usage_row(row, phone=phone, vendor=vendor)
                    if summary:
                        stats["charged"] += 1
                        process_log.info(
                            "unbilled reconcile billed req=%s user=%s model=%s vendor=%s in=%s cache_r=%s out=%s spent=%.6f",
                            row.request_id,
                            row.user_id,
                            row.model,
                            vendor or "-",
                            int(row.input_tokens or 0),
                            int(row.cache_read_tokens or 0),
                            int(row.output_tokens or 0),
                            float(summary.get("charged") or 0),
                        )
                    else:
                        stats["skipped"] += 1
                except Exception:
                    process_log.exception("unbilled reconcile failed req=%s", row.request_id)
                    stats["failed"] += 1
                finally:
                    if not summary:
                        await _release_claim(row.request_id)
        finally:
            await lock_db.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})
            await lock_db.commit()
    return stats


async def unbilled_supervisor(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            stats = await reconcile_unbilled_once()
            if stats.get("charged") or stats.get("failed") or stats.get("expired_pending"):
                process_log.info("unbilled reconcile %s", stats)
        except Exception:
            process_log.exception("unbilled reconcile failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RECONCILE_INTERVAL)
        except asyncio.TimeoutError:
            pass
