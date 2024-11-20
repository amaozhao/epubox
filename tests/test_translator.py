import pytest
from services.epub_book.translator import MistralTranslationService, TranslationConfig, TranslationProvider

@pytest.mark.asyncio
async def test_translate_batch():
    # Create test configuration
    config = TranslationConfig(
        provider=TranslationProvider.MISTRAL,
        source_lang="en",
        target_lang="zh",
        max_chars=2000,
        temperature=0.3,
        top_p=0.9
    )
    
    # Create the translation service
    service = MistralTranslationService(config)
    
    # Test data with HTML tags and different lengths
    test_texts = [
        "Hello world",
        "<p>This is a test</p>",
        "<h1>Chapter 1</h1><p>A longer paragraph with some <b>bold</b> text.</p>",
    ]
    
    # Call the batch translation method
    translations = await service.translate_batch(test_texts)
    
    # Verify the results
    assert len(translations) == len(test_texts), "Number of translations should match input texts"
    
    # Check that HTML tags are preserved in the translations
    assert "<p>" in translations[1], "HTML tags should be preserved in translations"
    assert "<h1>" in translations[2], "HTML tags should be preserved in translations"
    assert "<b>" in translations[2], "Nested HTML tags should be preserved"
    
    # Check that each translation is not empty and is different from the original
    for original, translated in zip(test_texts, translations):
        assert translated, "Translation should not be empty"
        assert translated != original, "Translation should be different from original text"
