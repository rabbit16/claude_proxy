"""把历史轮次的图片块替换成模型自己写下的图片笔记。

机制：
1. 提示词要求模型看图后，在回复里输出约定标记块
   ``<image_memo>…</image_memo>``（也认 ``<!-- image_memo: … -->``）。
2. 下一轮客户端把 assistant 原文回传，这里就在 history 里把对应的图片块
   换成那段笔记正文。

为什么这样不会反复打穿前缀缓存：
- 笔记取自客户端回传的历史原文，同一段对话每轮算出来的替换字节完全相同，
  是内容的纯函数；
- 只替换最后一个 user 消息之前的历史，当前轮新上传的图片原样发给上游；
- 没有笔记的消息一律不动，宁可不省也不降质量。
替换生效那一轮前缀会错一次，此后恢复命中。
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional

from settings.macro import IMAGE_MEMO_MAX_CHARS, IMAGE_MEMO_MIN_IMAGES

CHAT_SOURCES = ("anthropic", "openai", "responses")

# `<image_memo>...</image_memo>` 与 `<!-- image_memo: ... -->` 两种形态都认。
MEMO_RE = re.compile(
    r"<image_memo>\s*(?P<tagged>.*?)\s*</image_memo>"
    r"|<!--\s*image_memo\s*:\s*(?P<commented>.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)

REPLACEMENT_HEADER = "[图片内容已转写为文字，原始图片不再重复传输]"

_IMAGE_TYPES = {
    "anthropic": "image",
    "openai": "image_url",
    "responses": "input_image",
}
_TEXT_TYPES = {
    "anthropic": "text",
    "openai": "text",
    "responses": "input_text",
}
# Responses 的 input 里，user/assistant 消息可能省略 type（默认 message）。
_MESSAGE_DEFAULT_TYPES = {None, "", "message"}


def extract_memos(text: Any) -> str:
    """取出文本里全部图片笔记，按出现顺序用空行拼接。"""
    if not isinstance(text, str) or not text:
        return ""
    pieces = []
    for match in MEMO_RE.finditer(text):
        body = match.group("tagged") or match.group("commented") or ""
        body = body.strip()
        if body:
            pieces.append(body)
    return "\n\n".join(pieces)


def _truncate(text: str) -> str:
    limit = int(IMAGE_MEMO_MAX_CHARS or 0)
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def message_text(message: dict) -> str:
    """把消息里的所有文本部分拼起来，供查找 assistant 笔记。"""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _image_indexes(content: list, source: str) -> list[int]:
    kind = _IMAGE_TYPES.get(source, "image")
    found = []
    for index, part in enumerate(content):
        if isinstance(part, dict) and part.get("type") == kind:
            found.append(index)
    return found


def _build_text_part(cache_control: Optional[dict], text: str, source: str) -> dict:
    part: dict[str, Any] = {"type": _TEXT_TYPES.get(source, "text"), "text": text}
    if isinstance(cache_control, dict) and cache_control:
        part["cache_control"] = dict(cache_control)
    return part


def _replace_message_images(message: dict, memo: str, source: str) -> Optional[dict]:
    """把一条消息里的全部图片合并成一个文本块；没图片返回 None。"""
    content = message.get("content")
    if not isinstance(content, list):
        return None
    image_indexes = _image_indexes(content, source)
    if not image_indexes:
        return None
    # cache_control 挂在第一个图片块上时，迁移到新文本块，保住客户端缓存断点语义。
    cache_control = None
    first_image = content[image_indexes[0]]
    if isinstance(first_image, dict) and isinstance(first_image.get("cache_control"), dict):
        cache_control = first_image["cache_control"]
    text_part = _build_text_part(cache_control, f"{REPLACEMENT_HEADER}\n{memo}", source)
    updated_parts = []
    inserted = False
    for index, part in enumerate(content):
        if index in image_indexes:
            if not inserted:
                updated_parts.append(text_part)
                inserted = True
            continue
        updated_parts.append(part)
    updated = dict(message)
    updated["content"] = updated_parts
    return updated


def _last_user_index(messages: list) -> Optional[int]:
    """最后一个由用户发起、而非工具回填的消息下标。

    Claude Code 一轮里通常还有若干条 ``tool_result`` 的 user 消息，它们属于
    当前轮，不能当作历史边界，否则会把本轮刚上传的图片一起换掉。
    """
    found = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if message.get("type") not in _MESSAGE_DEFAULT_TYPES:
            continue
        if _is_tool_result_only(message):
            continue
        found = index
    return found


def _is_tool_result_only(message: dict) -> bool:
    """整条 user 消息都只是工具回填（Anthropic 的 tool_result 块）。"""
    content = message.get("content")
    if not isinstance(content, list) or not content:
        return False
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_result":
            return False
    return True


def rewrite_history_images(
    parsed_body: Any,
    source: str,
    *,
    enabled: bool = True,
) -> tuple[Any, int, int]:
    """返回 (parsed_body, 替换消息数, 笔记字符数)。

    没命中笔记、或命中数不足阈值时原对象原样返回，保证零改动时字节不变。
    """
    if not enabled or not isinstance(parsed_body, dict) or source not in CHAT_SOURCES:
        return parsed_body, 0, 0
    if source == "responses" and parsed_body.get("previous_response_id"):
        # 历史在服务端，请求体里没有可替换的图片。
        return parsed_body, 0, 0

    messages = parsed_body.get("messages") if source != "responses" else parsed_body.get("input")
    if not isinstance(messages, list) or not messages:
        return parsed_body, 0, 0
    history_end = _last_user_index(messages)
    if history_end is None or history_end <= 0:
        return parsed_body, 0, 0

    updated_messages = list(messages)
    replaced = 0
    memo_chars = 0
    for start, end in _segment_ranges(messages, history_end, source):
        memo = ""
        for index in range(start, end):
            candidate = messages[index]
            if not isinstance(candidate, dict) or candidate.get("role") != "assistant":
                continue
            memo = extract_memos(message_text(candidate))
            if memo:
                break
        if not memo:
            continue
        memo = _truncate(memo)
        target = messages[start]
        updated = _replace_message_images(target, memo, source)
        if not updated:
            continue
        updated_messages[start] = updated
        replaced += 1
        memo_chars += len(memo)

    if replaced < max(int(IMAGE_MEMO_MIN_IMAGES or 0), 1):
        return parsed_body, 0, 0
    result = dict(parsed_body)
    if source == "responses":
        result["input"] = updated_messages
    else:
        result["messages"] = updated_messages
    return result, replaced, memo_chars


def _segment_ranges(messages: list, history_end: int, source: str) -> Iterator[tuple[int, int]]:
    image_type = _IMAGE_TYPES.get(source, "")
    current_start: Optional[int] = None
    for index in range(history_end):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        has_image = isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == image_type for part in content
        )
        if not has_image:
            continue
        if current_start is None:
            current_start = index
            continue
        # 又一条带图 user 消息 → 上一段到此为止（不含它自己）。
        yield current_start, index
        current_start = index
    if current_start is not None:
        yield current_start, history_end