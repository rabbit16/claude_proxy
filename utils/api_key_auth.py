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


def billing_key_for_user(user) -> str:
    """鉴权后交给计费链路的用户键：手机号优先，匿名用户回落主键。

    匿名注册用户 tb_user.telephone 为 NULL；若继续返回空串，后续按手机号
    查用户的扣费写入会全部落空（user_missing）。主键字符串纯数字，
    也能被价目/路由里的 _user_id_from_key 识别。
    """
    telephone = (getattr(user, "telephone", None) or "").strip()
    if telephone:
        return telephone
    user_id = getattr(user, "user_id", None)
    return str(user_id) if user_id else ""


async def resolve_billing_user(db, user_key: str):
    """按计费键反查用户：手机号优先，匿名用户回落到主键。"""
    key = (user_key or "").strip()
    if not key:
        return None
    if key.startswith("sk-"):
        return await lookup_user_by_credential(db, key)
    result = await db.execute(select(User).filter_by(telephone=key))
    user = result.scalars().first()
    if user is not None:
        return user
    if key.isdigit():
        return await db.get(User, int(key))
    return None


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
