# -*- coding: utf-8 -*-
# @Time    : 2023/7/3 11:44 上午
# @Author  : rabbit
# @File    : macro.py
# @Software: PyCharm
import logging
import os
from pathlib import Path

# from utils.get_log import log_create

current_dir = os.path.dirname(__file__)

DEFAULT_EXPIRE_TIME = 1 * 24 * 60 * 60
LOGIN_FAILURE_TIME = 1 * 24 * 60 * 60
# LOGIN_FAILURE_TIME = 10*60
SAFE_LEN = 16
GPT_URL = "https://api.binjie.fun/api/generateStream"
TOKEN_URL = "http://127.0.0.1:8889/openaiKey"
# 号商 OpenAI 兼容网关。留空（或只空白）则回退原来的本地 LiteLLM：
# - 上游地址用 LEGACY_UPSTREAM_BASE
# - 仍用 CLAUDE_GPT_KEY 转发
# - 不做标准名 → -hc 模型覆盖
# - 不做 Anthropic→chat/completions 桥接（LiteLLM 本身可接 /v1/messages）
# 环境变量 YUNXIN_UPSTREAM_BASE 优先于下面硬编码。
_YUNXIN_UPSTREAM_RAW = os.environ.get(
    "YUNXIN_UPSTREAM_BASE",
    "",  # 留空=走本地 LiteLLM；要号商时填 https://ai.yunxinapi.com/hub
)
YUNXIN_UPSTREAM_BASE = (_YUNXIN_UPSTREAM_RAW or "").strip().rstrip("/")
LEGACY_UPSTREAM_BASE = os.environ.get("LEGACY_UPSTREAM_BASE", "http://127.0.0.1:4001").strip().rstrip("/")
# 默认接 TeamoRouter；显式设置 UPSTREAM_PROVIDER=legacy/yunxin 可切回旧上游。
UPSTREAM_PROVIDER = os.environ.get("UPSTREAM_PROVIDER", "teamo").strip().lower()
if UPSTREAM_PROVIDER not in {"teamo", "legacy", "yunxin"}:
    raise ValueError("UPSTREAM_PROVIDER must be teamo, legacy or yunxin")
TEAMO_UPSTREAM = UPSTREAM_PROVIDER == "teamo"
VENDOR_UPSTREAM = UPSTREAM_PROVIDER == "yunxin"
TEAMO_UPSTREAM_BASE = os.environ.get("TEAMO_UPSTREAM_BASE", "https://api.teamorouter.cn").strip().rstrip("/")
# 仅远端直连受阻时设置，如 http://127.0.0.1:7890；留空则直连。
TEAMO_HTTP_PROXY = os.environ.get("TEAMO_HTTP_PROXY", "").strip() or None
UPSTREAM_BASE = (
    TEAMO_UPSTREAM_BASE if TEAMO_UPSTREAM else
    YUNXIN_UPSTREAM_BASE if VENDOR_UPSTREAM else LEGACY_UPSTREAM_BASE
)
if VENDOR_UPSTREAM and not YUNXIN_UPSTREAM_BASE:
    raise ValueError("YUNXIN_UPSTREAM_BASE is required for yunxin")
if not UPSTREAM_BASE or UPSTREAM_BASE.endswith("/v1"):
    raise ValueError("upstream base must be the API root without /v1")
LITELLM_BASE = UPSTREAM_BASE  # 兼容旧引用名
IMAGE_UPSTREAM_BASE = UPSTREAM_BASE
DEFAULT_IMAGE_MODEL = "gpt-image-2"

# 号商实际模型 id（/hub/v1/models）。标准名 → 带 -hc 的映射见 common_utils。
YUNXIN_VENDOR_MODELS = (
    "claude-fable-5-hc",
    "claude-haiku-4-5-20251001-hc",
    "claude-opus-4-6-hc",
    "claude-opus-4-7-hc",
    "claude-opus-4-8-hc",
    "claude-opus-5-hc",
    "claude-sonnet-4-6-hc",
    "claude-sonnet-5-hc",
    "deepseek-flash",
    "deepseek-v4-flash-prime",
    "deepseek-v4-pro-prime",
    "gpt-5.4-hc",
    "gpt-5.5-hc",
    "gpt-5.6-luna-hc",
    "gpt-5.6-sol-hc",
    "gpt-5.6-terra-hc",
    "gpt-6-astra-hc",
    "gpt-6-luna-hc",
    "gpt-6-sol-hc",
    "gpt-image-2-hc",
    "kimi-k3",
)

# Headroom 默认关闭。压缩会改写历史工具结果，打断上游 prompt cache 前缀。
# 需要时再设 HEADROOM_ENABLED=1。
HEADROOM_ENABLED = os.environ.get("HEADROOM_ENABLED", "0").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}
# 默认只用确定性结构压缩，避免 Kompress 拉模型、也避免非确定性改写打穿 prompt cache。
HEADROOM_KOMPRESS = os.environ.get("HEADROOM_KOMPRESS", "0").strip().lower() in {
    "1",
    "true",
    "on",
    "yes",
}
HEADROOM_MIN_TOKENS = int(os.environ.get("HEADROOM_MIN_TOKENS", "250"))

ONLINE = 0
POOL_RECYCLE_TIME = 3600
from settings.capacity import CAPACITY
# 每 worker 连接数；云主机 2C/1.8G 会自动收到 4+4。
MAX_POOL_SIZE = CAPACITY["pool_size"]
MAX_OVERFLOW = CAPACITY["pool_overflow"]
POOL_TIME_OUT = 10
NGINX_ROUTER = "apiAI"
CONTEXT_TIME_OUT = 10

# PLUS_USER_MONEY = 30
PLUS_USER_MONEY = 50  # gpt4普通会员
# REGULAR_USER_MONEY = 10
REGULAR_USER_MONEY = 1  # 一块钱体验
# REGULAR_USER_COUNT_MONEY = 5
REGULAR_USER_COUNT_MONEY = 10  # 够用几次了
# PLUS_USER_COUNT_MONEY = 10
PLUS_USER_COUNT_MONEY = 20  # 意思一下
# PLUS_USER_YEAR_MONEY = 319.99  # gpt4年度会员
PLUS_USER_YEAR_MONEY = 100  # gpt4年度会员

REGULAR_USER_COUNT_BY_TIME = 20  # 单次购买3.5花的钱
REGULAR_USER_COUNT_NUM = 30  # 会员用户每天多少次
REGULAR_USER_PLUS_NUM = 4  # 会员用户每天gpt4多少次
PLUS_USER_PLUS_NUM = 15  # 会员用户每天gpt4多少次

SUPER_USER_ID_LIST = [39, 218, 116]

GPT_AUTH_DICT = {
    # "hhjjj0355@gmail.com": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6Ik1UaEVOVUpHTkVNMVFURTRNMEZCTWpkQ05UZzVNRFUxUlRVd1FVSkRNRU13UmtGRVFrRXpSZyJ9.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL3Byb2ZpbGUiOnsiZW1haWwiOiJoaGpqajAzNTVAZ21haWwuY29tIiwiZW1haWxfdmVyaWZpZWQiOnRydWV9LCJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsicG9pZCI6Im9yZy1FS3E3VW04eHI4cUUxTWF6dVZ6R2ZXZHciLCJ1c2VyX2lkIjoidXNlci0zcnlSdW04cEpJSVpOZTg0Wk5xemtHbGEifSwiaXNzIjoiaHR0cHM6Ly9hdXRoMC5vcGVuYWkuY29tLyIsInN1YiI6Imdvb2dsZS1vYXV0aDJ8MTE2MDEyODI5Njc4MTE5MzA4ODA4IiwiYXVkIjpbImh0dHBzOi8vYXBpLm9wZW5haS5jb20vdjEiLCJodHRwczovL29wZW5haS5vcGVuYWkuYXV0aDBhcHAuY29tL3VzZXJpbmZvIl0sImlhdCI6MTcwNDY4MTc3MCwiZXhwIjoxNzA1NTQ1NzcwLCJhenAiOiJUZEpJY2JlMTZXb1RIdE45NW55eXdoNUU0eU9vNkl0RyIsInNjb3BlIjoib3BlbmlkIHByb2ZpbGUgZW1haWwgbW9kZWwucmVhZCBtb2RlbC5yZXF1ZXN0IG9yZ2FuaXphdGlvbi5yZWFkIG9yZ2FuaXphdGlvbi53cml0ZSBvZmZsaW5lX2FjY2VzcyJ9.jP750VbE3CKMXPh6vqMYry13q_LcTiEOKtF65-EVVfb6XkXg0qW5Jp2Aquu1euNVjiWjmEx959XCI8ZXzYz2o4a3_2SxgMjP5Uyhspap0nU28Y9j_nkuMomy-IqaF0rep7un1Ze50cwOmemAeDg0b6piidnwaO9lWtmicDo57f-zK9hXf8hHd964kD3pnx79g5kwqvGmDJ6kyoxFH6ObeNir9k8toaC3M8LvNMLV34wJApINzm-lQ83vd0JvYxr61qJuuI1sEWT0Ceoa7IJTP3QcHL94Ki09onV_uGYESVTN8c9dtvpvrAiZzbZtW6vQVmbu-YuYpzmY234evCi05w",    # "nengyangjin@gmail.com": ""
    "xdfqflrt@ezztt.com": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6Ik1UaEVOVUpHTkVNMVFURTRNMEZCTWpkQ05UZzVNRFUxUlRVd1FVSkRNRU13UmtGRVFrRXpSZyJ9.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL3Byb2ZpbGUiOnsiZW1haWwiOiJ4ZGZxZmxydEBlenp0dC5jb20iLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZX0sImh0dHBzOi8vYXBpLm9wZW5haS5jb20vYXV0aCI6eyJwb2lkIjoib3JnLXh6enRBeEdPM1hsVWJRQkI2cmh3SGdENyIsInVzZXJfaWQiOiJ1c2VyLThiMUNubTgzbDdmYnF5SnVmTG9acjNFRCJ9LCJpc3MiOiJodHRwczovL2F1dGgwLm9wZW5haS5jb20vIiwic3ViIjoiYXV0aDB8NjU4ODFkODU3NmNlMjViOTYyZjMzMzE1IiwiYXVkIjpbImh0dHBzOi8vYXBpLm9wZW5haS5jb20vdjEiLCJodHRwczovL29wZW5haS5vcGVuYWkuYXV0aDBhcHAuY29tL3VzZXJpbmZvIl0sImlhdCI6MTcwNTI3ODc2MywiZXhwIjoxNzA2MTQyNzYzLCJhenAiOiJUZEpJY2JlMTZXb1RIdE45NW55eXdoNUU0eU9vNkl0RyIsInNjb3BlIjoib3BlbmlkIHByb2ZpbGUgZW1haWwgbW9kZWwucmVhZCBtb2RlbC5yZXF1ZXN0IG9yZ2FuaXphdGlvbi5yZWFkIG9yZ2FuaXphdGlvbi53cml0ZSBvZmZsaW5lX2FjY2VzcyJ9.IjFgmLgsvknJMjpp6FNU45CXRgG6hpLxdc63PUx3nOghrUtBqOwY6y6O-n8XOjqPDp4YRRABnjkPnJssy6pQWzVxyRwvYD0_nSqVHIHPQM3ErkMa5QA9I0c55CkDDFxT-doobkOkhPBJ6YkYCQB2mpNugSJBVwILBawTQdIdhD4sdIjYNGmmyWSa8T4Tfi8WVySxKwZeOCeFSAWr6UMal_fqcy_eM3rca5fCxsLLixuu8C8tgfxohTj5LHYWVHse_DFvYhS548o3tHV1wPaSbFb6sbP-MyP71tk96cO6TFx91aNYVGtaXt17zdBPFOtoFDQEGmdZ43slNfkKs8RZNg",
    "chkgfmbm@ezztt.com": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCIsImtpZCI6Ik1UaEVOVUpHTkVNMVFURTRNMEZCTWpkQ05UZzVNRFUxUlRVd1FVSkRNRU13UmtGRVFrRXpSZyJ9.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL3Byb2ZpbGUiOnsiZW1haWwiOiJjaGtnZm1ibUBlenp0dC5jb20iLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZX0sImh0dHBzOi8vYXBpLm9wZW5haS5jb20vYXV0aCI6eyJwb2lkIjoib3JnLVRHRUJWUlQ4SHRuc2NJU0hZRHB2ejRqTiIsInVzZXJfaWQiOiJ1c2VyLTFTdzhKR296ODRaaExBZXpKWWdFNXQ1YiJ9LCJpc3MiOiJodHRwczovL2F1dGgwLm9wZW5haS5jb20vIiwic3ViIjoiYXV0aDB8NjU4ODFjNDg4OTM2NTU0NDVlOTZhMjUyIiwiYXVkIjpbImh0dHBzOi8vYXBpLm9wZW5haS5jb20vdjEiLCJodHRwczovL29wZW5haS5vcGVuYWkuYXV0aDBhcHAuY29tL3VzZXJpbmZvIl0sImlhdCI6MTcwNTI3NjI5MywiZXhwIjoxNzA2MTQwMjkzLCJhenAiOiJUZEpJY2JlMTZXb1RIdE45NW55eXdoNUU0eU9vNkl0RyIsInNjb3BlIjoib3BlbmlkIHByb2ZpbGUgZW1haWwgbW9kZWwucmVhZCBtb2RlbC5yZXF1ZXN0IG9yZ2FuaXphdGlvbi5yZWFkIG9yZ2FuaXphdGlvbi53cml0ZSBvZmZsaW5lX2FjY2VzcyJ9.0x9ddyPkQS5oibnOphXdv4AJVLQmqtS3Qzt6Nvtge9pO83X7n1mgoTgTFToFL38q-N9LB-uaDdYNGU0F1_5Zt_TTDbWzUntRZRgXedubiHA1VMfAMZqnYE4x_cHW-lY7YWSDyR_fhltPbotguBk1wWYy8pYOmRk18wxMv-KR6z0_lVJTRuehoby1nY020mO3-AvlyK1V9QQ-DJPuoWTYftz61svVc3Z7Z5L5PHgxETwftj0tMbrObUQsse9ewVVEfONMBK2mcgHTUMspQoe6xt3JuqNH4eEQcwty3gDOFDiNkVGBahIE49zg1v0aAxvu5oX6VtduZL6WHrHXmG5MSg",

}

MONEY_MAP_DICT = {
    PLUS_USER_MONEY: "gpt4普通会员",
    REGULAR_USER_MONEY: "一天体验",
    REGULAR_USER_COUNT_MONEY: "gpt4plus会员",
    PLUS_USER_COUNT_MONEY: "4.0季度会员",
    PLUS_USER_YEAR_MONEY: "4.0年度会员",
}

# 默认费率（仅用于首次初始化数据库和数据库暂不可用时回退）；运行时以 tb_pricing_settings 为准。
# 基准成本美元价 × 6.9 元/$ × 1.5 成本加价 = 对用户单价（money_rate/discount 均为 1 时）。
PROFIT_MULTIPLIER = 1.5
PRICE_MULTIPLIER = PROFIT_MULTIPLIER  # 兼容旧名，不要单独改woxi
MONEY_RATE = PROFIT_MULTIPLIER
DOR2CHY = 6.9  # 美元兑人民币

# 表里没写 cache_read 时的兜底（GPT-5 以后官方 cached input 多为 10%）。
# 有官方缓存价的模型以表内 cache_read / cache_write 为准，不要靠这两个调利润。
CACHE_READ_MULTIPLIER = 0.1
# 官方没标 cache writes 的模型不收写入费（OpenAI 自动 prompt cache 通常只有 cached input）。
CACHE_WRITE_MULTIPLIER = 0.0
# None = 自动（有 OpenAI cached_tokens，或 input >= cache 合计时，视为 input 已含缓存）
# True = 总是从 input 里扣除 cache；False = 总是按 Anthropic 口径（input 不含 cache）
CACHE_INPUT_INCLUDES_CACHE = None

# OpenAI 的请求级处理模式价差（相对于 Standard）。
# Batch 是异步批处理价，Fast 是原 Priority processing 的新名称。
# 这里按官方价目表的比例保存，具体 token 数仍以响应 usage 为准。
SERVICE_TIER_MULTIPLIERS = {
    "standard": 1.0,
    "batch": 0.5,
    "fast": 2.0,
    "priority": 2.0,  # 兼容 Fast mode 的旧请求名
}

# 长上下文官方差价。整笔请求切换，不是只对超出阈值的 token 加价。
# 规格：(threshold, op, input_mul, output_mul, tiers)
#   op "gt" = prompt > threshold，"ge" = prompt >= threshold
#   prompt = 未缓存输入 + 缓存读取 + 缓存写入
#   input_mul 同时作用于 cache_read / cache_write；output_mul 只作用于输出
#   tiers 为 None 时所有处理模式都有长上下文价；否则只有列出的模式（如 Fast 未公布则不切换）
# 规格为 None 表示该前缀明确没有长短上下文差价，用来挡住更短前缀。
# 按倍率接入时只保存短上下文成本价，扣费时再乘这里的倍数，所以改倍率会自动适配长上下文。
# 对照：
#   OpenAI https://developers.openai.com/api/docs/pricing  （短 ≤272K，长 >272K；input/cache 2x，output 1.5x）
#   xAI    https://docs.x.ai/developers/pricing             （长 ≥200K，全价 2x）
#   Gemini https://ai.google.dev/gemini-api/docs/pricing    （Pro：长 >200K，input/cache 2x，output 1.5x；Flash 不分档）
#   Claude https://platform.claude.com/docs/en/about-claude/pricing
#     Haiku 5.5：prompt >100K 时输入/缓存/输出均为 5x。其余现售 Claude 全窗口一个价。
_ALL_CONTEXT_TIERS = None
_OPENAI_LONG = (272_000, "gt", 2.0, 1.5, ("standard", "batch", "fast", "priority"))
_OPENAI_LONG_NO_FAST = (272_000, "gt", 2.0, 1.5, ("standard", "batch"))
LONG_CONTEXT_RULES = (
    ("gpt-5.6-cyber", None),
    ("gpt-5.5-cyber", None),
    ("gpt-5.4-mini", None),
    ("gpt-5.4-nano", None),
    ("daybreak-red", None),
    ("gpt-5.5-pro", (272_000, "gt", 2.0, 1.5, ("standard",))),
    ("gpt-5.4-pro", (272_000, "gt", 2.0, 1.5, ("standard", "batch"))),
    ("gpt-6", _OPENAI_LONG),
    ("gpt-5.6", _OPENAI_LONG),
    ("daybreak-blue", _OPENAI_LONG),
    ("gpt-5.5", _OPENAI_LONG_NO_FAST),
    ("gpt-5.4", _OPENAI_LONG_NO_FAST),
    ("grok-4.7", (200_000, "ge", 2.0, 2.0, _ALL_CONTEXT_TIERS)),
    ("grok-4.6", (200_000, "ge", 2.0, 2.0, _ALL_CONTEXT_TIERS)),
    ("grok-4.5", (200_000, "ge", 2.0, 2.0, _ALL_CONTEXT_TIERS)),
    ("grok-4.3", (200_000, "ge", 2.0, 2.0, _ALL_CONTEXT_TIERS)),
    ("grok-4.20", (200_000, "ge", 2.0, 2.0, _ALL_CONTEXT_TIERS)),
    ("gemini-3.1-pro", (200_000, "gt", 2.0, 1.5, _ALL_CONTEXT_TIERS)),
    ("gemini-2.5-pro", (200_000, "gt", 2.0, 1.5, _ALL_CONTEXT_TIERS)),
    # 必须长于 claude-haiku-4-5，避免 4.5 误用 5 倍价。
    ("claude-haiku-5-5", (100_000, "gt", 5.0, 5.0, _ALL_CONTEXT_TIERS)),
)


def _p(inp, out, cache_read=None, cache_write=None):
    """OpenAI Standard short-context 官方价，$ / 1M tokens。"""
    row = {"input": inp, "output": out}
    if cache_read is not None:
        row["cache_read"] = cache_read
    if cache_write is not None:
        row["cache_write"] = cache_write
    return row


# https://developers.openai.com/api/docs/pricing  Standard short-context（2026-09）
MODEL_MAP_MONEY_DICT = {
    # xAI Grok（短上下文 < 200k prompt）
    # https://docs.x.ai/developers/pricing
    "grok-4.7": _p(2.00, 6.00, 0.50),
    "grok-4.6": _p(2.00, 6.00, 0.50),
    "grok-4.5": _p(2.00, 6.00, 0.30),
    "grok-4.3": _p(1.25, 2.50, 0.20),

    # GPT-6 / 5.6 / 5.5 / 5.4 的长上下文见 LONG_CONTEXT_RULES（prompt > 272K）
    "gpt-6-astra": _p(10.00, 50.00, 1.00, 12.50),
    "gpt-6-sol": _p(2.00, 10.00, 0.20, 2.50),
    # GPT-6.1 官方缓存读均为输入的 5%
    # https://developers.openai.com/api/docs/models/gpt-6.1-sol
    "gpt-6.1-sol": _p(2.00, 10.00, 0.10, 2.50),
    # https://developers.openai.com/api/docs/models/gpt-6.1-terra
    "gpt-6.1-terra": _p(1.00, 5.00, 0.05, 1.25),
    "gpt-6-luna": _p(0.10, 0.50, 0.01, 0.125),
    "gpt-6": _p(10.00, 50.00, 1.00, 12.50),  # 默认对齐 Astra

    # GPT-5.6 / Daybreak
    "gpt-5.6-sol": _p(4.00, 20.00, 0.40, 5.00),
    "gpt-5.6-terra": _p(2.00, 12.00, 0.20, 2.50),
    "gpt-5.6-luna": _p(0.20, 1.20, 0.02, 0.25),
    "gpt-5.6-cyber": _p(12.50, 75.00, 1.25, 15.625),
    "gpt-5.6": _p(4.00, 20.00, 0.40, 5.00),
    "gpt-daybreak-blue-latest": _p(4.00, 20.00, 0.40, 5.00),
    "gpt-daybreak-red-latest": _p(12.50, 75.00, 1.25, 15.625),
    "daybreak-blue-latest": _p(4.00, 20.00, 0.40, 5.00),
    "daybreak-red-latest": _p(12.50, 75.00, 1.25, 15.625),

    # GPT-5.5
    "gpt-5.5-cyber": _p(12.50, 75.00, 1.25),
    "gpt-5.5-pro": _p(30.00, 180.00),
    "gpt-5.5": _p(5.00, 30.00, 0.50),
    "chat-latest": _p(5.00, 30.00, 0.50),

    # GPT-5.4
    "gpt-5.4-mini": _p(0.75, 4.50, 0.075),
    "gpt-5.4-nano": _p(0.20, 1.25, 0.02),
    "gpt-5.4-pro": _p(30.00, 180.00),
    "gpt-5.4": _p(2.50, 15.00, 0.25),

    # GPT-5.3 / 5.2 / 5.1
    "gpt-5.3-codex-spark": _p(1.75, 14.00, 0.175),
    "gpt-5.3-codex": _p(1.75, 14.00, 0.175),
    "gpt-5.2-codex": _p(1.75, 14.00, 0.175),
    "gpt-5.2-pro": _p(21.00, 168.00),
    "gpt-5.2": _p(1.75, 14.00, 0.175),
    "gpt-5.1-codex-mini": _p(0.25, 2.00, 0.025),
    "gpt-5.1-codex-max": _p(1.25, 10.00, 0.125),
    "gpt-5.1-codex": _p(1.25, 10.00, 0.125),
    "gpt-5.1": _p(1.25, 10.00, 0.125),

    # GPT-5
    "gpt-5-codex": _p(1.25, 10.00, 0.125),
    "gpt-5-mini": _p(0.25, 2.00, 0.025),
    "gpt-5-nano": _p(0.05, 0.40, 0.005),
    "gpt-5-pro": _p(15.00, 120.00),
    "gpt-5-search-api": _p(1.25, 10.00, 0.125),
    "gpt-5": _p(1.25, 10.00, 0.125),
    "codex-mini-latest": _p(1.50, 6.00),

    # GPT-4.1 / 4o / 4 / 3.5
    "gpt-4.1-mini": _p(0.40, 1.60, 0.10),
    "gpt-4.1-nano": _p(0.10, 0.40, 0.025),
    "gpt-4.1": _p(2.00, 8.00, 0.50),
    "gpt-4o-mini": _p(0.15, 0.60, 0.075),
    "gpt-4o-2024-05-13": _p(5.00, 15.00),
    "gpt-4o": _p(2.50, 10.00, 1.25),
    "chatgpt-4o-latest": _p(2.50, 10.00, 1.25),
    "gpt-4-turbo": _p(10.00, 30.00),
    "gpt-4": _p(30.00, 60.00),
    # OpenAI GPT-Audio：音频 token 单列费率；文本输入/输出仍走基础单价（$/1M）。
    "gpt-audio": {**_p(2.50, 10.00), "audio_input": 32.00, "audio_output": 64.00},
    "gpt-3.5-turbo-instruct": _p(1.50, 2.00),
    "gpt-3.5-turbo": _p(0.50, 1.50),

    # o 系列
    "o4-mini-deep-research": _p(2.00, 8.00, 0.50),
    "o4-mini": _p(1.10, 4.40, 0.275),
    "o3-deep-research": _p(10.00, 40.00),
    "o3-mini": _p(1.10, 4.40, 0.55),
    "o3-pro": _p(20.00, 80.00),
    "o3": _p(2.00, 8.00, 0.50),
    "o1-pro": _p(150.00, 600.00),
    "o1-preview": _p(15.00, 60.00, 7.50),
    "o1-mini": _p(3.00, 12.00),
    "o1": _p(15.00, 60.00, 7.50),

    # OpenAI embeddings（官方价 / 1M input tokens；无输出计费）
    "text-embedding-3-large": _p(0.13, 0),
    "text-embedding-3-small": _p(0.02, 0),
    "text-embedding-ada-002": _p(0.10, 0),
}


def _img(text_in, image_in, image_out, text_cache=None, image_cache=None):
    """GPT Image 官方价，$ / 1M tokens。"""
    row = {
        "kind": "image",
        "text_input": text_in,
        "image_input": image_in,
        "output": image_out,
    }
    if text_cache is not None:
        row["text_cache"] = text_cache
    if image_cache is not None:
        row["image_cache"] = image_cache
    return row


def _cny(inp, out, cache_read=None):
    """智谱官网人民币标价，按 DOR2CHY 折成美元 / 1M tokens。"""
    rate = float(DOR2CHY)

    def usd(value):
        return round(float(value) / rate, 6)

    return _p(usd(inp), usd(out), None if cache_read is None else usd(cache_read))


# 官网标准价（USD / 1M tokens）。catalog_rate = 本表 × 倍率。
# 不要把 Teamo 折后价写进这里。
# Claude: https://platform.claude.com/docs/en/about-claude/pricing （2026-10-07，全球 Standard）
#   cache_read = cache hits；cache_write = 5 分钟缓存写入。1 小时写入是 2× 输入，默认按 5 分钟档。
# Gemini: https://ai.google.dev/gemini-api/docs/pricing （Standard，短上下文）
#   3.8 / 3.7 / 3.6 Flash 在 2026-12-31 前的现行价是 0.75 / 3.75，2027-01-01 起改为 1.50 / 7.50。
# DeepSeek: https://api-docs.deepseek.com/quick_start/pricing
#   官网分峰谷，目录按峰值，避免高峰少扣。旧名 v4-flash / vision-exp 官网按 Flash 计费。
# 智谱: https://docs.bigmodel.cn/cn/guide/start/pricing （标准价，不含限时折扣）
OFFICIAL_PROVIDER_MODEL_MONEY_DICT = {
    "claude-fable-5-1": _p(10.00, 50.00, 0.25, 12.50),
    "claude-fable-5": _p(10.00, 50.00, 1.00, 12.50),
    "claude-opus-5-5": _p(4.00, 20.00, 0.20, 5.00),
    "claude-opus-5": _p(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": _p(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-7": _p(5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-6": _p(5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5-5": _p(2.00, 10.00, 0.10, 2.50),
    "claude-sonnet-5": _p(2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4-6": _p(3.00, 15.00, 0.30, 3.75),
    # 短上下文标准价（prompt ≤100K）：输入 0.10，输出 0.50，缓存命中 0.01，5 分钟写入 0.125。
    # 1 小时写入 0.20 不单列。超过 100K 整笔 5 倍，见 LONG_CONTEXT_RULES，不要把长上下文价写进本行。
    "claude-haiku-5-5": _p(0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": _p(1.00, 5.00, 0.10, 1.25),
    "claude-haiku-4-5-20251001": _p(1.00, 5.00, 0.10, 1.25),
    "gemini-3.8-flash": _p(0.75, 3.75, 0.075),
    "gemini-3.7-flash": _p(0.75, 3.75, 0.075),
    "gemini-3.6-flash": _p(0.75, 3.75, 0.075),
    "gemini-3.5-flash": _p(1.50, 9.00, 0.15),
    "gemini-3.5-flash-lite": _p(0.30, 2.50, 0.03),
    "gemini-3.1-pro-preview": _p(2.00, 12.00, 0.20),
    "deepseek-flash": _p(0.30, 1.20, 0.006),
    "deepseek-v4-flash": _p(0.30, 1.20, 0.006),
    "deepseek-v4-flash-vision-exp": _p(0.30, 1.20, 0.006),
    "deepseek-v4-pro": _p(1.32, 3.96, 0.044),
    "glm-5.3": _cny(8, 28, 2),
    "glm-5.3-flash": _cny(0.8, 2.8, 0.23),
    "glm-5.2": _cny(8, 28, 2),
}

# https://ai.google.dev/gemini-api/docs/pricing  Gemini 3.1 Flash Image Standard
# 图像输出 $60 / 1M tokens；文本输出 $3，图像模型的 output 按图像 token 计。
OFFICIAL_PROVIDER_IMAGE_MODEL_MONEY_DICT = {
    "gemini-3.1-flash-image": _img(0.50, 0.50, 60.00),
}


# https://developers.openai.com/api/docs/pricing  Image generation（2026-09）
# 售价公式与 LLM 相同：官方美元成本 × PROFIT_MULTIPLIER × DOR2CHY × money_rate × discount
# 接入其他生图模型：在本表加一行 _img(text_in, image_in, image_out, ...)。
IMAGE_MODEL_MONEY_DICT = {
    "gpt-image-2.5-sunburst": _img(5.00, 8.00, 30.00, 1.25, 2.00),
    "gpt-image-2.5-flare": _img(5.00, 8.00, 30.00, 1.25, 2.00),
    "gpt-image-2": _img(2.50, 4.00, 15.00, 0.625, 1.00),
    "gpt-image-1.5": _img(5.00, 8.00, 32.00, 1.25, 2.00),
    "chatgpt-image-latest": _img(5.00, 8.00, 32.00, 1.25, 2.00),
    "gpt-image-1-mini": _img(2.00, 2.50, 8.00, 0.20, 0.25),
    "gpt-image-1": _img(5.00, 10.00, 40.00, 1.25, 2.50),
}


def _img_per_call(tiers: dict, *, text_in=0.0, image_in=0.0, image_out=0.0, text_cache=None):
    """Teamo 按张档位价（USD / image），同时保留 token 价供展示/回退。"""
    row = {
        "kind": "image",
        "billing": "per_image",
        "per_image_tiers": {str(k).lower(): float(v) for k, v in (tiers or {}).items()},
        "text_input": float(text_in or 0),
        "image_input": float(image_in or 0),
        "output": float(image_out or 0),
    }
    if text_cache is not None:
        row["text_cache"] = float(text_cache)
    return row


# https://teamorouter.cn/v1/models/pricing 绘图模型快照（2026-10-01）。
# GPT Image：官网按输出最长边分 1K/2K/4K 按张计费（per_call_tiers.cost）。
# Gemini Image：按 token 计费（input/output，$ / 1M tokens）。
TEAMO_IMAGE_MODEL_MONEY_DICT = {
    "gpt-image-2": _img_per_call(
        {"1k": 0.006, "2k": 0.02, "4k": 0.025},
        text_in=0.5, image_in=0.8, image_out=3.0, text_cache=0.125,
    ),
    "gpt-image-2.5-sunburst": _img_per_call(
        {"1k": 0.006, "2k": 0.02, "4k": 0.025},
        text_in=0.5, image_in=0.8, image_out=3.0, text_cache=0.125,
    ),
    "gpt-image-2.5-flare": _img_per_call(
        {"1k": 0.006, "2k": 0.02, "4k": 0.025},
        text_in=0.5, image_in=0.8, image_out=3.0, text_cache=0.125,
    ),
    "gemini-3.1-flash-image": {
        "kind": "image",
        "billing": "token",
        "text_input": 0.3,
        "image_input": 0.0,
        "output": 36.0,
    },
}

# TeamoRouter API Key 不写入仓库。优先级：环境变量 TEAMO_API_KEY
# > 本处手填值（保持为空，勿提交真实密钥）> TEAMO_API_KEY_FILE 指向的密钥文件。
TEAMO_API_KEY = ""


def _load_teamo_api_key() -> str:
    key = os.environ.get("TEAMO_API_KEY", "").strip() or TEAMO_API_KEY.strip()
    if key:
        return key
    path = Path(os.environ.get("TEAMO_API_KEY_FILE", "/etc/claude_code_proxy/teamo_api_key"))
    try:
        if path.stat().st_mode & 0o077:
            raise ValueError(f"TEAMO_API_KEY_FILE must not be accessible to group/others: {path}")
        key = path.read_text(encoding="utf-8").strip()
        if not key or "\n" in key or "\r" in key:
            raise ValueError(f"TEAMO_API_KEY_FILE must contain one non-empty key: {path}")
        return key
    except FileNotFoundError:
        return ""


CLAUDE_GPT_KEY = _load_teamo_api_key() if TEAMO_UPSTREAM else os.environ.get("CLAUDE_GPT_KEY", "").strip()

# https://teamorouter.cn/zh#pricing 展开全表的折后价格快照（2026-09-26，USD / 1M tokens）。
# 价格随上游波动；本地快照不是实时结算价，必须定期重新核价。
# DeepSeek 官网分峰/谷，但未公布准确时段；此处按峰值计费以避免漏扣。
# 绘图模型见 TEAMO_IMAGE_MODEL_MONEY_DICT（GPT Image 按张，Gemini Image 按 token）。
TEAMO_MODEL_MONEY_DICT = {
    "gpt-6-astra": _p(1.89, 9.45, 0.189),
    "gpt-6-sol": _p(0.356, 1.78, 0.0356),
    "gpt-6-luna": _p(0.0501, 0.251, 0.005),
    "gpt-5.6-sol": _p(0.84, 5.04, 0.084),
    "gpt-5.6-terra": _p(0.774, 4.64, 0.0774),
    "gpt-5.6-luna": _p(0.12, 0.721, 0.012),
    "gpt-5.5": _p(1.26, 7.59, 0.127),
    "gpt-5.4": _p(1.25, 7.5, 0.125),
    "gpt-5.4-mini": _p(0.45, 2.7, 0.045),
    "claude-opus-5-5": _p(1.01, 5.04, 0.0504),
    "claude-fable-5-1": _p(2.5, 12.5, 0.0625),
    "claude-opus-5": _p(1.28, 6.4, 0.128),
    "claude-fable-5": _p(2.5, 12.5, 0.25),
    "claude-sonnet-5": _p(0.518, 2.59, 0.0518),
    "claude-opus-4-8": _p(1.39, 6.92, 0.139),
    "claude-opus-4-7": _p(1.38, 6.88, 0.138),
    "claude-opus-4-6": _p(1.35, 6.78, 0.136),
    "claude-sonnet-4-6": _p(0.45, 2.25, 0.045),
    "claude-haiku-4-5": _p(0.15, 0.75, 0.015),
    "claude-haiku-4-5-20251001": _p(0.15, 0.75, 0.015),
    "gemini-3.8-flash": _p(0.29, 1.45, 0.029),
    "gemini-3.7-flash": _p(0.222, 1.11, 0.0222),
    "gemini-3.6-flash": _p(0.18, 0.9, 0.018),
    "gemini-3.5-flash-lite": _p(0.087, 0.725, 0.0087),
    "gemini-3.1-pro-preview": _p(0.48, 2.88, 0.048),
    "gemini-3.5-flash": _p(0.435, 2.61, 0.0435),
    "deepseek-flash": _p(0.197, 0.786, 0.002),
    "deepseek-flash-free": _p(0.0003, 0.0012, 0),
    "deepseek-v4-flash-vision-exp": _p(0.44, 1.32, 0.007),
    "deepseek-v4-pro": _p(0.8, 2.4, 0.0133),
    "deepseek-v4-flash": _p(0.294, 0.883, 0.0047),
    "deepseek-v4-flash-free": _p(0.0004, 0.0013, 0),
    "glm-5.3-flash": _p(0.0825, 0.29, 0.0217),
    "glm-5.3-flash-free": _p(0.0001, 0.0004, 0),
    "glm-5.3": _p(1.33, 4.17, 0.246),
    "glm-5.2": _p(0.84, 2.64, 0.156),
    "grok-4.6": _p(0.202, 0.606, 0.0505),
}

# Fast 档是独立价格，不能用 standard * 2 推导（官网可能低于标准档）。
TEAMO_FAST_MODEL_MONEY_DICT = {
    "gpt-5.6-sol": _p(1.01, 6.06, 0.101),
    "gpt-5.6-terra": _p(4.0, 24.0, 0.4),
    "gpt-5.6-luna": _p(0.4, 2.4, 0.04),
    "gpt-5.5": _p(0.8, 4.8, 0.08),
}

# /v1/models 官网模型 ID 与已有客户端旧别名；别名转发前必须规范为官方 ID。
TEAMO_MODEL_ALIASES = {
    "claude-opus-5.5": "claude-opus-5-5",
    "claude-fable-5.1": "claude-fable-5-1",
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-v4.1-flash-free": "deepseek-flash-free",
    "glm-5.3-flash-ox-alpha": "glm-5.3-flash",
    "glm-5.3-flash-free-ox-alpha": "glm-5.3-flash-free",
}
