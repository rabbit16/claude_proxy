"""把写库段从请求取消域里摘出来。

流式响应在客户端断开时，uvicorn/anyio 会取消请求任务，取消信号可能正好
打在 commit / rollback 的 await 上。事务被中途打断后连接没有归还连接池，
SQLAlchemy 只能等垃圾回收时强杀，日志里就是
"non-checked-in connection ... which will be terminated"。

不能用 Task.uncancel() 重跑：anyio 的取消作用域会对同一任务反复重发取消
（实测取消计数会累加到 5），uncancel 一次抵消不了。这里改成把写库放进
独立的 asyncio 任务执行——新任务不在原取消作用域里，取消打不到它；
调用方等写库结束后，在原 cancel 点把取消继续抛出去。
"""
from __future__ import annotations

import asyncio
import contextvars
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


async def finish_writes(operation: Callable[[], Awaitable[T]]) -> T:
    """执行整块写库操作，不受调用方取消影响。

    operation 必须自带会话生命周期（async with ... 提交/回滚），
    这样才能保证事务完整结束、连接归还连接池。
    写库被取消打断时，等写库跑完再让取消继续向上传播。
    """
    loop = asyncio.get_running_loop()
    # 复制上下文：保留 contextvar（如 req_id）不随任务切换丢失。
    task = asyncio.create_task(operation(), context=contextvars.copy_context())
    finished = loop.create_future()

    def _mark_done(*_args):
        if not finished.done():
            finished.set_result(None)

    task.add_done_callback(_mark_done)
    cancelled = False
    while not finished.done():
        try:
            await asyncio.shield(finished)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError() from None
    if task.cancelled():
        raise asyncio.CancelledError() from None
    exc = task.exception()
    if exc is not None:
        raise exc
    return task.result()
