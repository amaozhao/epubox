"""
Test cases for the EPUB processor module.
"""

from pathlib import Path

import ebooklib
import ebooklib.epub as epub
import pytest

from app.core.config import settings
from app.epub.processor import EpubProcessor


@pytest.fixture
def processor(create_test_epub, temp_work_dir):
    """创建EpubProcessor实例."""
    epub_path = create_test_epub("<p>Original content</p>")
    return EpubProcessor(str(epub_path), str(temp_work_dir), "mistral")


class TestEpubProcessor:
    """Test cases for EpubProcessor class."""

    async def test_prepare_success(self, processor):
        """测试prepare方法 - 成功场景."""
        await processor.prepare()
        assert processor.work_file.exists()
        assert processor.book is not None

    async def test_extract_html_success(self, processor):
        """测试extract_html方法 - 成功场景."""
        await processor.prepare()
        contents = processor.extract_html()
        assert len(contents) > 0
        assert any(
            "<p>Original content</p>" in content for content in contents.values()
        )

    async def test_extract_ncx_success(self, processor):
        """测试extract_ncx方法 - 成功场景."""
        await processor.prepare()
        contents = processor.extract_ncx()
        assert len(contents) > 0

    async def test_process_success(self, processor):
        """测试process方法 - 成功场景."""
        await processor.process()
        assert processor.work_file.exists()
        assert processor.book is not None
        assert len(processor.html_contents) > 0

    async def test_process_html_success(self, processor):
        """测试HTML内容处理 - 成功场景."""
        await processor.prepare()
        processor.html_contents = processor.extract_html()
        # 处理单个HTML内容
        for name, content in processor.html_contents.items():
            translated_content = await processor.html_processor.process(content)
            await processor.update_content(name, translated_content)
        assert len(processor.html_contents) > 0
        assert all(len(content) > 0 for content in processor.html_contents.values())

    async def test_process_ncx_success(self, processor):
        """测试NCX内容处理 - 成功场景."""
        await processor.prepare()
        processor.ncxs = processor.extract_ncx()
        # 处理单个NCX内容
        for name, content in processor.ncxs.items():
            translated_content = await processor.html_processor.process(
                content, parser="lxml"
            )
            await processor.update_content(
                name, translated_content, item_type=ebooklib.ITEM_NAVIGATION
            )
        assert len(processor.ncxs) > 0
        assert all(len(content) > 0 for content in processor.ncxs.values())
