from datetime import datetime

from sqlalchemy import select

from settings.models import User, UserApiKey

# 2026-10-01 00:00:00（不含）之前注册、且没生成过 sk- 的用户，仍可用手机号请求模型。
PHONE_KEY_CUTOFF = datetime(2026, 10, 1)


def phone_key_allowed(register_time) -> bool:
    if register_time is None:
        return True
    if getattr(register_time, "tzinfo", None) is not None:
        register_time = register_time.replace(tzinfo=None)
    return register_time < PHONE_KEY_CUTOFF


def phone_credential_allowed(register_time, *, revoked: bool = False, has_issued_key: bool = False) -> bool:
    """生成过 sk- 后，手机号不能再请求模型。删除密钥也不恢复。价目、用量不受这条限制。"""
    if revoked or has_issued_key:
        return False
    return phone_key_allowed(register_time)


async def lookup_user_by_credential(db, key: str):
    """用请求里的密钥找到未删除用户。

    sk- 开头：必须命中 tb_user_api_key 且 is_active。
    其他值：等于未删除用户的手机号即可。能不能请求模型另由 phone_blocked_for_model 判断。
    """
    key = (key or "").strip()
    if not key:
        return None
    if key.startswith("sk-"):
        result = await db.execute(
            select(User)
            .join(UserApiKey, UserApiKey.user_id == User.user_id)
            .where(
                UserApiKey.api_key == key,
                UserApiKey.is_active.is_(True),
                User.is_delete.is_(False),
            )
        )
        return result.scalars().first()

    result = await db.execute(
        select(User).where(User.telephone == key, User.is_delete.is_(False))
    )
    return result.scalars().first()


async def phone_blocked_for_model(db, key: str) -> bool:
    """手机号在生成过 sk-，或注册日达到截止日期后，不能再请求模型。"""
    key = (key or "").strip()
    if not key or key.startswith("sk-"):
        return False
    result = await db.execute(
        select(User).where(User.telephone == key, User.is_delete.is_(False))
    )
    user = result.scalars().first()
    if user is None:
        return False
    issued = await db.execute(
        select(UserApiKey.id).where(UserApiKey.user_id == user.user_id).limit(1)
    )
    return not phone_credential_allowed(
        getattr(user, "register_time", None),
        revoked=bool(getattr(user, "phone_key_revoked", False)),
        has_issued_key=issued.first() is not None,
    )
