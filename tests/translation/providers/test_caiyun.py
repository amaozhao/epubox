"""Test Caiyun translation provider."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import tenacity

from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.caiyun import CaiyunProvider


class TestCaiyunProvider:
    """Test cases for Caiyun translation provider."""

    @pytest.fixture
    def provider_model(self):
        """Create a mock provider model for testing."""
        return TranslationProviderModel(
            name="caiyun",
            provider_type="caiyun",
            config={"api_key": "test_key"},
            enabled=True,
            is_default=False,
            rate_limit=3,
            retry_count=3,
            retry_delay=5,
            limit_type=LimitType.CHARS,
            limit_value=4000,
        )

    @pytest.fixture
    async def provider(self, provider_model):
        """Create a provider instance for testing."""
        provider = CaiyunProvider(provider_model)
        await provider._initialize()
        yield provider
        await provider._cleanup()

    def test_provider_type(self, provider):
        """Test provider type."""
        assert provider.get_provider_type() == "caiyun"

    def test_validate_config(self):
        """Test config validation."""
        # 测试没有 API key
        provider_model = TranslationProviderModel(
            name="caiyun",
            provider_type="caiyun",
            config={},
            enabled=True,
            is_default=False,
            limit_type=LimitType.CHARS,
            limit_value=4000,
        )
        with pytest.raises(ConfigurationError) as exc_info:
            CaiyunProvider(provider_model)
        assert "API key is required" in str(exc_info.value)

        # 测试错误的限制类型
        provider_model = TranslationProviderModel(
            name="caiyun",
            provider_type="caiyun",
            config={"api_key": "test_key"},
            enabled=True,
            is_default=False,
            limit_type=LimitType.TOKENS,
            limit_value=4000,
        )
        with pytest.raises(ValueError) as exc_info:
            CaiyunProvider(provider_model)
        assert "must use character-based limits" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_translate_success(self, provider):
        """Test successful translation."""
        test_text = "你好，世界！"
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"target": "Hello, world!"})

        # Mock httpx client's post method
        provider.client.post = AsyncMock(return_value=mock_response)
        result = await provider.translate(test_text, "ZH", "en")
        assert result == "Hello, world!"

    @pytest.mark.asyncio
    async def test_translate_with_number(self, provider):
        """Test translation with number prefix."""
        test_text = "1\n你好，世界！"
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"target": "Hello, world!"})

        # Mock httpx client's post method
        provider.client.post = AsyncMock(return_value=mock_response)
        result = await provider.translate(test_text, "ZH", "en")
        assert result == "1\nHello, world!"

    @pytest.mark.asyncio
    async def test_translate_retry_on_error(self, provider):
        """Test translation retry mechanism."""
        test_text = "你好，世界！"
        mock_success = AsyncMock()
        mock_success.status_code = 200
        mock_success.json = MagicMock(return_value={"target": "Hello, world!"})

        # First request fails, second succeeds
        provider.client.post = AsyncMock(
            side_effect=[httpx.HTTPError("Test error"), mock_success]
        )
        result = await provider.translate(test_text, "ZH", "en")
        assert result == "Hello, world!"

    @pytest.mark.asyncio
    async def test_translate_max_retries_exceeded(self, provider):
        """Test translation when max retries are exceeded."""
        test_text = "你好，世界！"
        # All requests fail
        provider.client.post = AsyncMock(side_effect=httpx.HTTPError("Test error"))
        with pytest.raises(tenacity.RetryError):
            await provider.translate(test_text, "ZH", "en")

    @pytest.mark.asyncio
    async def test_translate_invalid_response(self, provider):
        """Test handling of invalid response."""
        test_text = "你好，世界！"
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(return_value={"invalid": "response"})

        # Mock httpx client's post method
        provider.client.post = AsyncMock(return_value=mock_response)
        with pytest.raises(tenacity.RetryError):
            await provider.translate(test_text, "ZH", "en")

    @pytest.mark.asyncio
    async def test_translate_unsupported_language(self, provider):
        """Test handling of unsupported target language."""
        test_text = "你好，世界！"
        with pytest.raises(tenacity.RetryError):
            await provider.translate(test_text, "ZH", "fr")

    @pytest.mark.asyncio
    async def test_translate_without_initialization(self, provider):
        """Test translation without initialization."""
        await provider._cleanup()
        with pytest.raises(ConfigurationError):
            await provider._translate(
                text="你好，世界！", source_lang="ZH", target_lang="en"
            )
