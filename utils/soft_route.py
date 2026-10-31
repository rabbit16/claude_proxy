"""临时软路由：同一模型对同一号商连续失败超过阈值后，短时全量切到备用号商。

- 规则落库（tb_soft_route），重启后仍在，无需重配。
- 触发状态与连续失败计数写 tb_soft_route_state，并在每个 worker 进程内保留镜像；
  进程重启时清空该表，临时路由随之消失（符合「重启后路由消失」）。
- model 支持完整名、`-` 边界前缀、`gemini*` 开头通配；from_vendor_code 为空表示任意号商。
"""
from __future__ import annotations

import asyncio
import datetime
import os
import time
from typing import Optional

from sqlalchemy import delete, select

from settings.database import AsyncSessionLocal
from settings.models import SoftRoute, SoftRouteState
from utils.get_log import process_log

# 触发后同一 (model, from_vendor) 的切换窗口
_rules: list[dict] = []
# (model, from_vendor_code) -> {"to_vendor_code": str, "active_until": float, "rule_id": int}
_active: dict[tuple[str, str], dict] = {}
# 本进程知道「有失败计数」的 key，成功时才需要回写 DB 清零，避免每个成功请求都写库
_fail_local: set[tuple[str, str]] = set()
# 本次服务启动的世代标记（多 worker 共享同一世代）
_run_epoch = ""
_worker_mark = ""


def _epoch() -> str:
    return _run_epoch


def _proc_start_ticks() -> str:
    """本进程启动时刻（/proc/<pid>/stat 第 22 列），用于区分 pid 复用。"""
    try:
        with open(f"/proc/{os.getpid()}/stat", encoding="utf-8") as fh:
            return fh.read().split()[21]
    except Exception:
        return ""


def _mark() -> str:
    return _worker_mark or f"{os.getpid()}:{_proc_start_ticks()}"


def _alive(mark: str) -> bool:
    """worker 标记对应进程是否仍存活。"""
    try:
        pid_text, _, ticks = (mark or "").partition(":")
        pid = int(pid_text)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if ticks:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                if fh.read().split()[21] != ticks:
                    return False
        except Exception:
            return False
    return True


async def claim_run_epoch() -> bool:
    """登记本 worker，并判断是否为「整个服务重启」。返回 True 表示需要清空状态。

    只有在没有任何其它存活 worker 时才视为新世代。单个 worker 重启不影响其它
    worker 已触发的临时切换；整个服务重启（所有 worker 同时起、旧进程都已退出）
    则换新世代，旧状态自然失效。
    """
    global _run_epoch, _worker_mark
    from settings.models import SoftRouteEpoch

    mark = f"{os.getpid()}:{_proc_start_ticks()}"
    _worker_mark = mark
    fresh_start = False
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SoftRouteEpoch).where(SoftRouteEpoch.id == 1).with_for_update()
        )).scalars().first()
        if row is None:
            row = SoftRouteEpoch(id=1, epoch="", worker_marks=[])
            db.add(row)
        marks = dict(row.worker_marks or {}) if isinstance(row.worker_marks, dict) else {}
        # 去掉已退出的 worker 标记（含自己上一次的记录）
        live = {key: value for key, value in marks.items() if key != mark and _alive(key)}
        if not row.epoch or not live:
            row.epoch = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
            fresh_start = True
        live[mark] = True
        row.worker_marks = live
        _run_epoch = row.epoch
        await db.commit()
    return fresh_start


async def release_run_epoch() -> None:
    """本 worker 退出时注销标记；最后一个 worker 退出后世代清空。"""
    global _run_epoch
    from settings.models import SoftRouteEpoch

    mark = _mark()
    try:
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SoftRouteEpoch).where(SoftRouteEpoch.id == 1).with_for_update()
            )).scalars().first()
            if row is not None:
                live = dict(row.worker_marks or {}) if isinstance(row.worker_marks, dict) else {}
                live.pop(mark, None)
                row.worker_marks = live
                if not live:
                    row.epoch = ""
                await db.commit()
    except Exception:
        process_log.exception("release soft route epoch failed")
    _run_epoch = ""

ACTIVE_POLL_SECONDS = 2.0


def _norm(model: str) -> str:
    from utils.model_route import _norm as route_norm
    return route_norm(model)


def _now_ts() -> float:
    return time.time()


def _rule_dict(row: SoftRoute) -> dict:
    return {
        "id": row.id,
        "model": (row.model or "").strip().lower(),
        "group_name": (row.group_name or "").strip(),
        "from_vendor_code": (row.from_vendor_code or "").strip().lower(),
        "to_vendor_code": (row.to_vendor_code or "").strip().lower(),
        "fail_threshold": int(row.fail_threshold or 2),
        "duration_seconds": int(row.duration_seconds or 300),
        "is_active": bool(row.is_active),
        "note": row.note or "",
    }


async def reload_soft_routes() -> list[dict]:
    """从 DB 热加载规则。按匹配强度排序，最长/最精确的规则优先。"""
    global _rules
    try:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(SoftRoute).order_by(SoftRoute.id))).scalars().all()
        loaded = [_rule_dict(row) for row in rows if row.is_active]
        loaded.sort(key=lambda item: -len(item["model"]))
        _rules = loaded
    except Exception:
        process_log.exception("reload soft routes failed")
    return list(_rules)


def list_soft_routes() -> list[dict]:
    return [dict(item) for item in _rules]


async def reload_active_states() -> None:
    """把 DB 里仍在生效的切换窗口同步到进程内镜像（多 worker 共享触发结果）。"""
    global _active
    try:
        now = datetime.datetime.now()
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(SoftRouteState).where(SoftRouteState.run_epoch == _epoch())
            )).scalars().all()
        active: dict[tuple[str, str], dict] = {}
        failed: set[tuple[str, str]] = set()
        for row in rows:
            key = ((row.model or "").strip().lower(), (row.from_vendor_code or "").strip().lower())
            if not key[0]:
                continue
            if row.fail_count and row.fail_count > 0:
                failed.add(key)
            if row.active_until and row.active_until > now:
                active[key] = {
                    "to_vendor_code": (row.to_vendor_code or "").strip().lower(),
                    "active_until": row.active_until.timestamp(),
                    "rule_id": row.rule_id,
                }
        _active = active
        # 不要 clear：本进程刚给某个 key 记了失败、但 DB 快照还没带上计数，
        # 一清空就会让接下来的成功请求漏掉清零，留一个永远清不掉的 fail_count。
        # 只做并集；note_success 成功回写后会自己 discard。
        _fail_local.update(failed)
    except Exception:
        process_log.exception("reload soft route states failed")


def active_states() -> list[dict]:
    """管理端展示用：当前生效中的临时切换。"""
    now = _now_ts()
    out = []
    for (model, from_vendor), state in list(_active.items()):
        remaining = int(state["active_until"] - now)
        if remaining <= 0:
            continue
        out.append({
            "model": model,
            "from_vendor_code": from_vendor,
            "to_vendor_code": state["to_vendor_code"],
            "remaining_seconds": remaining,
            "rule_id": state["rule_id"],
        })
    out.sort(key=lambda item: item["model"])
    return out


def match_rule(model: str, from_vendor: str) -> Optional[dict]:
    """按模型 + 当前号商找最合适的一条规则。from_vendor 为空时匹配任意号商。"""
    from utils.model_route import pattern_specificity

    model = _norm(model)
    from_vendor = (from_vendor or "").strip().lower()
    if not model:
        return None
    best = None
    best_score = None
    for item in _rules:
        if not item["is_active"]:
            continue
        if item["from_vendor_code"] and item["from_vendor_code"] != from_vendor:
            continue
        score = pattern_specificity(item["model"], model)
        if score is None:
            continue
        # 指定了来源号商比「任意号商」更精确
        ranked = (score[0], score[1], score[2], 1 if item["from_vendor_code"] else 0)
        if best_score is None or ranked > best_score:
            best = item
            best_score = ranked
    return best


def resolve_soft_route(model: str, from_vendor: str) -> Optional[dict]:
    """请求转发时查当前生效的切换。未命中返回 None。"""
    key = (_norm(model), (from_vendor or "").strip().lower())
    state = _active.get(key)
    if not state:
        return None
    if state["active_until"] <= _now_ts():
        _active.pop(key, None)
        return None
    return state


def is_failure_status(status_code: int) -> bool:
    """哪些上游结果计入「失败」：5xx、超时/限流、鉴权失效。请求本身有问题的 4xx 不计。"""
    code = int(status_code or 0)
    return code >= 500 or code in {401, 403, 408, 429}


async def _bump_failure(model: str, from_vendor: str, rule: dict) -> Optional[dict]:
    """原子自增失败计数；达到阈值就把切换窗口写进状态表。返回新触发的 state。"""
    from utils.model_route import _norm as route_norm

    model = route_norm(model)
    from_vendor = (from_vendor or "").strip().lower()
    now = datetime.datetime.now()
    triggered = None
    epoch = _epoch()
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SoftRouteState)
            .where(SoftRouteState.model == model, SoftRouteState.from_vendor_code == from_vendor)
            .with_for_update()
        )).scalars().first()
        if row is None:
            row = SoftRouteState(model=model, from_vendor_code=from_vendor, fail_count=0)
            db.add(row)
        if (row.run_epoch or "") != epoch:
            # 上一个世代（或未认领）的残留计数不参与本次启动
            row.fail_count = 0
            row.active_until = None
        row.run_epoch = epoch
        row.fail_count = int(row.fail_count or 0) + 1
        row.updated_at = now
        if row.fail_count >= int(rule["fail_threshold"]):
            row.rule_id = rule["id"]
            row.to_vendor_code = rule["to_vendor_code"]
            row.active_until = now + datetime.timedelta(seconds=int(rule["duration_seconds"]))
            row.fail_count = 0  # 触发后归零，窗口结束前不重复累积
            triggered = {
                "to_vendor_code": rule["to_vendor_code"],
                "active_until": row.active_until.timestamp(),
                "rule_id": rule["id"],
            }
            process_log.warning(
                "soft route activated model=%s from=%s to=%s for=%ss rule=%s",
                model, from_vendor or "*", rule["to_vendor_code"], rule["duration_seconds"], rule["id"],
            )
        await db.commit()
    return triggered


async def note_failure(model: str, from_vendor: str, status_code: int = 0) -> None:
    """记录一次失败。只有命中规则、且当前不在切换窗口内时才累计。"""
    try:
        model = _norm(model)
        from_vendor = (from_vendor or "").strip().lower()
        if not model:
            return
        # 用 resolve_soft_route 而不是直接查 _active：它会把已过期的窗口剔除，
        # 否则窗口到点后、supervisor 下一轮同步前，失败计数会被白白丢掉。
        if resolve_soft_route(model, from_vendor):
            return
        rule = match_rule(model, from_vendor)
        if not rule or not rule["to_vendor_code"]:
            return
        # 已经在备用号商上失败时不再累计：再切过去还是同一个目标，没有意义，
        # 反而会把「任意来源号商」的规则卡死在无解状态。from_vendor 为空时尤其要挡。
        if rule["to_vendor_code"] == from_vendor:
            return
        triggered = await _bump_failure(model, from_vendor, rule)
        _fail_local.add((model, from_vendor))
        if triggered:
            _active[(model, from_vendor)] = triggered
    except Exception:
        process_log.exception("soft route note failure failed model=%s vendor=%s", model, from_vendor)


async def note_failure_status(model: str, from_vendor: str, status_code: int = 0) -> None:
    """按上游状态码记录失败；不构成失败的状态码直接忽略。"""
    if not is_failure_status(status_code):
        return
    await note_failure(model, from_vendor, status_code)


async def note_success(model: str, from_vendor: str) -> None:
    """成功后清零连续失败计数；不取消已触发的切换窗口。"""
    try:
        model = _norm(model)
        from_vendor = (from_vendor or "").strip().lower()
        key = (model, from_vendor)
        if key not in _fail_local:
            return
        _fail_local.discard(key)
        async with AsyncSessionLocal() as db:
            await db.execute(
                SoftRouteState.__table__.update()
                .where(
                    SoftRouteState.model == model,
                    SoftRouteState.from_vendor_code == from_vendor,
                    SoftRouteState.run_epoch == _epoch(),
                )
                .values(fail_count=0, updated_at=datetime.datetime.now())
            )
            await db.commit()
    except Exception:
        process_log.exception("soft route note success failed model=%s vendor=%s", model, from_vendor)


async def reset_states() -> None:
    """清空触发状态：用于启动（新世代）与管理端手动解除。

    旧世代的残留本来就不参与转发，这里一并删掉，避免状态表长期堆垃圾行。
    """
    global _active
    _active = {}
    _fail_local.clear()
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(SoftRouteState))
            await db.commit()
    except Exception:
        process_log.exception("reset soft route states failed")


async def clear_active_states() -> int:
    """管理端手动解除全部临时切换，并把失败计数归零。返回解除条数。

    多 worker 下每个进程的内存镜像各不相同，清库之后要靠各进程自己的
    supervisor 下一轮 reload_active_states() 才发现「已经没切换了」，
    所以不能只清本地缓存就当作完成（本地 _active 仍会被 supervisor 覆盖回来）。
    """
    await reload_active_states()
    count = len(active_states())
    await reset_states()
    return count


def normalize_soft_route_models(models: Optional[list], fallback: str = "") -> list[str]:
    from utils.model_route import normalize_route_models
    return normalize_route_models(models, fallback=fallback)


async def upsert_soft_route(
    *,
    model: str,
    to_vendor_code: str,
    from_vendor_code: str = "",
    fail_threshold: int = 2,
    duration_seconds: int = 300,
    is_active: bool = True,
    note: str = "",
    route_id: Optional[int] = None,
    group_name: Optional[str] = None,
    models: Optional[list] = None,
) -> dict:
    """单条或分组批量写入规则。分组名相同视为同一组，一次对齐到本次模型集合。"""
    from utils.model_route import normalize_route_models
    from utils.vendor_catalog import resolve_vendor

    patterns = normalize_route_models(models, fallback=model)
    to_vendor = (to_vendor_code or "").strip().lower()
    from_vendor = (from_vendor_code or "").strip().lower()
    if not resolve_vendor(vendor=to_vendor):
        raise ValueError(f"unknown vendor: {to_vendor}")
    if from_vendor and not resolve_vendor(vendor=from_vendor):
        raise ValueError(f"unknown vendor: {from_vendor}")
    if from_vendor and from_vendor == to_vendor:
        raise ValueError("备用号商不能与来源号商相同")
    threshold = 2 if fail_threshold is None else int(fail_threshold)
    if threshold < 1 or threshold > 20:
        raise ValueError("连续失败阈值需在 1~20 之间")
    duration = 300 if duration_seconds is None else int(duration_seconds)
    if duration < 10 or duration > 86400:
        raise ValueError("路由时长需在 10~86400 秒之间")
    group = (group_name or "").strip()[:128]

    async with AsyncSessionLocal() as db:
        existing = list((await db.execute(select(SoftRoute))).scalars().all())
        by_model = {row.model: row for row in existing}
        target_row = next((row for row in existing if route_id and row.id == route_id), None)
        old_group = (getattr(target_row, "group_name", "") or "").strip() if target_row else ""

        def members_of(name: str) -> list[SoftRoute]:
            if not name:
                return []
            return [row for row in existing if (row.group_name or "").strip() == name]

        old_members = members_of(old_group) if old_group else ([target_row] if target_row else [])

        # 单条规则改名：直接改掉这一行的模型名。否则会按新名字建一条、
        # 旧名字那条原封不动留着，等于多出一条生效中的规则。
        if target_row is not None and not old_group and len(patterns) == 1 and target_row.model not in set(patterns):
            occupant = by_model.get(patterns[0])
            if occupant is not None and occupant.id != target_row.id:
                raise ValueError(f"model already soft-routed: {patterns[0]}")
            by_model.pop(target_row.model, None)
            target_row.model = patterns[0]
            by_model[patterns[0]] = target_row
            old_members = [target_row]

        for pattern in patterns:
            clash = by_model.get(pattern)
            if clash is not None and clash not in old_members and clash.id != route_id:
                raise ValueError(f"model already soft-routed: {pattern}")

        # 先处理分组改名，再清理本次不再包含的旧成员；顺序反了会把刚拆出去的
        # 成员又按新组名塞回来。
        if old_group and group and old_group != group:
            for row in old_members:
                row.group_name = group
        kept = set(patterns)
        for row in old_members:
            if row.model not in kept:
                row.group_name = ""

        saved_ids: list[int] = []
        for pattern in patterns:
            row = by_model.get(pattern)
            if row is None:
                row = SoftRoute(model=pattern)
                db.add(row)
            row.model = pattern
            row.group_name = group
            row.from_vendor_code = from_vendor
            row.to_vendor_code = to_vendor
            row.fail_threshold = threshold
            row.duration_seconds = duration
            row.is_active = bool(is_active)
            row.note = (note or "")[:200]
            await db.flush()
            saved_ids.append(row.id)
        await db.commit()

    await reload_soft_routes()
    saved = next((item for item in list_soft_routes() if item["id"] == saved_ids[0]), None)
    if saved is None:
        raise ValueError("soft route save failed")
    return saved


async def delete_soft_route(route_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        row = await db.get(SoftRoute, route_id)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
    await reload_soft_routes()
    return True


async def soft_route_supervisor(stop_event: asyncio.Event) -> None:
    """周期性同步规则和触发状态：其它 worker 的改动 / 触发 2s 内可见。"""
    while not stop_event.is_set():
        await reload_soft_routes()
        await reload_active_states()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ACTIVE_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
