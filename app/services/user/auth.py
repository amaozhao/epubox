from typing import Optional, Union, Dict, Any
from datetime import timedelta, datetime

from fastapi import Depends, Request, HTTPException, status
from fastapi_users import BaseUserManager, IntegerIDMixin
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import jwt
from jwt import PyJWTError

from app.core.config import settings
from app.models.user import User
from app.db.session import get_async_session
from app.core.security import get_password_hash, verify_password
from app.schemas.user import UserCreate, Token


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
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
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

refresh_auth_backend = AuthenticationBackend(
    name="jwt-refresh",
    transport=refresh_bearer_transport,
    get_strategy=get_refresh_jwt_strategy,
)


class AuthService:
    """认证服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def register_new_user(self, user_in: UserCreate) -> User:
        """注册新用户"""
        # 检查邮箱是否已存在
        if user_in.email:
            result = await self.db.execute(
                select(User).where(User.email == user_in.email)
            )
            if result.scalar_one_or_none():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="该邮箱已被注册",
                )

        # 检查用户名是否已存在
        result = await self.db.execute(
            select(User).where(User.username == user_in.username)
        )
        if result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="该用户名已被使用",
            )

        # 创建新用户
        user = User(
            email=user_in.email,
            username=user_in.username,
            hashed_password=get_password_hash(user_in.password),
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def authenticate_user(
        self, username: str, password: str
    ) -> Optional[User]:
        """验证用户"""
        # 查找用户
        result = await self.db.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()

        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    async def get_current_user(self, token: str) -> Optional[User]:
        """获取当前用户"""
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            username = payload.get("sub")
            if not username:
                return None
        except PyJWTError:
            return None

        result = await self.db.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()
        return user

    async def create_token(self, user: User) -> Token:
        """创建访问令牌"""
        access_token_expires = timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
        refresh_token_expires = timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )

        # 创建访问令牌
        access_token = self._create_token(
            data={"sub": user.username},
            expires_delta=access_token_expires,
        )

        # 创建刷新令牌
        refresh_token = self._create_token(
            data={"sub": user.username},
            expires_delta=refresh_token_expires,
        )

        return Token(
            access_token=access_token,
            token_type="bearer",
            refresh_token=refresh_token,
        )

    async def refresh_token(self, refresh_token: str) -> Token:
        """刷新访问令牌"""
        try:
            payload = jwt.decode(
                refresh_token,
                settings.SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
            )
            username = payload.get("sub")
            if not username:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="无效的刷新令牌",
                )
        except PyJWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="无效的刷新令牌",
            )

        # 获取用户
        result = await self.db.execute(
            select(User).where(User.username == username)
        )
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户不存在",
            )

        return await self.create_token(user)

    def _create_token(
        self,
        data: Dict[str, Any],
        expires_delta: Optional[timedelta] = None,
    ) -> str:
        """创建JWT令牌"""
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(
            to_encode,
            settings.SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        return encoded_jwt


async def get_current_user(
    db: AsyncSession = Depends(get_async_session),
    token: str = Depends(bearer_transport.scheme),
) -> User:
    """获取当前用户依赖项"""
    auth_service = AuthService(db)
    user = await auth_service.get_current_user(token)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
