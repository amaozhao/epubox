from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.auth import router as auth_router
from app.core.config import settings
from app.db.base import Base, get_async_session
from app.db.models import User
from app.schemas.user import UserRead
from tests.conftest import TestingSessionLocal, engine


class TestAuth:
    """Test cases for authentication endpoints."""

    @pytest.fixture
    def mock_oauth_settings(self, monkeypatch):
        """Mock OAuth settings"""
        monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "mock_google_client_id")
        monkeypatch.setattr(
            settings, "GOOGLE_CLIENT_SECRET", "mock_google_client_secret"
        )
        monkeypatch.setattr(settings, "GITHUB_CLIENT_ID", "mock_github_client_id")
        monkeypatch.setattr(
            settings, "GITHUB_CLIENT_SECRET", "mock_github_client_secret"
        )

    @pytest.fixture
    async def setup_database(self):
        """设置测试数据库"""
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @pytest.fixture
    async def db_session(self, setup_database) -> AsyncSession:
        """提供异步数据库会话"""
        async with TestingSessionLocal() as session:
            try:
                yield session
            finally:
                await session.rollback()
                await session.close()

    @pytest.fixture
    def app(self, db_session: AsyncSession, mock_oauth_settings):
        """创建测试应用"""
        app = FastAPI()

        async def get_test_session():
            yield db_session

        app.dependency_overrides[get_async_session] = get_test_session
        app.include_router(auth_router, prefix="/api/v1/auth")
        return app

    @pytest.fixture
    def client(self, app):
        """创建测试客户端"""
        return TestClient(app)

    @pytest.mark.asyncio
    async def test_register_endpoint(self, client, db_session: AsyncSession):
        """测试用户注册端点"""
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "test@example.com",
                "password": "string",
                "username": "testuser",
                "is_active": True,
                "is_superuser": False,
                "is_verified": False,
            },
        )
        assert response.status_code == 201

        # 从数据库中获取用户并显式加载关系
        query = (
            select(User)
            .where(User.email == "test@example.com")
            .options(selectinload(User.oauth_accounts))
        )
        result = await db_session.execute(query)
        user = result.scalar_one()

        # 使用 Pydantic 模型验证用户数据
        user_data = UserRead.model_validate(user)
        assert user_data.email == "test@example.com"
        assert user_data.username == "testuser"
        assert "hashed_password" not in response.json()

    @pytest.mark.asyncio
    async def test_login_endpoint(self, client):
        """测试用户登录端点"""
        # 先注册用户
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "login_test@example.com",
                "password": "string",
                "username": "loginuser",
                "is_active": True,
                "is_superuser": False,
                "is_verified": False,
            },
        )

        # 测试登录
        response = client.post(
            "/api/v1/auth/jwt/login",
            data={
                "username": "login_test@example.com",
                "password": "string",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    @pytest.mark.asyncio
    @patch("httpx_oauth.clients.github.GitHubOAuth2.get_authorization_url")
    async def test_github_oauth_login(self, mock_get_auth_url, client):
        """测试 GitHub OAuth 登录"""
        mock_auth_url = "https://mock-github.com/auth"

        async def mock_get_auth_url_impl(*args, **kwargs):
            return mock_auth_url

        mock_get_auth_url.side_effect = mock_get_auth_url_impl

        response = client.get("/api/v1/auth/github/authorize")
        assert response.status_code == 200
        data = response.json()
        assert "authorization_url" in data
        assert data["authorization_url"] == mock_auth_url

    @pytest.mark.asyncio
    @patch("httpx_oauth.clients.google.GoogleOAuth2.get_authorization_url")
    async def test_google_oauth_login(self, mock_get_auth_url, client):
        """测试 Google OAuth 登录"""
        mock_auth_url = "https://mock-google.com/auth"

        async def mock_get_auth_url_impl(*args, **kwargs):
            return mock_auth_url

        mock_get_auth_url.side_effect = mock_get_auth_url_impl

        response = client.get("/api/v1/auth/google/authorize")
        assert response.status_code == 200
        data = response.json()
        assert "authorization_url" in data
        assert data["authorization_url"] == mock_auth_url

    @pytest.mark.asyncio
    async def test_get_current_user(self, client, db_session: AsyncSession):
        """测试获取当前用户信息"""
        # 先注册并登录用户
        user_data = {
            "email": "current_user@example.com",
            "password": "string",
            "username": "currentuser",
        }

        register_response = client.post(
            "/api/v1/auth/register",
            json=user_data,
        )
        assert register_response.status_code == 201

        login_response = client.post(
            "/api/v1/auth/jwt/login",
            data={
                "username": user_data["email"],
                "password": user_data["password"],
            },
        )
        assert login_response.status_code == 200
        token = login_response.json()["access_token"]

        # 测试获取当前用户信息
        response = client.get(
            "/api/v1/auth/users/me",  # 修改为正确的路径：/api/v1/auth + /users/me
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        user_data = response.json()
        assert user_data["email"] == "current_user@example.com"
        assert user_data["username"] == "currentuser"
        assert "oauth_accounts" in user_data
        assert isinstance(user_data["oauth_accounts"], list)
