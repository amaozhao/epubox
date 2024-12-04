import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import OAuthAccount, OAuthProvider, User


@pytest.mark.asyncio
async def test_create_user(db_session: AsyncSession):
    """测试创建用户"""
    # 创建用户
    user = User(
        email="test_create@example.com",
        hashed_password="hashed_password",
        is_active=True,
        is_superuser=False,
        is_verified=False,
        username="test_create_user",
        full_name="Test User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # 验证用户
    assert user.id is not None
    assert user.email == "test_create@example.com"
    assert user.hashed_password == "hashed_password"
    assert user.is_active is True
    assert user.is_superuser is False
    assert user.is_verified is False
    assert user.username == "test_create_user"
    assert user.full_name == "Test User"


@pytest.mark.asyncio
async def test_create_oauth_account(db_session: AsyncSession):
    """测试创建 OAuth 账号"""
    # 创建用户
    user = User(
        email="test_oauth@example.com",
        hashed_password="hashed_password",
        is_active=True,
        username="test_oauth_user",
    )
    db_session.add(user)
    await db_session.commit()

    # 创建 OAuth 账号
    oauth_account = OAuthAccount(
        provider=OAuthProvider.GITHUB,
        provider_user_id="test_oauth_123",
        provider_user_login="test_user",
        provider_user_email="oauth_account@example.com",
        access_token="access_token",
        expires_at=3600,
        refresh_token="refresh_token",
        token_type="bearer",
        scopes="user,repo",
        user=user,
    )
    db_session.add(oauth_account)
    await db_session.commit()
    await db_session.refresh(oauth_account)

    # 验证 OAuth 账号
    assert oauth_account.id is not None
    assert oauth_account.provider == OAuthProvider.GITHUB
    assert oauth_account.provider_user_id == "test_oauth_123"
    assert oauth_account.provider_user_login == "test_user"
    assert oauth_account.provider_user_email == "oauth_account@example.com"
    assert oauth_account.access_token == "access_token"
    assert oauth_account.expires_at == 3600
    assert oauth_account.refresh_token == "refresh_token"
    assert oauth_account.token_type == "bearer"
    assert oauth_account.scopes == "user,repo"
    assert oauth_account.user_id == user.id


@pytest.mark.asyncio
async def test_user_oauth_relationship(db_session: AsyncSession):
    """测试用户和 OAuth 账号的关系"""
    # 创建用户
    user = User(
        email="test_relationship@example.com",
        hashed_password="hashed_password",
        is_active=True,
        username="test_relationship_user",
    )
    db_session.add(user)
    await db_session.commit()

    # 创建多个 OAuth 账号
    oauth_accounts = [
        OAuthAccount(
            provider=OAuthProvider.GITHUB,
            provider_user_id="test_relationship_github_123",
            provider_user_login="github_user",
            provider_user_email="github_oauth@example.com",
            access_token="github_token",
            token_type="bearer",
            user=user,
        ),
        OAuthAccount(
            provider=OAuthProvider.GOOGLE,
            provider_user_id="test_relationship_google_123",
            provider_user_email="google_oauth@example.com",
            access_token="google_token",
            token_type="bearer",
            user=user,
        ),
    ]
    db_session.add_all(oauth_accounts)
    await db_session.commit()

    # 验证关系 - 使用 selectinload 显式加载关系
    query = (
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.oauth_accounts))
    )
    result = await db_session.execute(query)
    db_user = result.scalar_one()

    assert len(db_user.oauth_accounts) == 2
    assert {acc.provider for acc in db_user.oauth_accounts} == {
        OAuthProvider.GITHUB,
        OAuthProvider.GOOGLE,
    }


@pytest.mark.asyncio
async def test_cascade_delete(db_session: AsyncSession):
    """测试级联删除"""
    # 创建用户和 OAuth 账号
    user = User(
        email="test_cascade@example.com",
        hashed_password="hashed_password",
        is_active=True,
        username="test_cascade_user",
    )
    db_session.add(user)
    await db_session.commit()

    oauth_account = OAuthAccount(
        provider=OAuthProvider.GITHUB,
        provider_user_id="test_cascade_123",
        provider_user_login="cascade_user",
        provider_user_email="cascade_oauth@example.com",
        access_token="access_token",
        token_type="bearer",
        user=user,
    )
    db_session.add(oauth_account)
    await db_session.commit()

    # 删除用户
    await db_session.delete(user)
    await db_session.commit()

    # 验证 OAuth 账号也被删除
    query = select(OAuthAccount).where(OAuthAccount.user_id == user.id)
    result = await db_session.execute(query)
    remaining_oauth = result.scalars().all()
    assert len(remaining_oauth) == 0
