"""Test settings module."""

import logging
import os
from pathlib import Path

import pytest

from src.infrastructure.config import Settings


@pytest.fixture
def mock_env(monkeypatch):
    """Mock environment variables."""
    env_vars = {
        "APP_NAME": "epubox-test",
        "DEBUG": "true",
        "DATABASE_URL": "postgresql+asyncpg://user:password@localhost/epubox",
        "LOG_LEVEL": str(logging.INFO),
        "SECRET_KEY": "test-key",
        "OPENAI_API_KEY": "test-openai-key",
        "GOOGLE_API_KEY": "test-google-key",
        "MISTRAL_API_KEY": "test-mistral-key",
        "DEEPL_API_KEY": "test-deepl-key",
        "TRANSLATION_API_KEY": "test-key",
    }

    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)

    return env_vars


class TestSettings:
    """Test settings class."""

    def test_load_settings(self, mock_env):
        """Test loading settings from environment."""
        settings = Settings()

        assert settings.APP_NAME == "epubox-test"
        assert settings.DEBUG is True
        assert (
            settings.DATABASE_URL
            == "postgresql+asyncpg://user:password@localhost/epubox"
        )
        assert settings.LOG_LEVEL == logging.INFO
        assert settings.SECRET_KEY == "test-key"
        assert settings.OPENAI_API_KEY == "test-openai-key"
        assert settings.GOOGLE_API_KEY == "test-google-key"
        assert settings.MISTRAL_API_KEY == "test-mistral-key"
        assert settings.DEEPL_API_KEY == "test-deepl-key"
        assert settings.TRANSLATION_API_KEY == "test-key"

    def test_default_settings(self):
        """Test default settings."""
        settings = Settings()

        assert settings.APP_NAME == "EPUBox"
        assert settings.DEBUG is False
        assert settings.DATABASE_URL == "sqlite+aiosqlite:///./epubox.db"
        assert settings.LOG_LEVEL == logging.INFO
        assert isinstance(settings.UPLOAD_DIR, Path)
        assert isinstance(settings.TRANSLATION_DIR, Path)
        assert isinstance(settings.LOG_DIR, Path)
        assert settings.OPENAI_API_KEY == "sk-123"
        assert settings.GOOGLE_API_KEY == "test-google-key"
        assert settings.MISTRAL_API_KEY == "Hmpty6LRYAgJ28YIDr837aNgLg5JVfnD"
        assert settings.DEEPL_API_KEY == "test-deepl-key"
