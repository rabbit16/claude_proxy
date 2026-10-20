# -*- coding: utf-8 -*-
"""按机器规格收缩并发。环境变量 PROXY_* 可覆盖。"""
from __future__ import annotations

import os
from typing import Any, Dict


def _cpu_count() -> int:
    return os.cpu_count() or 1


def _mem_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 2048


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def detect() -> Dict[str, Any]:
    cpus = _cpu_count()
    mem_mb = _mem_mb()

    # 云主机 2C/1.8G：必须单进程。LiteLLM 已经占 ~300MB。
    if cpus <= 2 or mem_mb < 3072:
        profile = "small"
        workers = 1
        http_max = 256
        http_keepalive = 64
        upstream = 0  # 保留配置兼容性；main.py 不再使用上游 Semaphore
        compress = 2
        pool_size = 4
        pool_overflow = 4
        limit_concurrency = 256
    elif cpus >= 8 and mem_mb >= 16384:
        profile = "desktop"
        workers = 4
        http_max = 120
        http_keepalive = 40
        upstream = 80
        compress = 4
        pool_size = 8
        pool_overflow = 8
        limit_concurrency = 100
    else:
        profile = "medium"
        workers = 2
        http_max = 80
        http_keepalive = 24
        upstream = 48
        compress = 3
        pool_size = 6
        pool_overflow = 6
        limit_concurrency = 64

    return {
        "profile": os.environ.get("PROXY_PROFILE", profile),
        "cpus": cpus,
        "mem_mb": mem_mb,
        "workers": _env_int("PROXY_WORKERS", workers),
        "http_max_connections": _env_int("PROXY_HTTP_MAX", http_max),
        "http_max_keepalive": _env_int("PROXY_HTTP_KEEPALIVE", http_keepalive),
        "upstream_concurrency": _env_int("PROXY_UPSTREAM", upstream),
        "compress_threads": _env_int("PROXY_COMPRESS_THREADS", compress),
        "pool_size": _env_int("PROXY_DB_POOL", pool_size),
        "pool_overflow": _env_int("PROXY_DB_OVERFLOW", pool_overflow),
        "limit_concurrency": _env_int("PROXY_LIMIT_CONCURRENCY", limit_concurrency),
    }


CAPACITY = detect()
