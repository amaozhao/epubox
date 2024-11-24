import pytest
import os
import json
import logging
from pathlib import Path
from unittest.mock import Mock, AsyncMock
from typing import List
import aiofiles
from bs4 import BeautifulSoup, NavigableString
from ebooklib import epub
import ebooklib
from datetime import datetime, timedelta

from app.services.epub_processor import (
    EPUBProcessor, TranslationProgress, ChapterProgress,
    TranslationError
)
from app.services.translation.base import (
    BaseTranslationAdapter, TranslationRequest, TranslationResponse
)
from app.services.translation.html_processor import HTMLProcessor

# 设置日志级别为 DEBUG
logging.basicConfig(level=logging.DEBUG)

class MockTranslationAdapter(BaseTranslationAdapter):
    def __init__(self, simulate_errors: bool = False):
        super().__init__(api_key="mock_api_key")
        self.simulate_errors = simulate_errors
        self.error_count = 0
        self.max_errors = 2

    async def translate_text(self, request: TranslationRequest) -> TranslationResponse:
        """Translate a single piece of text."""
        if self.simulate_errors:
            self.error_count += 1
            if self.error_count <= self.max_errors:
                logging.debug(f"Simulating error {self.error_count} in translate_text")
                raise Exception(f"Simulated translation error {self.error_count}")
        
        if not request.text.strip():
            return TranslationResponse(
                translated_text=request.text,
                source_language=request.source_language,
                target_language=request.target_language,
                confidence=1.0
            )
            
        return TranslationResponse(
            translated_text=f"[{request.target_language}]{request.text}",
            source_language=request.source_language,
            target_language=request.target_language,
            confidence=1.0
        )

    async def translate_batch(self, requests: List[TranslationRequest]) -> List[TranslationResponse]:
        """Translate multiple pieces of text in batch."""
        responses = []
        for request in requests:
            response = await self.translate_text(request)
            responses.append(response)
        return responses

    async def detect_language(self, text: str) -> str:
        """Detect the language of a text."""
        return "en"  # Always return English for testing

    def get_supported_languages(self) -> List[str]:
        """Get list of supported language codes."""
        return ["en", "zh", "ja", "ko"]

    async def get_translation_cost(self, text: str, source_lang: str, target_lang: str) -> float:
        """Calculate the cost of translation."""
        return len(text) * 0.001

    async def validate_languages(self, source_lang: str, target_lang: str) -> bool:
        """Validate language pair."""
        supported = self.get_supported_languages()
        return source_lang in supported and target_lang in supported

    async def translate_html(self, html_content: str, source_lang: str, target_lang: str) -> str:
        """
        Translate HTML content while preserving HTML structure.
        This method simulates HTML translation by wrapping text content with target language marker.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        processor = HTMLProcessor()
        
        async def process_node(node):
            if isinstance(node, NavigableString):
                if not processor._should_skip_node(node):
                    try:
                        text = str(node).strip()
                        if text:
                            request = TranslationRequest(
                                text=text,
                                source_language=source_lang,
                                target_language=target_lang
                            )
                            response = await self.translate_text(request)
                            node.replace_with(response.translated_text)
                    except Exception as e:
                        raise e
            else:
                for child in node.children:
                    await process_node(child)
        
        try:
            await process_node(soup)
            return str(soup)
        except Exception as e:
            raise e

@pytest.fixture
def test_epub_path():
    """Return path to test EPUB file."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(current_dir, 'test.epub')

@pytest.fixture
def epub_processor():
    """Create an EPUBProcessor instance with mock translation service."""
    return EPUBProcessor(MockTranslationAdapter())

@pytest.fixture
def epub_processor_with_errors():
    """Create an EPUBProcessor instance that simulates translation errors."""
    return EPUBProcessor(MockTranslationAdapter(simulate_errors=True))

@pytest.mark.asyncio
async def test_translation_statistics(epub_processor, test_epub_path):
    """Test translation statistics tracking."""
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )

    # Verify progress file exists
    progress_path = epub_processor._get_progress_path(test_epub_path)
    assert os.path.exists(progress_path)

    # Load progress data
    progress = await epub_processor._load_progress(test_epub_path)
    
    # Verify overall statistics
    assert progress.total_chapters > 0
    assert progress.completed_chapters == progress.total_chapters
    assert progress.total_word_count > 0
    assert progress.total_character_count > 0
    assert progress.total_translation_cost > 0
    assert progress.completion_percentage == 100.0
    
    # Verify timing information
    assert progress.start_time is not None
    assert progress.end_time is not None
    assert progress.duration > 0
    
    # Verify chapter statistics
    for chapter in progress.chapters_progress.values():
        assert chapter.word_count > 0
        assert chapter.character_count > 0
        assert chapter.translation_cost > 0
        assert chapter.completion_percentage == 100.0
        assert chapter.is_completed
        assert chapter.duration is not None

@pytest.mark.asyncio
async def test_error_handling_and_retry(epub_processor_with_errors, test_epub_path, caplog):
    """Test error handling and retry mechanism."""
    caplog.set_level(logging.ERROR)
    
    # 执行翻译
    translated_path = await epub_processor_with_errors.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )
    
    # 验证翻译完成
    assert os.path.exists(translated_path)
    
    # 验证错误日志
    error_logs = [record for record in caplog.records if record.levelname == 'ERROR']
    assert len(error_logs) > 0, "No error logs were recorded"
    assert any("Simulated translation error" in record.message for record in error_logs)
    
    # 验证最终状态
    progress = await epub_processor_with_errors._load_progress(test_epub_path)
    assert progress.completion_percentage == 100.0
    assert all(chapter.is_completed for chapter in progress.chapters_progress.values())

@pytest.mark.asyncio
async def test_invalid_language_pair(epub_processor, test_epub_path):
    """Test handling of invalid language pairs."""
    with pytest.raises(ValueError) as exc_info:
        await epub_processor.translate_epub(
            test_epub_path,
            source_language="en",
            target_language="invalid"
        )
    assert "Unsupported language pair" in str(exc_info.value)

@pytest.mark.asyncio
async def test_progress_serialization(epub_processor, test_epub_path):
    """Test progress serialization and deserialization."""
    # Create a progress object
    progress = TranslationProgress(
        total_chapters=2,
        completed_chapters=1,
        current_chapter=None,
        chapters_progress={},
        start_time=datetime.now(),
        total_word_count=100,
        total_character_count=500,
        total_translation_cost=0.5
    )
    
    # Add a chapter
    chapter = ChapterProgress(
        chapter_id="ch1",
        title="Chapter 1",
        total_fragments=10,
        completed_fragments=5,
        is_completed=False,
        start_time=datetime.now(),
        word_count=50,
        character_count=250,
        translation_cost=0.25
    )
    chapter.add_error("test_error", "Test error message", "Test fragment")
    progress.chapters_progress["ch1"] = chapter
    
    # Save progress
    await epub_processor._save_progress(test_epub_path, progress)
    
    # Load progress
    loaded_progress = await epub_processor._load_progress(test_epub_path)
    
    # Verify loaded data
    assert loaded_progress.total_chapters == progress.total_chapters
    assert loaded_progress.completed_chapters == progress.completed_chapters
    assert loaded_progress.total_word_count == progress.total_word_count
    assert loaded_progress.total_character_count == progress.total_character_count
    assert abs(loaded_progress.total_translation_cost - progress.total_translation_cost) < 0.001
    
    # Verify chapter data
    loaded_chapter = loaded_progress.chapters_progress["ch1"]
    assert loaded_chapter.chapter_id == chapter.chapter_id
    assert loaded_chapter.title == chapter.title
    assert loaded_chapter.total_fragments == chapter.total_fragments
    assert loaded_chapter.completed_fragments == chapter.completed_fragments
    assert loaded_chapter.word_count == chapter.word_count
    assert loaded_chapter.character_count == chapter.character_count
    assert abs(loaded_chapter.translation_cost - chapter.translation_cost) < 0.001
    
    # Verify error data
    assert len(loaded_chapter.errors) == 1
    error = loaded_chapter.errors[0]
    assert error.error_type == "test_error"
    assert error.message == "Test error message"
    assert error.fragment == "Test fragment"

@pytest.mark.asyncio
async def test_translation_interruption_and_resume(epub_processor, test_epub_path):
    """Test translation interruption and resumption."""
    # Start translation but interrupt after first chapter
    interrupt_count = 0
    
    async def interrupt_callback(progress: TranslationProgress):
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            raise Exception("Simulated interruption")
    
    # First attempt should fail
    with pytest.raises(Exception) as exc_info:
        await epub_processor.translate_epub(
            test_epub_path,
            source_language="en",
            target_language="zh",
            progress_callback=interrupt_callback
        )
    assert "Simulated interruption" in str(exc_info.value)
    
    # Verify partial progress was saved
    progress = await epub_processor._load_progress(test_epub_path)
    assert progress is not None
    assert progress.completed_chapters < progress.total_chapters
    
    # Resume translation
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh",
        resume=True
    )
    
    # Verify translation completed
    assert os.path.exists(translated_path)
    
    # Verify final progress
    final_progress = await epub_processor._load_progress(test_epub_path)
    assert final_progress.completed_chapters == final_progress.total_chapters
    assert final_progress.completion_percentage == 100.0

@pytest.mark.asyncio
async def test_basic_epub_translation(epub_processor, test_epub_path):
    """Test basic EPUB translation functionality."""
    # 创建进度回调函数来验证进度更新
    progress_updates = []
    async def progress_callback(progress: TranslationProgress):
        progress_updates.append(progress)
    
    # 执行翻译
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh",
        progress_callback=progress_callback
    )
    
    # 验证进度更新
    assert len(progress_updates) > 0
    final_progress = progress_updates[-1]
    assert final_progress.completion_percentage == 100.0
    
    # 验证最终的 EPUB 文件
    assert os.path.exists(translated_path)
    translated_book = epub.read_epub(translated_path)
    
    # 验证元数据
    assert translated_book.get_metadata('DC', 'language')[0][0] == "zh"
    assert translated_book.get_metadata('DC', 'title')[0][0].startswith("[zh]")
    
    # 验证章节内容
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]
    assert len(html_items) > 0
    
    # HTML 标签集合
    html_tags = {
        'html', 'head', 'body', 'article', 'section', 'nav', 'aside',
        'header', 'footer', 'main', 'figure', 'figcaption', 'div', 'span',
        'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'a',
        'img', 'table', 'tr', 'td', 'th', 'thead', 'tbody', 'tfoot'
    }
    
    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
            
        content = item.get_content().decode('utf-8')
        soup = BeautifulSoup(content, 'html.parser')
        
        # 检查需要被翻译的内容
        for element in soup.find_all(text=True):
            text = element.strip()
            if not text or text.isspace():
                continue
                
            parent = element.parent.name if element.parent else None
            
            # 跳过不需要翻译的内容
            if (text.startswith('<?xml') or
                text.startswith('<!DOCTYPE') or
                'xml version' in text or
                (element.parent and element.parent.name == 'xml') or
                parent in {'html', 'head', 'body', 'article', 'section', 'nav', 'aside',
                          'header', 'footer', 'main', 'figure', 'figcaption'} or
                text.lower() in html_tags):  # 检查文本是否是 HTML 标签名
                assert not text.startswith("[zh]"), f"XML/DOCTYPE/structural text was translated: {text}"
                continue
            
            # 检查特殊标签
            if parent in {'code', 'script', 'style', 'sup'}:
                assert not text.startswith("[zh]"), f"Special tag content was translated: {text}"
            else:
                # 检查普通内容是否被翻译
                assert text.startswith("[zh]") or text in {"", "\n"}, f"Normal text not translated: {text}"

@pytest.mark.asyncio
async def test_special_tags_preservation(epub_processor, test_epub_path):
    """Test preservation of special tags during translation."""
    translated_path = await epub_processor.translate_epub(
        test_epub_path,
        source_language="en",
        target_language="zh"
    )

    assert os.path.exists(translated_path)
    translated_book = epub.read_epub(translated_path)
    
    html_items = [item for item in translated_book.get_items() if isinstance(item, epub.EpubHtml)]
    for item in html_items:
        if item.get_type() == ebooklib.ITEM_NAVIGATION:
            continue
            
        soup = BeautifulSoup(item.get_content(), 'html.parser')
        
        # Check special tags are preserved
        for tag in ['code', 'script', 'style', 'sup']:
            elements = soup.find_all(tag)
            for element in elements:
                text = element.get_text().strip()
                if text:
                    assert not text.startswith("[zh]"), f"Content in {tag} tag should not be translated"

@pytest.mark.asyncio
async def test_empty_chapter_handling(epub_processor):
    """Test handling of empty chapters."""
    # Create a progress object
    progress = TranslationProgress(
        total_chapters=2,
        completed_chapters=0,
        current_chapter=None,
        chapters_progress={},
        start_time=datetime.now()
    )
    
    # Add an empty chapter
    empty_chapter = ChapterProgress(
        chapter_id="empty_ch",
        title="Empty Chapter",
        total_fragments=0,
        completed_fragments=0,
        is_completed=True,
        start_time=datetime.now()
    )
    progress.chapters_progress["empty_ch"] = empty_chapter
    progress.completed_chapters += 1  # 增加已完成章节计数
    
    # Add a normal chapter
    normal_chapter = ChapterProgress(
        chapter_id="normal_ch",
        title="Normal Chapter",
        total_fragments=10,
        completed_fragments=5,
        is_completed=False,
        start_time=datetime.now()
    )
    progress.chapters_progress["normal_ch"] = normal_chapter
    
    # Verify empty chapter completion percentage
    assert empty_chapter.completion_percentage == 100.0
    
    # Verify normal chapter completion percentage
    assert normal_chapter.completion_percentage == 50.0
    
    # Verify overall completion percentage
    assert progress.completion_percentage == 50.0  # One out of two chapters completed

@pytest.mark.asyncio
async def test_empty_book_handling(epub_processor):
    """Test handling of empty books."""
    # Create a progress object with no chapters
    progress = TranslationProgress(
        total_chapters=0,
        completed_chapters=0,
        current_chapter=None,
        chapters_progress={},
        start_time=datetime.now()
    )
    
    # Verify completion percentage for empty book
    assert progress.completion_percentage == 100.0
