from typing import Optional

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, IntegerIDMixin
from fastapi_users.db import SQLAlchemyUserDatabase

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import get_async_session
from app.db.models import User

logger = get_logger(__name__)


class UserManager(IntegerIDMixin, BaseUserManager[User, int]):
    reset_password_token_secret = settings.SECRET_KEY
    verification_token_secret = settings.SECRET_KEY

    async def on_after_register(self, user: User, request: Optional[Request] = None):
        logger.info(
            "user_registered",
            user_id=user.id,
            username=user.username,
            email=user.email,
        )

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        logger.info(
            "password_reset_requested",
            user_id=user.id,
            username=user.username,
            email=user.email,
        )

    async def on_after_request_verify(
        self, user: User, token: str, request: Optional[Request] = None
    ):
        logger.info(
            "verification_requested",
            user_id=user.id,
            username=user.username,
            email=user.email,
        )


async def get_user_db(session=Depends(get_async_session)):
    """获取用户数据库会话"""
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(user_db=Depends(get_user_db)):
    """获取用户管理器"""
    yield UserManager(user_db)
