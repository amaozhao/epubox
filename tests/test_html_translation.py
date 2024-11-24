import pytest
from bs4 import BeautifulSoup
from typing import List
from app.services.translation.html_processor import HTMLProcessor, TextFragment
from app.services.translation.semantic import SemanticTranslationService, TranslationService
from app.services.translation.base import BaseTranslationAdapter, TranslationRequest, TranslationResponse

class MockTranslator(BaseTranslationAdapter):
    def __init__(self):
        super().__init__(api_key="mock_key")

    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        return TranslationResponse(
            translated_text=f"[{request.target_language}]{request.text}",
            source_language=request.source_language,
            target_language=request.target_language,
            confidence=1.0
        )

    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        responses = []
        for request in requests:
            response = await self.translate_text(request)
            responses.append(response)
        return responses

    async def detect_language(self, text: str) -> str:
        return "en"

    def get_supported_languages(self) -> List[str]:
        return ["en", "zh", "ja", "ko"]

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        return len(text) * 0.001

    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        supported = self.get_supported_languages()
        return source_lang in supported and target_lang in supported

class MockTranslationService(TranslationService):
    async def translate_text(self, text: str, source_lang: str, target_lang: str) -> str:
        return f"[{target_lang}]{text}"

    async def translate_batch(self, texts: List[str], source_language: str, target_language: str) -> List[str]:
        return [f"[{target_language}]{text}" for text in texts]

@pytest.fixture
def html_processor():
    return HTMLProcessor()

@pytest.fixture
def translation_service():
    return SemanticTranslationService(MockTranslator())

def test_html_processor_skip_tags(html_processor):
    html = """
    <div>
        <p>Translate this</p>
        <code>Don't translate this</code>
        <p>Translate this too</p>
    </div>
    """
    soup = html_processor.parse_html(html)
    fragments = html_processor.extract_text_fragments(soup)
    
    # Get normalized text content (strip whitespace)
    texts = [f.text.strip() for f in fragments]
    
    # Verify number of fragments and their content
    assert len(fragments) == 2, f"Expected 2 fragments, got {len(fragments)}: {texts}"
    assert "Translate this" in texts, f"Expected 'Translate this' in {texts}"
    assert "Translate this too" in texts, f"Expected 'Translate this too' in {texts}"
    assert "Don't translate this" not in texts, f"Found 'Don't translate this' in {texts}"
    
    # Verify that code tag was skipped
    assert all('code' not in f.path for f in fragments), \
        f"Found code tag in paths: {[f.path for f in fragments]}"

def test_html_processor_text_splitting(html_processor):
    # Create a text with 20 sentences, each with 10 words
    sentence = "This is a test sentence with exactly ten words."
    long_text = (sentence + " ") * 20
    html = f"<p>{long_text}</p>"
    
    # Set max_tokens to 50 (5 sentences worth)
    html_processor.max_tokens = 50
    
    soup = html_processor.parse_html(html)
    fragments = html_processor.extract_text_fragments(soup)
    
    # With 200 total words and max_tokens=50, we should get at least 4 fragments
    assert len(fragments) >= 4
    
    # Verify each fragment's token count
    for fragment in fragments:
        words = fragment.text.split()
        assert len(words) <= html_processor.max_tokens, \
            f"Fragment has {len(words)} tokens, should be <= {html_processor.max_tokens}"
        
    # Verify that each fragment (except last) ends with a period
    for fragment in fragments[:-1]:
        assert fragment.text.strip().endswith('.'), \
            f"Fragment does not end with period: {fragment.text}"

def test_html_processor_comprehensive():
    """Test comprehensive HTML processing with various elements and attributes."""
    html_content = '''
    <?xml version="1.0" encoding="utf-8"?>
    <!DOCTYPE html>
    <html xmlns="http://www.w3.org/1999/xhtml">
    <head>
        <title>Test Page</title>
        <meta charset="utf-8"/>
        <style>.notranslate { color: red; }</style>
        <script>var x = 1;</script>
    </head>
    <body>
        <h1>Main Title</h1>
        <p>Regular paragraph.</p>
        <p class="notranslate">Technical content</p>
        <pre>Code block</pre>
        <div translate="no">Do not translate</div>
        <img src="test.jpg" alt="Image description" title="Image title"/>
        <input type="text" placeholder="Enter name"/>
        <button aria-label="Submit form">Submit</button>
        <div contenteditable="false">Configuration: xyz</div>
        <div hidden>Hidden content</div>
        <a href="http://example.com">Link text</a>
        <code>print("Hello")</code>
    </body>
    </html>
    '''
    
    processor = HTMLProcessor()
    soup, fragments = processor.process_html(html_content)
    
    # Verify text nodes that should be translated
    translatable_texts = {f.text for f in fragments}
    assert 'Main Title' in translatable_texts
    assert 'Regular paragraph.' in translatable_texts
    assert 'Link text' in translatable_texts
    
    # Verify attributes that should be translated
    translatable_attrs = {f.text for f in fragments if ' @' in f.path}
    assert 'Image description' in translatable_attrs
    assert 'Image title' in translatable_attrs
    assert 'Enter name' in translatable_attrs
    assert 'Submit form' in translatable_attrs
    
    # Verify content that should not be translated
    assert 'Technical content' not in translatable_texts
    assert 'Code block' not in translatable_texts
    assert 'Do not translate' not in translatable_texts
    assert 'Configuration: xyz' not in translatable_texts
    assert 'Hidden content' not in translatable_texts
    assert 'print("Hello")' not in translatable_texts
    assert 'var x = 1' not in translatable_texts
    
    # Test translation
    translations = [(f, f'[zh]{f.text}') for f in fragments]
    translated_html = processor.rebuild_html(soup, translations)
    
    # Verify translations in output
    assert '[zh]Main Title' in translated_html
    assert '[zh]Regular paragraph' in translated_html
    assert '[zh]Link text' in translated_html
    assert '[zh]Image description' in translated_html
    assert '[zh]Image title' in translated_html
    assert '[zh]Enter name' in translated_html
    assert '[zh]Submit form' in translated_html
    
    # Verify untranslated content remains unchanged
    assert 'Technical content' in translated_html
    assert 'Code block' in translated_html
    assert 'Do not translate' in translated_html
    assert 'Configuration: xyz' in translated_html
    assert 'Hidden content' in translated_html
    assert 'print("Hello")' in translated_html

@pytest.mark.asyncio
async def test_semantic_translation(translation_service):
    html = """
    <div>
        <h1>Title</h1>
        <p>First paragraph</p>
        <code>print("Hello")</code>
        <p>Second paragraph</p>
    </div>
    """
    
    translated_html = await translation_service.translate_html(
        chapter_id="test_chapter",
        html_content=html,
        source_lang="en",
        target_lang="zh",
    )
    
    assert "[zh]Title" in translated_html
    assert "[zh]First paragraph" in translated_html
    assert "print(\"Hello\")" in translated_html  # Code should not be translated
    assert "[zh]Second paragraph" in translated_html

@pytest.mark.asyncio
async def test_translation_progress(translation_service):
    html = """
    <div>
        <p>Text 1</p>
        <p>Text 2</p>
    </div>
    """
    
    chapter_id = "test_progress"
    
    # First translation
    await translation_service.translate_html(
        chapter_id=chapter_id,
        html_content=html,
        source_lang="en",
        target_lang="zh",
    )
    
    progress = translation_service.get_progress(chapter_id)
    assert progress.total_fragments == 2
    assert progress.completed_fragments == 2
    
    # Clear progress
    translation_service.clear_progress(chapter_id)
    assert translation_service.get_progress(chapter_id) is None
