import pytest
import asyncio
from services.epub_book.translator import (
    TranslationService,
    TranslationConfig,
    TranslationProvider,
    MockTranslationService
)

@pytest.fixture
def mock_config():
    return TranslationConfig(
        provider=TranslationProvider.MOCK,
        source_lang="en",
        target_lang="zh",
        max_chars=100,
        preserve_tags=True
    )

@pytest.fixture
def mock_translator(mock_config):
    return MockTranslationService(mock_config)

@pytest.mark.asyncio
async def test_batch_translation_basic():
    """Test basic batch translation functionality."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    texts = ["Hello", "World", "Test"]
    result = await translator._translate_batch(texts)
    
    assert len(result) == len(texts)
    assert all(isinstance(r, str) for r in result)
    assert all(r.startswith("[MOCK]") for r in result)

@pytest.mark.asyncio
async def test_batch_creation():
    """Test batch creation with token limits."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    # Create texts that should be split into multiple batches
    # Each text is about 250 tokens (1000 chars / 4)
    long_texts = ["A" * 1000, "B" * 1000, "C" * 1000]
    
    # First, test batch creation directly
    batches = translator._create_batches(long_texts)
    assert len(batches) > 1, "Should split into multiple batches due to token limit"
    
    # Then test actual translation
    results = await translator._translate_batch(long_texts)
    assert len(results) == len(long_texts)

@pytest.mark.asyncio
async def test_rate_limiting():
    """Test rate limiting functionality."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    start_time = asyncio.get_event_loop().time()
    
    # Make multiple requests that should be rate limited
    texts = ["Test1", "Test2", "Test3"]
    await translator._translate_batch(texts)
    
    end_time = asyncio.get_event_loop().time()
    time_taken = end_time - start_time
    
    # Should take at least min_request_interval * (number of batches - 1) seconds
    min_expected_time = translator.min_request_interval * (len(translator._create_batches(texts)) - 1)
    assert time_taken >= min_expected_time, f"Rate limiting should enforce minimum delay of {min_expected_time}s between requests"

@pytest.mark.asyncio
async def test_html_preservation():
    """Test HTML tag preservation during batch translation."""
    config = TranslationConfig(
        provider=TranslationProvider.MOCK,
        preserve_tags=True
    )
    translator = MockTranslationService(config)
    
    html_texts = [
        "<p>Hello</p>",
        "<div class='test'>World</div>",
        "<span id='123'>Test</span>"
    ]
    
    results = await translator._translate_batch(html_texts)
    
    assert len(results) == len(html_texts)
    assert all("<" in r and ">" in r for r in results)
    assert all("[MOCK HTML]" in r for r in results)

@pytest.mark.asyncio
async def test_error_handling():
    """Test error handling during batch translation."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    # Include None to simulate an error
    texts = ["Valid", None, "Also Valid"]
    
    results = await translator._translate_batch(texts)
    
    assert len(results) == len(texts)
    assert results[0] is not None
    assert results[1] is not None
    assert "[MOCK ERROR]" in results[1]
    assert results[2] is not None

@pytest.mark.asyncio
async def test_empty_batch():
    """Test handling of empty batch."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    results = await translator._translate_batch([])
    assert results == []

@pytest.mark.asyncio
async def test_large_batch():
    """Test handling of a large batch of texts."""
    config = TranslationConfig(provider=TranslationProvider.MOCK)
    translator = MockTranslationService(config)
    
    # Create a large batch of texts
    texts = [f"Text {i}" for i in range(100)]
    results = await translator._translate_batch(texts)
    
    assert len(results) == len(texts)
    assert all(isinstance(r, str) for r in results)
    assert all(r.startswith("[MOCK]") for r in results)