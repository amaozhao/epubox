import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.oauth import OAuthAccount
from app.services.user.oauth.base import OAuthUserInfo


@pytest.mark.asyncio
async def test_oauth_github_login(async_client: AsyncClient):
    """测试 GitHub OAuth 登录流程"""
    # 测试授权 URL 生成
    response = await async_client.get("/api/v1/auth/oauth/github/authorize")
    assert response.status_code == 200
    assert "github.com/login/oauth/authorize" in response.json()["url"]
    assert "state" in response.json()["url"]


@pytest.mark.asyncio
async def test_oauth_user_creation(db: AsyncSession):
    """测试 OAuth 用户创建"""
    # 模拟 OAuth 用户信息
    oauth_info = OAuthUserInfo(
        provider="github",
        provider_user_id="12345",
        email="test@example.com",
        username="testuser",
        avatar_url="https://example.com/avatar.jpg",
        raw_data={},
    )

    # 创建用户
    user = User(
        email=oauth_info.email,
        username=oauth_info.username,
        avatar_url=oauth_info.avatar_url,
    )
    db.add(user)
    await db.commit()

    # 创建 OAuth 账号
    oauth_account = OAuthAccount(
        user_id=user.id,
        provider=oauth_info.provider,
        provider_user_id=oauth_info.provider_user_id,
        provider_data=oauth_info.raw_data,
    )
    db.add(oauth_account)
    await db.commit()

    # 验证用户和 OAuth 账号创建
    assert user.id is not None
    assert oauth_account.id is not None
    assert oauth_account.user_id == user.id


@pytest.mark.asyncio
async def test_oauth_token_management(async_client: AsyncClient, db: AsyncSession):
    """测试 OAuth 令牌管理"""
    # 创建测试用户
    user = User(
        email="test@example.com",
        username="testuser",
    )
    db.add(user)
    await db.commit()

    # 测试令牌创建
    response = await async_client.post(
        "/api/v1/auth/token",
        json={
            "username": "testuser",
            "password": "testpass",  # 在实际场景中，这里应该是 OAuth 令牌
        },
    )
    assert response.status_code == 200
    assert "access_token" in response.json()

    # 测试令牌验证
    access_token = response.json()["access_token"]
    response = await async_client.get(
        "/api/v1/auth/token/verify",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200

    # 测试令牌刷新
    refresh_token = response.json().get("refresh_token")
    if refresh_token:
        response = await async_client.post(
            "/api/v1/auth/token/refresh",
            json={"refresh_token": refresh_token},
        )
        assert response.status_code == 200
        assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_oauth_account_linking(db: AsyncSession):
    """测试 OAuth 账号关联"""
    # 创建测试用户
    user = User(
        email="test@example.com",
        username="testuser",
    )
    db.add(user)
    await db.commit()

    # 关联 GitHub 账号
    github_account = OAuthAccount(
        user_id=user.id,
        provider="github",
        provider_user_id="github123",
        provider_data={},
    )
    db.add(github_account)

    # 关联 Google 账号
    google_account = OAuthAccount(
        user_id=user.id,
        provider="google",
        provider_user_id="google123",
        provider_data={},
    )
    db.add(google_account)

    await db.commit()

    # 验证关联
    assert len(user.oauth_accounts) == 2
    providers = {acc.provider for acc in user.oauth_accounts}
    assert providers == {"github", "google"}
