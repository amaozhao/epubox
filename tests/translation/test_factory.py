"""Test translation provider factory."""

from unittest.mock import Mock, patch

import pytest

from app.translation.errors import ConfigurationError
from app.translation.factory import ProviderFactory
from app.translation.models import TranslationProvider as ProviderModel
from app.translation.providers.base import TranslationProvider


class MockProvider(TranslationProvider):
    """Mock provider for testing."""

    @classmethod
    def get_provider_type(cls) -> str:
        return "mock"

    def validate_config(self, config: dict):
        pass

    async def _translate(self, text: str, source_lang: str, target_lang: str, **kwargs):
        return "mock translation"


@pytest.fixture
def mock_configs():
    """Provider configurations for testing."""
    return {
        "mock": {"module": "tests.translation.test_factory", "class": "MockProvider"}
    }


@pytest.fixture
def provider_factory(mock_configs):
    """Create a provider factory instance for testing."""
    with patch("yaml.safe_load") as mock_load:
        mock_load.return_value = mock_configs
        factory = ProviderFactory()
        return factory


@pytest.fixture
def provider_model():
    """Create a provider model for testing."""
    return ProviderModel(
        id=1,
        name="Mock Provider",
        provider_type="mock",
        is_default=True,
        enabled=True,
        config={"api_key": "test_key", "model": "test-model"},
        rate_limit=1,
        retry_count=3,
        retry_delay=1,
    )


def test_load_provider_class(provider_factory, mock_configs):
    """Test loading provider class from configuration."""
    provider_class = provider_factory._load_provider_class("mock")
    assert provider_class == MockProvider


def test_load_provider_class_invalid_type(provider_factory):
    """Test loading provider class with invalid type."""
    with pytest.raises(ValueError) as exc_info:
        provider_factory._load_provider_class("invalid")
    assert "No configuration found for provider" in str(exc_info.value)


def test_load_provider_class_invalid_module(provider_factory, mock_configs):
    """Test loading provider class with invalid module."""
    # Modify the config to use an invalid module
    provider_factory._provider_configs["mock"]["module"] = "invalid.module"

    with pytest.raises(ValueError) as exc_info:
        provider_factory._load_provider_class("mock")
    assert "Failed to load provider" in str(exc_info.value)


def test_get_provider_class(provider_factory):
    """Test getting provider class."""
    # First call should load the class
    provider_class = provider_factory.get_provider_class("mock")
    assert provider_class == MockProvider

    # Second call should use cached class
    cached_class = provider_factory.get_provider_class("mock")
    assert cached_class == provider_class
    assert provider_factory._provider_classes["mock"] == MockProvider


def test_create_provider(provider_factory, provider_model):
    """Test creating provider instance."""
    provider = provider_factory.create_provider(provider_model)
    assert isinstance(provider, MockProvider)
    assert provider.provider_model == provider_model
