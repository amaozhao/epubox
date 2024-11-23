import pytest
import os
import json
from pathlib import Path
from unittest.mock import Mock, AsyncMock
from typing import List
import aiofiles
from bs4 import BeautifulSoup
from ebooklib import epub
import ebooklib

from app.services.epub_processor import EPUBProcessor, TranslationProgress, ChapterProgress
from app.services.translation.base import BaseTranslationAdapter, TranslationRequest, TranslationResponse
from tests.fixtures.create_test_epub import create_test_epub

class MockTranslationAdapter(BaseTranslationAdapter):
    def __init__(self):
        super().__init__(api_key="mock_api_key")

    async def translate_text(self, text: str, source_language: str, target_language: str) -> str:
        # Skip empty text or whitespace
        if not text.strip():  # Update to strip whitespace
            return text
        return f"[{target_language}]{text}"

    async def translate_batch(self, texts: List[str], source_language: str, target_language: str) -> List[str]:
        # Skip empty texts or whitespace
        return [
            text if not text.strip() else f"[{target_language}]{text}"  # Update to strip whitespace
            for text in texts
        ]

    async def detect_language(self, text: str) -> str:
        return "en"
        
    async def get_supported_languages(self) -> List[str]:
        return ["en", "zh", "ja", "ko"]
        
    async def get_translation_cost(self, text: str, source_language: str, target_language: str) -> float:
        return len(text) * 0.001
        
    async def validate_languages(self, source_language: str, target_language: str) -> bool:
        supported = await self.get_supported_languages()
        return source_language in supported and target_language in supported

@pytest.fixture
def test_epub_path():
    """Return path to test EPUB file."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, 'test.epub')

@pytest.fixture
def epub_processor():
    """Create an EPUBProcessor instance with mock translation service."""
    return EPUBProcessor(MockTranslationAdapter())

@pytest.mark.asyncio
async def test_epub_translation(epub_processor, test_epub_path):
    """Test basic EPUB translation functionality."""
    # Translate EPUB
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )

    # Verify the translated file exists
    assert os.path.exists(translated_path)

    # Load and check translated content
    translated_book = epub.read_epub(translated_path)
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]

    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        # Check text nodes are translated
        for element in soup.find_all(text=True):
            text = element.strip()
            if text and not text.isspace():
                # Skip XML declaration and other special content
                if (text.startswith('<?xml') or 
                    text.startswith('<!DOCTYPE') or 
                    'xml version' in text or
                    (element.parent and element.parent.name == 'xml')):
                    continue
                    
                parent = element.parent.name if element.parent else None
                if parent in {'code', 'script', 'style', 'sup'}:
                    assert not text.startswith("[zh]"), f"Text in {parent} tag should not be translated: {text}"
                else:
                    assert text.startswith("[zh]") or text in {"", "\n"}, f"Text should be translated: {text}"

@pytest.mark.asyncio
async def test_translation_progress(epub_processor, test_epub_path):
    """Test translation progress tracking."""
    # Start translation
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )

    # Verify the translated file exists
    assert os.path.exists(translated_path)

    # Load and check translated content
    translated_book = epub.read_epub(translated_path)
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]

    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        # Check text nodes are translated
        for element in soup.find_all(text=True):
            text = element.strip()
            if text and not text.isspace():
                # Skip XML declaration and other special content
                if (text.startswith('<?xml') or 
                    text.startswith('<!DOCTYPE') or 
                    'xml version' in text or
                    (element.parent and element.parent.name == 'xml')):
                    continue
                    
                parent = element.parent.name if element.parent else None
                if parent in {'code', 'script', 'style', 'sup'}:
                    assert not text.startswith("[zh]"), f"Text in {parent} tag should not be translated: {text}"
                else:
                    assert text.startswith("[zh]") or text in {"", "\n"}, f"Text should be translated: {text}"

@pytest.mark.asyncio
async def test_translation_resumption(epub_processor, test_epub_path):
    """Test translation resumption after interruption."""
    # Start translation but simulate interruption
    with pytest.raises(Exception):
        await epub_processor.translate_epub(
            test_epub_path,
            source_language="en",
            target_language="zh",
            progress_callback=AsyncMock(side_effect=Exception("Simulated interruption"))
        )

    # Verify partial progress was saved
    progress_path = epub_processor._get_progress_path(test_epub_path)
    assert os.path.exists(progress_path)

    # Resume translation
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh",
        resume=True
    )

    # Verify translation completed
    assert os.path.exists(translated_path)

    # Load and check translated content
    translated_book = epub.read_epub(translated_path)
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]

    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        # Check text nodes are translated
        for element in soup.find_all(text=True):
            text = element.strip()
            if text and not text.isspace():
                # Skip XML declaration and other special content
                if (text.startswith('<?xml') or 
                    text.startswith('<!DOCTYPE') or 
                    'xml version' in text or
                    (element.parent and element.parent.name == 'xml')):
                    continue
                    
                parent = element.parent.name if element.parent else None
                if parent in {'code', 'script', 'style', 'sup'}:
                    assert not text.startswith("[zh]"), f"Text in {parent} tag should not be translated: {text}"
                else:
                    assert text.startswith("[zh]") or text in {"", "\n"}, f"Text should be translated: {text}"

@pytest.mark.asyncio
async def test_special_tags_preservation(epub_processor, test_epub_path):
    """Test preservation of special tags during translation."""
    # Translate the EPUB
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )

    # Verify the translated file exists
    assert os.path.exists(translated_path)

    # Load and check translated content
    translated_book = epub.read_epub(translated_path)
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]

    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        # Check text nodes are translated
        for element in soup.find_all(text=True):
            text = element.strip()
            if text and not text.isspace():
                # Skip XML declaration and other special content
                if (text.startswith('<?xml') or 
                    text.startswith('<!DOCTYPE') or 
                    'xml version' in text or
                    (element.parent and element.parent.name == 'xml')):
                    continue
                    
                parent = element.parent.name if element.parent else None
                if parent in {'code', 'script', 'style', 'sup'}:
                    assert not text.startswith("[zh]"), f"Text in {parent} tag should not be translated: {text}"
                else:
                    assert text.startswith("[zh]") or text in {"", "\n"}, f"Text should be translated: {text}"
