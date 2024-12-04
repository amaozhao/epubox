import pytest
from pydantic import AnyHttpUrl, EmailStr

from app.core.config import Settings


def test_settings_default_values():
    """测试配置的默认值"""
    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///./test.db",
        SECRET_KEY="test-secret-key",
        FIRST_SUPERUSER_EMAIL="admin@example.com",
        FIRST_SUPERUSER_PASSWORD="admin123",
    )

    assert settings.PROJECT_NAME == "EPUBox"  # 使用正确的大小写
    assert settings.VERSION == "1.0.0"
    assert settings.API_V1_STR == "/api/v1"
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 30
    assert settings.ALGORITHM == "HS256"
    assert settings.LOG_LEVEL == "INFO"
    assert settings.LOG_FORMAT == "json"
    assert settings.LOG_RENDER_JSON_LOGS is True


def test_settings_custom_values():
    """测试自定义配置值"""
    settings = Settings(
        PROJECT_NAME="CustomApp",
        VERSION="2.0.0",
        DATABASE_URL="postgresql://user:pass@localhost/db",
        SECRET_KEY="custom-secret",
        ACCESS_TOKEN_EXPIRE_MINUTES=60,
        ALGORITHM="RS256",
        BACKEND_CORS_ORIGINS=["http://localhost:3000"],
        GOOGLE_CLIENT_ID="google-id",
        GOOGLE_CLIENT_SECRET="google-secret",
        GITHUB_CLIENT_ID="github-id",
        GITHUB_CLIENT_SECRET="github-secret",
        FIRST_SUPERUSER_EMAIL="custom@example.com",  # 直接使用字符串
        FIRST_SUPERUSER_PASSWORD="custom123",
        LOG_LEVEL="DEBUG",
        LOG_FORMAT="console",
    )

    assert settings.PROJECT_NAME == "CustomApp"
    assert settings.VERSION == "2.0.0"
    assert settings.DATABASE_URL == "postgresql://user:pass@localhost/db"
    assert settings.SECRET_KEY == "custom-secret"
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 60
    assert settings.ALGORITHM == "RS256"
    assert isinstance(settings.BACKEND_CORS_ORIGINS[0], AnyHttpUrl)
    assert str(settings.BACKEND_CORS_ORIGINS[0]) == "http://localhost:3000/"
    assert settings.GOOGLE_CLIENT_ID == "google-id"
    assert settings.GOOGLE_CLIENT_SECRET == "google-secret"
    assert settings.GITHUB_CLIENT_ID == "github-id"
    assert settings.GITHUB_CLIENT_SECRET == "github-secret"
    assert (
        str(settings.FIRST_SUPERUSER_EMAIL) == "custom@example.com"
    )  # 使用 str() 进行比较
    assert settings.FIRST_SUPERUSER_PASSWORD == "custom123"
    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.LOG_FORMAT == "console"


def test_cors_origins_validator():
    """测试 CORS 源验证器"""
    # 测试字符串输入
    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///./test.db",
        SECRET_KEY="test-secret-key",
        FIRST_SUPERUSER_EMAIL="admin@example.com",
        FIRST_SUPERUSER_PASSWORD="admin123",
        BACKEND_CORS_ORIGINS="http://localhost:3000",
    )
    assert isinstance(settings.BACKEND_CORS_ORIGINS[0], AnyHttpUrl)
    assert str(settings.BACKEND_CORS_ORIGINS[0]) == "http://localhost:3000/"

    # 测试列表输入
    settings = Settings(
        DATABASE_URL="sqlite+aiosqlite:///./test.db",
        SECRET_KEY="test-secret-key",
        FIRST_SUPERUSER_EMAIL="admin@example.com",
        FIRST_SUPERUSER_PASSWORD="admin123",
        BACKEND_CORS_ORIGINS=["http://localhost:3000", "https://example.com"],
    )
    assert len(settings.BACKEND_CORS_ORIGINS) == 2
    assert all(
        isinstance(origin, AnyHttpUrl) for origin in settings.BACKEND_CORS_ORIGINS
    )
    assert str(settings.BACKEND_CORS_ORIGINS[0]) == "http://localhost:3000/"
    assert str(settings.BACKEND_CORS_ORIGINS[1]) == "https://example.com/"


def test_invalid_settings():
    """测试无效的配置值"""
    # 测试无效的邮箱
    with pytest.raises(ValueError):
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            SECRET_KEY="test-secret-key",
            FIRST_SUPERUSER_EMAIL="invalid-email",
            FIRST_SUPERUSER_PASSWORD="admin123",
        )

    # 测试无效的 CORS 源
    with pytest.raises(ValueError):
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            SECRET_KEY="test-secret-key",
            FIRST_SUPERUSER_EMAIL="admin@example.com",
            FIRST_SUPERUSER_PASSWORD="admin123",
            BACKEND_CORS_ORIGINS=["invalid-url"],
        )

    # 测试无效的日志级别
    with pytest.raises(ValueError):
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            SECRET_KEY="test-secret-key",
            FIRST_SUPERUSER_EMAIL="admin@example.com",
            FIRST_SUPERUSER_PASSWORD="admin123",
            LOG_LEVEL="INVALID",
        )

    # 测试无效的日志格式
    with pytest.raises(ValueError):
        Settings(
            DATABASE_URL="sqlite+aiosqlite:///./test.db",
            SECRET_KEY="test-secret-key",
            FIRST_SUPERUSER_EMAIL="admin@example.com",
            FIRST_SUPERUSER_PASSWORD="admin123",
            LOG_FORMAT="INVALID",
        )
