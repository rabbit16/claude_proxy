from datetime import datetime, timedelta

from sqlalchemy import or_, select, text, update

from settings.database import AsyncSessionLocal
from settings.models import RequestUsage
from utils.api_key_auth import lookup_user_by_credential
from utils.get_log import process_log
from utils.task_guard import finish_writes

LATENCY_WINDOW = 10
LATENCY_ALERT_MS = 15_000
LATENCY_LOOKBACK = timedelta(days=7)


async def start_request_usage(req_id: str, user_key: str, model: str, path: str, source: str):
    async def write():
        async with AsyncSessionLocal() as db:
            user = await lookup_user_by_credential(db, user_key) if user_key else None
            user_id = user.user_id if user else None
            db.add(RequestUsage(
                request_id=req_id, user_id=user_id, model=model or "",
                path=path, source=source,
            ))
            await db.commit()

    try:
        await finish_writes(write)
    except Exception:
        process_log.exception("usage insert failed req=%s", req_id)


def _ttfb_value(ttfb_ms):
    if ttfb_ms is None:
        return None
    return max(0, int(ttfb_ms))


def shape_latency_model(model, window_count, avg_ttfb_ms, max_ttfb_ms, latest_ttfb_ms) -> dict:
    """最近一个窗口的首字时延。满窗口且平均超过阈值才告警。"""
    count = int(window_count or 0)
    avg = None if avg_ttfb_ms is None else int(round(float(avg_ttfb_ms)))
    return {
        "model": model or "-",
        "window_count": count,
        "avg_ttfb_ms": avg,
        "max_ttfb_ms": None if max_ttfb_ms is None else int(max_ttfb_ms),
        "latest_ttfb_ms": None if latest_ttfb_ms is None else int(latest_ttfb_ms),
        "alert": count >= LATENCY_WINDOW and avg is not None and avg > LATENCY_ALERT_MS,
    }


def shape_latency_models(rows) -> list:
    models = [
        shape_latency_model(
            row.model, row.window_count, row.avg_ttfb_ms, row.max_ttfb_ms, row.latest_ttfb_ms,
        )
        for row in rows
    ]
    models.sort(key=lambda item: (0 if item["alert"] else 1, -(item["avg_ttfb_ms"] or 0), item["model"]))
    return models


def latency_models_from_samples(rows) -> list:
    """rows 按时间从新到旧。兼容不支持窗口函数的 MySQL。"""
    grouped = {}
    for row in rows:
        model = row.model or "-"
        bucket = grouped.setdefault(model, [])
        if len(bucket) >= LATENCY_WINDOW or row.ttfb_ms is None:
            continue
        bucket.append(int(row.ttfb_ms))
    shaped = []
    for model, samples in grouped.items():
        shaped.append(type("Row", (), {
            "model": model,
            "window_count": len(samples),
            "avg_ttfb_ms": sum(samples) / len(samples),
            "max_ttfb_ms": max(samples),
            "latest_ttfb_ms": samples[0],
        })())
    return shape_latency_models(shaped)


async def _ensure_usage_column(conn, sql: str) -> None:
    try:
        await conn.execute(text(sql))
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate column" in msg or "1060" in msg:
            return
        if "doesn't exist" in msg or "1146" in msg:
            return
        raise


async def ensure_usage_schema_columns(conn) -> None:
    """已有库补齐首字时延、上下文计费档和扣后余额。新库由 create(checkfirst=True) 建表。"""
    await _ensure_usage_column(conn, "ALTER TABLE tb_request_usage ADD COLUMN ttfb_ms INT NULL")
    await _ensure_usage_column(
        conn, "ALTER TABLE tb_request_usage ADD COLUMN context_tier VARCHAR(16) NULL",
    )
    await _ensure_usage_column(
        conn, "ALTER TABLE tb_request_usage ADD COLUMN balance_cny DECIMAL(18, 8) NULL",
    )


async def finish_request_usage(req_id: str, status_code: int, usage: dict = None, ttfb_ms: int = None):
    usage = usage or {}
    values = {
        "http_status": status_code,
        "status": "failed" if status_code >= 400 else "unbilled",
    }
    if usage:
        values.update({
            "input_tokens": usage.get("input_tokens", 0) + usage.get("audio_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0) + usage.get("audio_output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
        })
    recorded_ttfb = _ttfb_value(ttfb_ms)
    if recorded_ttfb is not None:
        values["ttfb_ms"] = recorded_ttfb
    async def write():
        async with AsyncSessionLocal() as db:
            await db.execute(update(RequestUsage).where(
                RequestUsage.request_id == req_id,
                RequestUsage.status == "pending",
            ).values(**values))
            # 已计费的行不再走 pending 更新，首字时延仍要补上。
            if recorded_ttfb is not None:
                await db.execute(update(RequestUsage).where(
                    RequestUsage.request_id == req_id,
                    RequestUsage.ttfb_ms.is_(None),
                ).values(ttfb_ms=recorded_ttfb))
            await db.commit()

    try:
        await finish_writes(write)
    except Exception:
        process_log.exception("usage finalize failed req=%s", req_id)


async def retarget_request_usage(req_id: str, model: str) -> None:
    """把已落库的请求改记成最终模型。用量列表、时延和补扣都读这一列。"""
    model = (model or "").strip()
    if not req_id or not model or model == "-":
        return

    async def write():
        async with AsyncSessionLocal() as db:
            await db.execute(update(RequestUsage).where(
                RequestUsage.request_id == req_id,
            ).values(model=model[:255]))
            await db.commit()

    try:
        await finish_writes(write)
    except Exception:
        process_log.exception("usage retarget failed req=%s", req_id)


def billed_context_tier(summary: dict):
    tier = summary.get("context_tier")
    if tier in {"short", "long"}:
        return tier
    if summary.get("long_context"):
        return "long"
    return None


async def apply_billed_usage(db, req_id: str, summary: dict):
    result = await db.execute(update(RequestUsage).where(RequestUsage.request_id == req_id).values(
        status="billed", http_status=200,
        input_tokens=summary.get("input_tokens", 0) + summary.get("audio_input_tokens", 0),
        output_tokens=summary.get("output_tokens", 0) + summary.get("audio_output_tokens", 0),
        cache_read_tokens=summary.get("cache_read_tokens", 0),
        cache_write_tokens=summary.get("cache_write_tokens", 0),
        context_tier=billed_context_tier(summary),
        charged_cny=summary["charged"],
        balance_cny=summary.get("remain"),
        cost_cny=summary["cost_cny"],
        profit_cny=summary["profit"],
    ))
    if result.rowcount != 1:
        raise RuntimeError(f"missing usage record for request {req_id}")


def assign_missing_balances(current_balance, charges, recharges) -> dict:
    """从当前余额倒推每笔已计费请求扣完后的余额。

    charges: {request_id, at, charged, balance}。balance 已有值的不覆盖。
    recharges: (at, money)。已写入的余额优先于事后改过的当前余额。
    """
    events = []
    for item in charges:
        events.append((
            item.get("at") is not None,
            item.get("at") or datetime.min,
            1,
            str(item.get("request_id") or ""),
            "charge",
            item,
        ))
    for at, money in recharges:
        events.append((at is not None, at or datetime.min, 0, "", "recharge", money))
    events.sort(reverse=True)
    cursor = float(current_balance or 0)
    assigned = {}
    for _known, _at, _rank, _rid, kind, payload in events:
        if kind == "recharge":
            cursor -= float(payload or 0)
            continue
        stored = payload.get("balance")
        if stored is not None:
            cursor = float(stored)
        else:
            assigned[payload["request_id"]] = round(cursor, 8)
        cursor += float(payload.get("charged") or 0)
    return assigned


async def _backfill_user_balances(user_id: int) -> int:
    from settings.models import Bill, User

    async with AsyncSessionLocal() as db:
        token = await db.scalar(select(User.token).where(User.user_id == user_id))
        charge_rows = (await db.execute(select(
            RequestUsage.request_id,
            RequestUsage.updated_at,
            RequestUsage.created_at,
            RequestUsage.charged_cny,
            RequestUsage.balance_cny,
        ).where(
            RequestUsage.user_id == user_id,
            RequestUsage.status == "billed",
        ))).all()
        recharge_rows = (await db.execute(select(
            Bill.generate_time, Bill.money,
        ).where(
            Bill.generate_by == user_id,
            or_(Bill.is_delete.is_(False), Bill.is_delete.is_(None)),
        ))).all()
        assigned = assign_missing_balances(
            token,
            [{
                "request_id": row.request_id,
                "at": row.updated_at or row.created_at,
                "charged": float(row.charged_cny or 0),
                "balance": None if row.balance_cny is None else float(row.balance_cny),
            } for row in charge_rows],
            [(row.generate_time, float(row.money or 0)) for row in recharge_rows],
        )
        if not assigned:
            return 0
        for request_id, balance in assigned.items():
            await db.execute(update(RequestUsage).where(
                RequestUsage.request_id == request_id,
                RequestUsage.balance_cny.is_(None),
            ).values(balance_cny=balance))
        await db.commit()
        return len(assigned)


async def backfill_usage_balances() -> int:
    """给还没有扣后余额的已计费记录补上。可重复执行，只填空值。"""
    import os
    from settings.database import async_engine

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0
    try:
        async with async_engine.connect() as conn:
            locked = await conn.scalar(text(
                "SELECT GET_LOCK('claude_proxy_usage_balance_backfill', 0)"
            ))
            await conn.commit()
            if locked != 1:
                return 0
            try:
                async with AsyncSessionLocal() as db:
                    user_ids = (await db.execute(select(RequestUsage.user_id).where(
                        RequestUsage.status == "billed",
                        RequestUsage.balance_cny.is_(None),
                        RequestUsage.user_id.is_not(None),
                    ).distinct())).scalars().all()
                updated = 0
                for user_id in user_ids:
                    updated += await _backfill_user_balances(int(user_id))
                if updated:
                    process_log.info("usage balance backfill updated %s rows", updated)
                return updated
            finally:
                await conn.execute(text(
                    "SELECT RELEASE_LOCK('claude_proxy_usage_balance_backfill')"
                ))
                await conn.commit()
    except Exception:
        process_log.exception("usage balance backfill failed")
        return 0
