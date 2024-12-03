"""Test EPUB processor."""

import os
import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib.epub")
import shutil
from pathlib import Path

import ebooklib
import pytest
from bs4 import BeautifulSoup
from ebooklib import epub

from src.services.processors.epub import EPUBProcessor, EPUBProcessorError
from src.services.processors.html import HTMLProcessor
from src.services.translation.translation_service import TranslationService


class SimpleTranslationService(TranslationService):
    """简单的翻译服务实现，用于测试"""

    def get_token_limit(self) -> int:
        return 4096

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """简单的翻译实现，仅用于测试HTML结构保持"""
        return text


@pytest.fixture
def translation_service():
    """Create translation service for testing."""
    return SimpleTranslationService()


@pytest.fixture
def test_data_dir():
    """测试数据目录"""
    return os.path.dirname(os.path.dirname(__file__))  # 返回 tests 目录


@pytest.fixture
def test_output_dir(test_data_dir):
    """测试输出目录"""
    output_dir = os.path.join(test_data_dir, "output")
    os.makedirs(output_dir, exist_ok=True)
    yield output_dir
    shutil.rmtree(output_dir, ignore_errors=True)


@pytest.fixture
def html_processor(translation_service):
    """Create HTML processor for testing."""
    return HTMLProcessor(translation_service=translation_service)


@pytest.fixture
def epub_processor(test_data_dir, html_processor):
    """Create EPUB processor for testing."""
    return EPUBProcessor(html_processor=html_processor, project_root=test_data_dir)


@pytest.fixture
def sample_epub(test_output_dir):
    """Create a sample EPUB file."""
    epub_path = os.path.join(test_output_dir, "test.epub")

    # Create a basic EPUB file
    book = epub.EpubBook()

    # Set metadata
    book.set_identifier("id123")
    book.set_title("Test Book")
    book.set_language("en")

    # Add a chapter
    c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    c1.content = """
        <html>
            <head></head>
            <body>
                <h1>Chapter 1</h1>
                <p>This is a test paragraph.</p>
            </body>
        </html>
    """
    book.add_item(c1)

    # Add navigation files
    nav = epub.EpubNav()
    book.add_item(nav)
    book.add_item(epub.EpubNcx())

    # Create spine
    book.spine = ["nav", c1]

    # Create TOC
    book.toc = [(epub.Section("Chapter 1"), [c1])]

    # Write the EPUB file
    epub.write_epub(epub_path, book, {})
    return epub_path


class TestEPUBProcessor:
    """Test EPUB processor."""

    async def test_translate_epub(self, epub_processor, sample_epub, test_output_dir):
        """测试完整的EPUB翻译流程"""
        # 1. 设置输出路径
        output_path = os.path.join(test_output_dir, "translated.epub")

        # 2. 翻译EPUB文件
        result_path = await epub_processor.translate_epub(
            epub_path=sample_epub,
            source_lang="en",
            target_lang="zh",
            output_path=output_path,
        )

        # 3. 验证结果
        assert result_path == output_path
        assert os.path.exists(result_path)

        # 4. 验证翻译内容的HTML结构
        book = epub.read_epub(result_path)
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
        assert len(items) > 0

        # 解析原始HTML和翻译后的HTML
        original_soup = BeautifulSoup(items[0].content.decode("utf-8"), "html.parser")
        translated_soup = BeautifulSoup(items[0].content.decode("utf-8"), "html.parser")

        # 验证HTML结构
        # 1. 检查标签数量是否一致
        assert len(original_soup.find_all()) == len(translated_soup.find_all())

        # 2. 检查标签层次结构是否一致
        original_tags = [tag.name for tag in original_soup.find_all()]
        translated_tags = [tag.name for tag in translated_soup.find_all()]
        assert original_tags == translated_tags

        # 3. 检查特定标签是否存在且位置正确
        assert translated_soup.find("h1") is not None
        assert translated_soup.find("p") is not None
        assert translated_soup.find("h1").find_next_sibling().name == "p"
