import pytest
from services.epub_book.translator import (
    Translator,
    TranslationConfig,
    TranslationProvider,
)


@pytest.fixture
def translation_config():
    """Create a test translation configuration"""
    return TranslationConfig(
        provider=TranslationProvider.MOCK,
        max_chars=50,
        separator="###",
    )


@pytest.fixture
def translator(translation_config):
    """Create a test translator instance"""
    return Translator(translation_config)


def test_merge_contents(translator):
    """Test merging content into chunks"""
    contents = [
        "Hello",
        "World",
        "This is a longer piece of text that should be in its own chunk",
        "Goodbye",
    ]
    
    merged = translator.merge_contents(contents)
    
    assert len(merged) == 3
    assert merged[0] == "Hello###World"
    assert merged[1] == "This is a longer piece of text that should be in its own chunk"
    assert merged[2] == "Goodbye"


def test_split_translations(translator):
    """Test splitting translated chunks back into individual translations"""
    translated_chunks = [
        "[Mock] Hello###World",
        "[Mock] This is a test",
        "[Mock] Goodbye",
    ]
    original_contents = ["Hello", "World", "This is a test", "Goodbye"]
    
    split = translator.split_translations(translated_chunks, original_contents)
    
    assert len(split) == 4
    assert split == [
        "[Mock] Hello",
        "World",
        "[Mock] This is a test",
        "[Mock] Goodbye",
    ]


@pytest.mark.asyncio
async def test_translate_text(translator):
    """Test translating a single piece of text"""
    text = "Hello, World!"
    translated = await translator.translate(text)
    assert translated == "[Mock] Hello, World!"


@pytest.mark.asyncio
async def test_translate_all(translator):
    """Test translating multiple pieces of text"""
    contents = [
        "Hello",
        "World",
        "This is a test",
        "Goodbye",
    ]
    
    translated = await translator.translate_all(contents)
    
    assert len(translated) == 4
    assert all(t.startswith("[Mock]") for t in translated)


def test_invalid_translation_provider():
    """Test creating a translator with an invalid provider"""
    with pytest.raises(ValueError):
        TranslationConfig(provider="invalid")
