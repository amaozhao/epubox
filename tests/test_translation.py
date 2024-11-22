from unittest.mock import patch
import pytest
from app.services.translation.base import (
    TranslationRequest,
    TranslationResponse,
    TranslationAPIError,
    TranslationQuotaExceededError,
    UnsupportedLanguageError
)
from app.services.translation.google_translate import GoogleTranslateAdapter
from tests.mocks import mock_httpx, MockAsyncClient

@pytest.fixture
def translator():
    return GoogleTranslateAdapter()

@pytest.mark.asyncio
async def test_translate_text_success(translator):
    """Test successful text translation."""
    with patch('app.services.translation.google_translate.httpx', mock_httpx):
        request = TranslationRequest(
            text="Hello world",
            source_language="en",
            target_language="zh-CN"
        )
        response = await translator.translate_text(request)
        assert isinstance(response, TranslationResponse)
        assert response.translated_text == "Hello World"
        assert response.source_language == "en"
        assert response.target_language == "zh-CN"

@pytest.mark.asyncio
async def test_translate_empty_text(translator):
    """Test translation of empty text."""
    request = TranslationRequest(
        text="",
        source_language="auto",
        target_language="zh-CN"
    )
    response = await translator.translate_text(request)
    assert isinstance(response, TranslationResponse)
    assert response.translated_text == ""
    assert response.source_language == "auto"
    assert response.target_language == "zh-CN"

@pytest.mark.asyncio
async def test_translate_unsupported_language(translator):
    """Test translation with unsupported language."""
    request = TranslationRequest(
        text="Hello world",
        source_language="xx",  # Invalid language code
        target_language="zh-CN"
    )
    with pytest.raises(UnsupportedLanguageError) as exc_info:
        await translator.translate_text(request)
    assert "Language pair not supported" in str(exc_info.value)

@pytest.mark.asyncio
async def test_translate_batch(translator, monkeypatch):
    """Test batch translation."""
    requests = [
        TranslationRequest(
            text=f"Text {i}",
            source_language="auto",
            target_language="zh-CN"
        )
        for i in range(3)
    ]
    monkeypatch.setattr(mock_httpx, "AsyncClient", MockAsyncClient)
    responses = await translator.translate_batch(requests)
    assert len(responses) == 3
    assert all(isinstance(r, TranslationResponse) for r in responses)
    assert all(r.translated_text == "Hello World" for r in responses)  # From our mock

@pytest.mark.asyncio
async def test_retry_mechanism(translator, monkeypatch):
    """Test translation retry mechanism."""
    request = TranslationRequest(
        text="Hello world",
        source_language="auto",
        target_language="zh-CN"
    )
    client = MockAsyncClient()
    MockAsyncClient.set_failure_pattern(id(client), [
        ('TimeoutException', None, None),  # First attempt: timeout
        (None, 200, {})  # Second attempt: success
    ])
    monkeypatch.setattr(mock_httpx, "AsyncClient", lambda *args, **kwargs: client)
    response = await translator.translate_text(request)
    assert isinstance(response, TranslationResponse)
    assert response.translated_text == "Hello World"
    assert response.source_language == "auto"
    assert response.target_language == "zh-CN"
    assert response.service_metadata == {"provider": "google_translate_free"}

@pytest.mark.asyncio
async def test_retry_mechanism_quota_exceeded(translator, monkeypatch):
    """Test translation retry with quota exceeded."""
    request = TranslationRequest(
        text="Hello world",
        source_language="auto",
        target_language="zh-CN"
    )
    client = MockAsyncClient()
    MockAsyncClient.set_failure_pattern(id(client), [
        (None, 429, {'Retry-After': '60'})  # Quota exceeded
    ])
    monkeypatch.setattr(mock_httpx, "AsyncClient", lambda *args, **kwargs: client)
    with pytest.raises(TranslationQuotaExceededError) as exc_info:
        await translator.translate_text(request)
    assert "Translation quota exceeded" in str(exc_info.value)

@pytest.mark.asyncio
async def test_retry_mechanism_batch(translator, monkeypatch):
    """Test batch translation retry mechanism."""
    requests = [
        TranslationRequest(
            text=f"Text {i}",
            source_language="auto",
            target_language="zh-CN"
        )
        for i in range(3)
    ]
    client = MockAsyncClient()
    MockAsyncClient.set_failure_pattern(id(client), [
        ('TimeoutException', None, None),  # First attempt: timeout
        (None, 200, {})  # Second attempt: success
    ])
    monkeypatch.setattr(mock_httpx, "AsyncClient", lambda *args, **kwargs: client)
    responses = await translator.translate_batch(requests)
    assert len(responses) == 3
    assert all(isinstance(r, TranslationResponse) for r in responses)
    assert all(r.translated_text == "Hello World" for r in responses)
    assert all(r.service_metadata == {"provider": "google_translate_free"} for r in responses)

@pytest.mark.asyncio
async def test_retry_mechanism_max_retries(translator, monkeypatch):
    """Test translation retry mechanism with max retries exceeded."""
    request = TranslationRequest(
        text="Hello world",
        source_language="auto",
        target_language="zh-CN"
    )
    client = MockAsyncClient()
    MockAsyncClient.set_failure_pattern(id(client), [
        ('TimeoutException', None, None),  # First attempt: timeout
        ('TimeoutException', None, None),  # Second attempt: timeout
        ('TimeoutException', None, None),  # Third attempt: timeout
        ('TimeoutException', None, None)   # Fourth attempt: timeout (exceeds max_retries)
    ])
    monkeypatch.setattr(mock_httpx, "AsyncClient", lambda *args, **kwargs: client)
    with pytest.raises(TranslationAPIError) as exc_info:
        await translator.translate_text(request)
    assert "Network error during translation" in str(exc_info.value)

@pytest.mark.asyncio
async def test_supported_languages(translator):
    """Test getting supported languages."""
    languages = translator.get_supported_languages()
    assert isinstance(languages, list)
    assert "en" in languages
    assert "zh-CN" in languages
    assert len(languages) > 0

@pytest.mark.asyncio
async def test_language_validation(translator):
    """Test language validation."""
    # Valid language pair
    assert await translator.validate_languages("auto", "zh-CN") is True
    assert await translator.validate_languages("en", "zh-CN") is True
    # Invalid language pair
    assert await translator.validate_languages("xx", "zh-CN") is False
    assert await translator.validate_languages("en", "xx") is False

@pytest.mark.asyncio
async def test_translation_cost(translator):
    """Test translation cost calculation."""
    cost = await translator.get_translation_cost("Hello world", "en", "zh-CN")
    assert cost == 0.0  # Free API

@pytest.mark.asyncio
async def test_detect_language(translator):
    """Test language detection."""
    detected = await translator.detect_language("Hello world")
    assert detected == "auto"  # Free API always returns 'auto'

# @pytest.mark.asyncio
# async def test_network_error():
#     """Test behavior on network error."""
#     client = MockAsyncClient()
#     client.set_failure_pattern(id(client), [
#         (mock_httpx.NetworkError, None, None)  # Network error
#     ])
#     
#     with patch('app.services.translation.google_translate.httpx.AsyncClient', return_value=client):
#         async with GoogleTranslateAdapter() as translator:
#             request = TranslationRequest(
#                 text="Hello world",
#                 source_language="auto",
#                 target_language="zh-CN"
#             )
#             
#             with pytest.raises(TranslationAPIError) as exc_info:
#                 await translator.translate_text(request)
#             assert "Network error during translation" in str(exc_info.value)
#             assert exc_info.value.service == "google_translate"
#             assert "error_type" in exc_info.value.details
#             assert exc_info.value.details["error_type"] == "NetworkError"

@pytest.mark.asyncio
async def test_quota_exceeded():
    """Test behavior when quota is exceeded."""
    client = MockAsyncClient()
    client.set_failure_pattern(id(client), [
        (None, 429, {'Retry-After': '60'})  # Quota exceeded
    ])
    
    with patch('app.services.translation.google_translate.httpx.AsyncClient', return_value=client):
        async with GoogleTranslateAdapter() as translator:
            request = TranslationRequest(
                text="Hello world",
                source_language="auto",
                target_language="zh-CN"
            )
            
            with pytest.raises(TranslationQuotaExceededError) as exc_info:
                await translator.translate_text(request)
            assert "Translation quota exceeded" in str(exc_info.value)
            assert exc_info.value.service == "google_translate"
            assert "retry_after" in exc_info.value.details
            assert exc_info.value.details["retry_after"] == "60"

@pytest.mark.asyncio
async def test_batch_translation():
    """Test batch translation functionality."""
    with patch('app.services.translation.google_translate.httpx.AsyncClient', MockAsyncClient):
        async with GoogleTranslateAdapter() as translator:
            requests = [
                TranslationRequest(text=f"Text {i}", source_language="auto", target_language="zh-CN")
                for i in range(3)
            ]

            responses = await translator.translate_batch(requests)
            assert len(responses) == 3
            for response in responses:
                assert isinstance(response, TranslationResponse)
                assert response.translated_text == "Hello World"

@pytest.mark.asyncio
async def test_context_manager():
    """Test async context manager functionality."""
    with patch('app.services.translation.google_translate.httpx.AsyncClient', MockAsyncClient):
        async with GoogleTranslateAdapter() as translator:
            request = TranslationRequest(
                text="Hello world",
                source_language="auto",
                target_language="zh-CN"
            )
            response = await translator.translate_text(request)
            assert isinstance(response, TranslationResponse)
            assert response.translated_text == "Hello World"

@pytest.mark.asyncio
async def test_invalid_language():
    """Test behavior with invalid language code."""
    with patch('app.services.translation.google_translate.httpx.AsyncClient', MockAsyncClient):
        async with GoogleTranslateAdapter() as translator:
            request = TranslationRequest(
                text="Hello world",
                source_language="invalid",
                target_language="en"
            )
            
            with pytest.raises(UnsupportedLanguageError) as exc_info:
                await translator.translate_text(request)
            assert "Language pair not supported: invalid -> en" in str(exc_info.value)
