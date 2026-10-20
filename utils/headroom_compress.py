# -*- coding: utf-8 -*-
"""在转发上游前压缩请求上下文，降低 billed input tokens。

只改 messages / Responses input，不碰 system、tools、图片。失败则原样转发。
压缩结果必须可重复：同样的历史每次压成同样的文本，才能保住 prompt cache。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any, Callable, Dict, Optional, Tuple

from settings.macro import HEADROOM_ENABLED, HEADROOM_KOMPRESS, HEADROOM_MIN_TOKENS
from utils.common_utils import is_audio_chat_completion, is_audio_path, is_billable_path, is_image_path, normalize_upstream_path
from utils.get_log import process_log

os.environ.setdefault("HEADROOM_BEACON", "off")
os.environ.setdefault("HEADROOM_TIKTOKEN_LOAD_TIMEOUT_SECONDS", "3")

CompressFn = Callable[..., Any]

_compress_fn: Any = None
_compress_config_cls: Any = None
_import_tried = False
_stats_lock = threading.Lock()
_stats = {
    "requests": 0,
    "requests_saved": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "tokens_saved": 0,
}


def record_compression(metrics: Optional[Dict[str, Any]]) -> None:
    if not metrics:
        return
    before = int(metrics.get("tokens_before") or 0)
    after = int(metrics.get("tokens_after") or 0)
    saved = int(metrics.get("tokens_saved") or 0)
    with _stats_lock:
        _stats["requests"] += 1
        _stats["tokens_before"] += before
        _stats["tokens_after"] += after
        _stats["tokens_saved"] += saved
        if saved > 0:
            _stats["requests_saved"] += 1


def get_stats() -> Dict[str, Any]:
    with _stats_lock:
        before = _stats["tokens_before"]
        saved = _stats["tokens_saved"]
        after = _stats["tokens_after"]
        snapshot = dict(_stats)
    ratio = (saved / before) if before else 0.0
    snapshot.update(
        {
            "enabled": headroom_enabled(),
            "compression_ratio": ratio,
            "savings_percent": round(ratio * 100, 2),
        }
    )
    return snapshot


def reset_stats() -> None:
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0


def _load_headroom() -> Tuple[Optional[CompressFn], Any]:
    global _compress_fn, _compress_config_cls, _import_tried
    if _import_tried:
        return _compress_fn, _compress_config_cls
    _import_tried = True
    try:
        from headroom import CompressConfig, compress
    except Exception as exc:
        process_log.warning("headroom import failed, compression disabled: %s", exc)
        return None, None
    _compress_fn = compress
    _compress_config_cls = CompressConfig
    process_log.info("headroom compress loaded")
    return _compress_fn, _compress_config_cls


def headroom_enabled() -> bool:
    return bool(HEADROOM_ENABLED)


def _should_compress(method: str, path: str, parsed_body: dict) -> bool:
    if not headroom_enabled():
        return False
    if (method or "").upper() not in {"POST", "PUT"}:
        return False
    if not isinstance(parsed_body, dict):
        return False
    if is_image_path(path) or is_audio_path(path) or is_audio_chat_completion(path, parsed_body.get("model")) or not is_billable_path(path):
        return False
    try:
        from utils.common_utils import is_gemini_native_path
        if is_gemini_native_path(path):
            return False
    except Exception:
        pass
    normalized = normalize_upstream_path(path)
    if normalized.startswith("/v1/responses") and parsed_body.get("previous_response_id"):
        return False
    return True


def _text_from_content(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if item.get("type") in {"text", "input_text", "output_text"} and isinstance(text, str):
            parts.append(text)
        elif isinstance(text, str) and item.get("type") not in {"input_image", "image", "image_url"}:
            parts.append(text)
    return "\n".join(parts) if parts else None


def _write_text_content(original: Any, new_text: str) -> Any:
    if isinstance(original, str) or original is None:
        return new_text
    if not isinstance(original, list):
        return new_text
    written = False
    extra_indexes = []
    for idx, item in enumerate(original):
        if isinstance(item, str):
            if not written:
                original[idx] = new_text
                written = True
            else:
                extra_indexes.append(idx)
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"input_image", "image", "image_url"}:
            continue
        if kind in {"text", "input_text", "output_text"} or isinstance(item.get("text"), str):
            if not written:
                item["text"] = new_text
                written = True
            else:
                extra_indexes.append(idx)
    for idx in reversed(extra_indexes):
        original.pop(idx)
    return original if written else new_text


def _responses_to_messages(input_items: list) -> Tuple[list, list]:
    messages = []
    slots = []
    for idx, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call_output":
            text = _text_from_content(item.get("output"))
            if not text:
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or str(idx),
                    "content": text,
                }
            )
            slots.append(("output", idx))
    return messages, slots


def _apply_responses_result(input_items: list, slots: list, compressed: list) -> None:
    for message, (field, idx) in zip(compressed, slots):
        item = input_items[idx]
        new_text = message.get("content")
        if not isinstance(new_text, str):
            new_text = _text_from_content(new_text)
        if new_text is None:
            continue
        item[field] = _write_text_content(item.get(field), new_text)


def _build_config(config_cls: Any):
    kwargs = {
        "compress_user_messages": False,
        "compress_system_messages": False,
        "protect_recent": 0,
        "protect_analysis_context": True,
        "frozen_message_count": 0,
        "min_tokens_to_compress": HEADROOM_MIN_TOKENS,
    }
    if not HEADROOM_KOMPRESS:
        kwargs["kompress_model"] = "disabled"
    fields = getattr(config_cls, "__dataclass_fields__", None)
    if isinstance(fields, dict):
        kwargs = {key: value for key, value in kwargs.items() if key in fields}
    try:
        return config_cls(**kwargs)
    except TypeError:
        kwargs.pop("kompress_model", None)
        kwargs.pop("frozen_message_count", None)
        return config_cls(**kwargs)


def _run_compress(messages: list, model: str) -> Any:
    compress_fn, config_cls = _load_headroom()
    if compress_fn is None:
        return None
    kwargs = {"model": model or "gpt-4o"}
    if config_cls is not None:
        kwargs["config"] = _build_config(config_cls)
    return compress_fn(messages, **kwargs)


def _metrics_from_result(result: Any) -> Dict[str, Any]:
    tokens_before = int(getattr(result, "tokens_before", 0) or 0)
    tokens_after = int(getattr(result, "tokens_after", 0) or 0)
    tokens_saved = int(getattr(result, "tokens_saved", 0) or 0)
    ratio = getattr(result, "compression_ratio", 0.0) or 0.0
    transforms = list(getattr(result, "transforms_applied", None) or [])
    return {
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "tokens_saved": tokens_saved,
        "compression_ratio": float(ratio),
        "transforms": transforms,
    }


def compress_payload(payload: dict, source: str, model: str) -> Tuple[dict, Optional[Dict[str, Any]]]:
    """原地压缩 payload，成功时返回 metrics，无需压缩或失败时 metrics 为 None。"""
    if source == "responses":
        input_items = payload.get("input")
        if isinstance(input_items, str):
            return payload, None
        if not isinstance(input_items, list) or not input_items:
            return payload, None
        messages, slots = _responses_to_messages(input_items)
        if not messages:
            return payload, None
        result = _run_compress(messages, model)
        compressed = getattr(result, "messages", None) if result is not None else None
        if not result or not isinstance(compressed, list) or len(compressed) != len(messages):
            return payload, None
        metrics = _metrics_from_result(result)
        if metrics["tokens_saved"] <= 0:
            return payload, metrics
        _apply_responses_result(input_items, slots, compressed)
        return payload, metrics

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload, None
    result = _run_compress(messages, model)
    compressed = getattr(result, "messages", None) if result is not None else None
    if not result or not isinstance(compressed, list) or len(compressed) != len(messages):
        return payload, None
    metrics = _metrics_from_result(result)
    if metrics["tokens_saved"] <= 0:
        return payload, metrics
    payload["messages"] = compressed
    return payload, metrics


def compress_request_body(
    method: str,
    path: str,
    source: str,
    body: bytes,
    parsed_body: dict,
    model: str,
    req_id: str = "-",
) -> Tuple[bytes, dict, Optional[Dict[str, Any]]]:
    if not body or not _should_compress(method, path, parsed_body):
        return body, parsed_body, None
    try:
        working = json.loads(json.dumps(parsed_body, ensure_ascii=False))
    except Exception:
        return body, parsed_body, None
    try:
        working, metrics = compress_payload(working, source, model)
    except Exception:
        process_log.warning("headroom compress failed req=%s, forwarding original", req_id, exc_info=True)
        return body, parsed_body, None
    if not metrics:
        return body, parsed_body, None
    record_compression(metrics)
    if metrics.get("tokens_saved", 0) <= 0:
        process_log.info(
            "headroom skip req=%s source=%s model=%s saved=0 before=%s",
            req_id,
            source,
            model,
            metrics.get("tokens_before", 0),
        )
        return body, parsed_body, metrics
    try:
        new_body = json.dumps(working, ensure_ascii=False).encode("utf-8")
    except Exception:
        process_log.exception("headroom serialize failed req=%s", req_id)
        return body, parsed_body, None
    process_log.info(
        "headroom saved req=%s source=%s model=%s before=%s after=%s saved=%s ratio=%.2f transforms=%s",
        req_id,
        source,
        model,
        metrics.get("tokens_before", 0),
        metrics.get("tokens_after", 0),
        metrics.get("tokens_saved", 0),
        metrics.get("compression_ratio", 0),
        ",".join(metrics.get("transforms") or []) or "-",
    )
    return new_body, working, metrics


def warmup_headroom() -> None:
    if not headroom_enabled():
        return
    fn, _cfg = _load_headroom()
    if fn is None:
        return
    sample = [
        {
            "role": "tool",
            "tool_call_id": "warmup",
            "content": json.dumps({"results": [{"id": i, "ok": True} for i in range(20)]}),
        }
    ]
    try:
        _run_compress(sample, "gpt-4o")
        process_log.info("headroom warmup done")
    except Exception:
        process_log.exception("headroom warmup failed")


async def maybe_compress_request(
    method: str,
    path: str,
    source: str,
    body: bytes,
    parsed_body: dict,
    model: str,
    req_id: str = "-",
) -> Tuple[bytes, dict, Optional[Dict[str, Any]]]:
    if not _should_compress(method, path, parsed_body):
        return body, parsed_body, None
    return await asyncio.to_thread(
        compress_request_body,
        method,
        path,
        source,
        body,
        parsed_body,
        model,
        req_id,
    )
