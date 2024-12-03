from typing import Optional
from datetime import timedelta

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, IntegerIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)

from app.core.config import settings
from app.models.user import User
from app.db.session import get_async_session
from sqlalchemy.ext.asyncio import AsyncSession


# Bearer transport
bearer_transport = BearerTransport(tokenUrl=f"{settings.API_V1_STR}/auth/token")
refresh_bearer_transport = BearerTransport(
    tokenUrl=f"{settings.API_V1_STR}/auth/token/refresh"
)


# JWT strategy
def get_jwt_strategy() -> JWTStrategy:
    """获取JWT策略"""
    return JWTStrategy(
        secret=settings.SECRET_KEY,
        lifetime_seconds=timedelta(
            days=settings.ACCESS_TOKEN_EXPIRE_DAYS
        ).total_seconds(),
    )


def get_refresh_jwt_strategy() -> JWTStrategy:
    """获取refresh token的JWT策略"""
    return JWTStrategy(
        secret=settings.SECRET_KEY,
        lifetime_seconds=timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        ).total_seconds(),
    )


# 认证后端
auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)

# Refresh token认证后端
refresh_auth_backend = AuthenticationBackend(
    name="jwt-refresh",
    transport=refresh_bearer_transport,
    get_strategy=get_refresh_jwt_strategy,
)


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    reset_password_token_secret = settings.SECRET_KEY
    verification_token_secret = settings.SECRET_KEY

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        """用户注册后的回调"""
        print(f"User {user.id} has registered.")

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """忘记密码后的回调"""
        print(f"User {user.id} has forgot their password. Reset token: {token}")

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        """请求验证后的回调"""
        print(f"Verification requested for user {user.id}. Verification token: {token}")


async def get_user_manager(session: AsyncSession = Depends(get_async_session)):
    yield UserManager(session)
