# -*- coding: utf-8 -*-
# @Time    : 2026/04/17 2:56 下午
# @Author  : rabbit -> converted to async
# @File    : database.py
# @Software: PyCharm

# import aioredis
import os

from sqlalchemy import select, exc, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from settings.macro import POOL_RECYCLE_TIME, MAX_POOL_SIZE, MAX_OVERFLOW, POOL_TIME_OUT

# 数据库连接串从环境变量 DATABASE_URL 读取，不要在仓库中提交真实账号密码。
# 例：mysql+aiomysql://user:pass@host:3306/dbname
SQLALCHEMY_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+aiomysql://user:password@127.0.0.1:3306/gpt_db",
)
# If you need the sync URL elsewhere, keep original as SQLALCHEMY_DATABASE_URL_SYNC


def create_async_engine_inner():
    engine = create_async_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_recycle=POOL_RECYCLE_TIME,
        pool_size=MAX_POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIME_OUT,
        pool_pre_ping=True,
        pool_use_lifo=True,
        future=True,
        echo=False,
        connect_args={"connect_timeout": 5},
    )

    return engine

# Async engine and session factory
async_engine = create_async_engine_inner()
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# Declarative base (same as sync)
Base = declarative_base()

# Helper async context generator for acquiring sessions (e.g. for FastAPI deps)
async def get_async_session():
    """
    Async generator that yields an AsyncSession.
    Usage (FastAPI):
        async with get_async_session() as session:
            ...
    """
    async with AsyncSessionLocal() as session:
        yield session

