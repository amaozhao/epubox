"""Test DeepL translation provider."""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
import tenacity

from app.db.models import LimitType
from app.db.models import TranslationProvider as TranslationProviderModel
from app.translation.errors import ConfigurationError, TranslationError
from app.translation.providers.deepl import DeepLProvider


@pytest_asyncio.fixture
async def provider():
    """Create a test provider instance."""
    provider_model = TranslationProviderModel(
        name="deepl",
        provider_type="deepl",
        config={},
        enabled=True,
        is_default=False,
        rate_limit=3,
        retry_count=3,
        retry_delay=5,
        limit_type=LimitType.CHARS,
        limit_value=4000,
    )
    provider = DeepLProvider(provider_model)
    await provider._initialize()
    yield provider
    await provider._cleanup()


@pytest.mark.asyncio
async def test_initialization(provider):
    """Test provider initialization."""
    assert provider.get_provider_type() == "deepl"
    assert provider.provider_model.limit_type == LimitType.CHARS
    assert isinstance(provider.client, httpx.AsyncClient)

    # 测试清理
    await provider._cleanup()
    assert provider.client is None


@pytest.mark.asyncio
async def test_translate_success(provider):
    """Test successful translation."""
    # 设置模拟响应
    mock_response = {
        "result": {
            "texts": [{"text": "Hello, world!"}],
            "lang": "en",
        }
    }

    # 创建模拟响应对象
    mock_response_obj = AsyncMock()
    mock_response_obj.status_code = 200
    mock_response_obj.json = MagicMock(return_value=mock_response)

    # 创建模拟客户端
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response_obj)

    provider.client = mock_client

    # 直接调用 _translate 方法避免重试装饰器
    result = await provider._translate(
        text="你好，世界！", source_lang="ZH", target_lang="en"
    )

    # 验证结果
    assert result == "Hello, world!"
    assert mock_client.post.call_count == 1

    # 验证请求参数
    call_args = mock_client.post.call_args
    assert call_args[1]["url"] == "https://www2.deepl.com/jsonrpc"

    # 验证请求数据
    post_data = json.loads(call_args[1]["content"])
    assert post_data["params"]["texts"][0]["text"] == "你好，世界！"
    assert post_data["params"]["lang"]["source_lang_user_selected"] == "ZH"
    assert post_data["params"]["lang"]["target_lang"] == "en"


@pytest.mark.asyncio
async def test_translate_rate_limit(provider):
    """Test rate limit handling."""
    # 设置模拟响应
    mock_response = {
        "result": {
            "texts": [{"text": "Hello, world!"}],
            "lang": "en",
        }
    }

    # 创建模拟响应对象
    mock_response_obj = AsyncMock()
    mock_response_obj.status_code = 200
    mock_response_obj.json = MagicMock(return_value=mock_response)

    # 创建模拟客户端
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response_obj)

    provider.client = mock_client

    # 连续发送多个请求
    start_time = time.time()
    for _ in range(3):
        await provider.translate(
            text="你好，世界！", source_lang="ZH", target_lang="en"
        )
    end_time = time.time()

    # 验证请求间隔至少为1秒
    assert end_time - start_time >= 2.0  # 3个请求，至少2秒间隔


@pytest.mark.asyncio
async def test_translate_too_many_requests(provider):
    """Test handling of too many requests error."""
    # 创建模拟响应对象
    mock_response_obj = AsyncMock()
    mock_response_obj.status_code = 429
    mock_response_obj.json = MagicMock(return_value={})

    # 创建模拟客户端
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response_obj)

    provider.client = mock_client

    # 直接使用 _translate 方法绕过重试逻辑
    with pytest.raises(TranslationError) as exc_info:
        await provider._translate(
            text="你好，世界！", source_lang="ZH", target_lang="en"
        )

    # 验证错误消息
    assert "Too many requests" in str(exc_info.value)
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_translate_http_error(provider):
    """Test handling of HTTP errors."""
    # 创建模拟客户端
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Connection failed"))

    provider.client = mock_client

    # 直接使用 _translate 方法绕过重试逻辑
    with pytest.raises(TranslationError) as exc_info:
        await provider._translate(
            text="你好，世界！", source_lang="ZH", target_lang="en"
        )

    # 验证错误消息
    assert "Connection failed" in str(exc_info.value)
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_translate_invalid_response(provider):
    """Test handling of invalid response."""
    # 创建模拟响应对象
    mock_response_obj = AsyncMock()
    mock_response_obj.status_code = 200
    mock_response_obj.json = MagicMock(return_value={"invalid": "response"})

    # 创建模拟客户端
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response_obj)

    provider.client = mock_client

    # 直接使用 _translate 方法绕过重试逻辑
    with pytest.raises(TranslationError) as exc_info:
        await provider._translate(
            text="你好，世界！", source_lang="ZH", target_lang="en"
        )

    # 验证错误消息
    assert "Invalid response format" in str(exc_info.value)
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_translate_without_initialization(provider):
    """Test translation without initialization."""
    await provider._cleanup()
    with pytest.raises(tenacity.RetryError) as exc_info:
        await provider.translate(
            text="你好，世界！", source_lang="ZH", target_lang="en"
        )

    # 获取内部的 TranslationError
    inner_exception = exc_info.value.last_attempt.exception()
    assert isinstance(inner_exception, TranslationError)
    assert "HTTP client not initialized" in str(inner_exception)


@pytest.mark.asyncio
async def test_helper_functions(provider):
    """Test helper functions."""
    # Test get_provider_type
    assert provider.get_provider_type() == "deepl"

    # Test validate_config
    assert provider.validate_config({}) is True

    # Test cleanup
    await provider._cleanup()
    assert provider.client is None
