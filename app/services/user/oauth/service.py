from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.oauth import OAuthAccount
from app.services.user.oauth.base import OAuthUserInfo
from app.core.config import settings


class OAuthService:
    """OAuth 服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_or_create_user(self, oauth_info: OAuthUserInfo) -> User:
        """获取或创建用户"""
        # 首先通过 provider + provider_user_id 查找
        oauth_account = await self._get_oauth_account(
            oauth_info.provider, oauth_info.provider_user_id
        )

        if oauth_account:
            # 更新 OAuth 账号信息
            await self._update_oauth_account(oauth_account, oauth_info)
            return oauth_account.user

        # 尝试通过邮箱查找用户（如果有邮箱）
        user = None
        if oauth_info.email:
            user = await self._get_user_by_email(oauth_info.email)

        # 如果用户不存在，创建新用户
        if not user:
            user = await self._create_user(oauth_info)

        # 创建新的 OAuth 账号关联
        oauth_account = OAuthAccount(
            user_id=user.id,
            provider=oauth_info.provider,
            provider_user_id=oauth_info.provider_user_id,
            access_token=oauth_info.access_token,
            refresh_token=oauth_info.refresh_token,
            token_type=oauth_info.token_type,
            expires_at=oauth_info.expires_at,
            scopes=oauth_info.scopes,
            provider_data=oauth_info.raw_data,  # 存储原始数据
        )
        self.db.add(oauth_account)
        await self.db.commit()

        return user

    async def _get_oauth_account(
        self, provider: str, provider_user_id: str
    ) -> Optional[OAuthAccount]:
        """通过OAuth信息查找账号"""
        stmt = select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _get_user_by_email(self, email: str) -> Optional[User]:
        """通过邮箱查找用户"""
        if not email:  # 如果没有邮箱，直接返回 None
            return None

        stmt = select(User).where(User.email == email)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _create_user(self, oauth_info: OAuthUserInfo) -> User:
        """创建新用户"""
        # 生成用户名
        base_username = (
            oauth_info.username
            or f"user_{oauth_info.provider}_{oauth_info.provider_user_id}"
        )
        username = await self._generate_unique_username(base_username)

        # 创建用户
        user = User(
            email=oauth_info.email,  # 可以为 None
            username=username,
            is_active=True,
            is_verified=bool(oauth_info.email),  # 如果有邮箱则认为已验证
            avatar_url=oauth_info.avatar_url,
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def _generate_unique_username(self, base: str) -> str:
        """生成唯一的用户名"""
        # 清理用户名，只保留字母、数字和下划线
        username = "".join(c if c.isalnum() or c == "_" else "_" for c in base)

        # 检查用户名是否已存在
        stmt = select(User).where(User.username == username)
        result = await self.db.execute(stmt)
        existing = result.scalar_one_or_none()

        if not existing:
            return username

        # 如果存在，添加数字后缀直到找到唯一的用户名
        counter = 1
        while True:
            new_username = f"{username}_{counter}"
            stmt = select(User).where(User.username == new_username)
            result = await self.db.execute(stmt)
            existing = result.scalar_one_or_none()

            if not existing:
                return new_username
            counter += 1

    async def _create_oauth_account(
        self, user: User, oauth_info: OAuthUserInfo
    ) -> OAuthAccount:
        """创建OAuth账号关联"""
        oauth_account = OAuthAccount(
            user_id=user.id,
            provider=oauth_info.provider,
            provider_user_id=oauth_info.provider_user_id,
            access_token=oauth_info.access_token,
            refresh_token=oauth_info.refresh_token,
            expires_at=(
                datetime.utcnow() + timedelta(seconds=oauth_info.expires_in)
                if oauth_info.expires_in
                else None
            ),
            token_type=oauth_info.token_type,
            scopes=oauth_info.scopes,
        )
        self.db.add(oauth_account)
        await self.db.commit()
        await self.db.refresh(oauth_account)
        return oauth_account

    async def _update_oauth_account(
        self, oauth_account: OAuthAccount, oauth_info: OAuthUserInfo
    ) -> None:
        """更新OAuth账号信息"""
        oauth_account.access_token = oauth_info.access_token
        oauth_account.refresh_token = oauth_info.refresh_token
        oauth_account.expires_at = (
            datetime.utcnow() + timedelta(seconds=oauth_info.expires_in)
            if oauth_info.expires_in
            else None
        )
        oauth_account.token_type = oauth_info.token_type
        oauth_account.scopes = oauth_info.scopes
        await self.db.commit()

    async def _update_user_info(self, user: User, oauth_info: OAuthUserInfo) -> None:
        """更新用户信息"""
        # 只更新非空字段
        if oauth_info.avatar_url:
            user.avatar_url = oauth_info.avatar_url
        if oauth_info.email and not user.email:
            user.email = oauth_info.email
            user.is_verified = True
        await self.db.commit()

    async def _username_exists(self, username: str) -> bool:
        """检查用户名是否存在"""
        stmt = select(User).where(User.username == username)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is not None
