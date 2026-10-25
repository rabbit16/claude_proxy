"""根据中转访问日志的 finish / result 归并代理任务。

同一账号、同一会话里，连续的 tool_use / function_call 算同一个还在进行的任务。
Claude 用 end_turn 结束。OpenAI 的 status=completed 只表示这次请求成功；
输出里还有 function_call 时，日志会记成 function_call，任务继续。
没有待执行工具的 completed / stop 才把任务收成已完成。
请求失败，或 result 为 error / incomplete / cancelled，记为失败。
"""
import asyncio
import contextvars
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select

from settings.database import AsyncSessionLocal
from settings.models import AgentTask, User
from utils.common_utils import AUTH_FAIL_REASONS, normalize_upstream_path
from utils.get_log import process_log
from utils.task_guard import finish_writes

TRACKED_PATHS = {"/v1/messages", "/v1/responses", "/v1/chat/completions"}
TOOL_FINISH = {"tool_use", "tool_calls", "function_call"}
DONE_FINISH = {"end_turn", "stop", "completed"}
FAIL_RESULTS = {
    "error", "failed", "upstream_error", "upstream_failed",
    "cancelled", "canceled", "incomplete", "filtered",
}
FAIL_FINISH = {"failed", "error", "incomplete", "cancelled", "canceled", "length", "max_tokens"}
CHAIN_GAP = timedelta(minutes=30)
STATUSES = ("running", "done", "failed")

_ctx = contextvars.ContextVar("agent_task_ctx", default=None)
_locks: dict[str, asyncio.Lock] = {}


def bind_agent_task(*, account: str, model: str, source: str, path: str, session_key: str):
    account = (account or "").strip()
    if not account or account in AUTH_FAIL_REASONS or account == "-":
        _ctx.set(None)
        return
    normalized = normalize_upstream_path(path or "")
    if normalized not in TRACKED_PATHS:
        _ctx.set(None)
        return
    _ctx.set({
        "account": account[:32],
        "model": (model or "")[:255],
        "source": (source or "")[:32],
        "path": normalized[:255],
        "session_key": (session_key or "")[:200],
    })


def extract_session_key(parsed_body) -> str:
    """优先用客户端自己带的会话号，把同一次代理任务串起来。"""
    if not isinstance(parsed_body, dict):
        return ""
    meta = parsed_body.get("metadata")
    if isinstance(meta, dict):
        for key in ("user_id", "session_id"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    for key in ("prompt_cache_key", "safety_identifier", "user"):
        value = parsed_body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    conversation = parsed_body.get("conversation")
    if isinstance(conversation, str) and conversation.strip():
        return conversation.strip()[:200]
    if isinstance(conversation, dict) and conversation.get("id"):
        return str(conversation.get("id"))[:200]
    return ""


def judge_task(finish: str, result: str, http_status: int) -> str:
    finish_text = (finish or "").strip().lower()
    result_text = (result or "").strip().lower()
    base = finish_text.split(":", 1)[0]
    if http_status >= 400 or result_text in FAIL_RESULTS or base in FAIL_FINISH:
        return "failed"
    if base in TOOL_FINISH or finish_text.startswith("tool"):
        return "running"
    if base in DONE_FINISH:
        return "done"
    if result_text == "completed":
        return "done"
    return "running"


def explain_task(status: str, finish: str, result: str) -> str:
    finish_text = (finish or "").strip() or "无"
    result_text = (result or "").strip() or "无"
    if status == "running":
        return f"最近一次 finish={finish_text}，模型还在调用工具，任务未结束"
    if status == "done":
        return f"最近一次 finish={finish_text}，这一轮任务已结束"
    return f"最近一次 finish={finish_text} result={result_text}，任务失败"


def schedule_agent_task(*, req_id: str, finish: str, result: str, http_status: int):
    ctx = _ctx.get()
    if not ctx:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(record_agent_task(
        ctx=dict(ctx),
        req_id=req_id,
        finish=finish or "",
        result=result or "",
        http_status=int(http_status or 0),
    ))


def _lock_for(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


async def record_agent_task(*, ctx: dict, req_id: str, finish: str, result: str, http_status: int):
    account = ctx.get("account") or ""
    if not account:
        return
    judged = judge_task(finish, result, http_status)
    decision = explain_task(judged, finish, result)[:300]
    lock_key = f"{account}|{ctx.get('session_key') or ''}|{ctx.get('model') or ''}|{ctx.get('source') or ''}"

    async def write():
        async with _lock_for(lock_key):
            async with AsyncSessionLocal() as db:
                now = datetime.now()
                row = await _find_open(db, ctx, now)
                user_id = await _user_id_for_account(db, account)
                if row is None:
                    row = AgentTask(
                        user_id=user_id,
                        account=account,
                        session_key=ctx.get("session_key") or "",
                        model=ctx.get("model") or "",
                        source=ctx.get("source") or "",
                        path=ctx.get("path") or "",
                        status=judged,
                        status_source="log",
                        suggested_status=judged,
                        finish=(finish or "")[:64],
                        result=(result or "")[:64],
                        decision=decision,
                        steps=1,
                        last_request_id=(req_id or "")[:32],
                        request_ids=(req_id or "")[:400],
                        http_status=http_status or None,
                        started_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    ids = [item for item in (row.request_ids or "").split(",") if item]
                    if req_id and req_id not in ids:
                        ids.append(req_id)
                    row.request_ids = ",".join(ids[-12:])[:400]
                    row.steps = int(row.steps or 0) + 1
                    row.model = ctx.get("model") or row.model
                    row.path = ctx.get("path") or row.path
                    row.finish = (finish or "")[:64]
                    row.result = (result or "")[:64]
                    row.suggested_status = judged
                    row.decision = decision
                    row.last_request_id = (req_id or "")[:32]
                    row.http_status = http_status or None
                    row.updated_at = now
                    if user_id and not row.user_id:
                        row.user_id = user_id
                    if row.status_source != "manual":
                        row.status = judged
                        row.status_source = "log"
                await db.commit()

    try:
        await finish_writes(write)
    except Exception:
        process_log.exception("agent task record failed req=%s", req_id)


async def _find_open(db, ctx: dict, now: datetime):
    since = now - CHAIN_GAP
    query = select(AgentTask).where(
        AgentTask.account == ctx.get("account"),
        AgentTask.is_delete.is_(False),
        AgentTask.status == "running",
        AgentTask.updated_at >= since,
    )
    session_key = ctx.get("session_key") or ""
    if session_key:
        query = query.where(AgentTask.session_key == session_key)
    else:
        query = query.where(
            or_(AgentTask.session_key == "", AgentTask.session_key.is_(None)),
            AgentTask.model == (ctx.get("model") or ""),
            AgentTask.source == (ctx.get("source") or ""),
        )
    return (await db.execute(
        query.order_by(AgentTask.updated_at.desc(), AgentTask.id.desc()).limit(1)
    )).scalar_one_or_none()


async def _user_id_for_account(db, account: str) -> Optional[int]:
    """账号通常是手机号；匿名用户的计费键是主键字符串，只在电话为空时按主键匹配。"""
    user_id = await db.scalar(select(User.user_id).where(
        User.telephone == account,
        or_(User.is_delete.is_(False), User.is_delete.is_(None)),
    ))
    if user_id:
        return int(user_id)
    if not (account or "").isdigit():
        return None
    user_id = await db.scalar(select(User.user_id).where(
        User.user_id == int(account),
        or_(User.telephone.is_(None), User.telephone == ""),
        or_(User.is_delete.is_(False), User.is_delete.is_(None)),
    ))
    return int(user_id) if user_id else None
