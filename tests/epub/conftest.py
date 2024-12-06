"""
EPUB testing fixtures.
"""

from pathlib import Path

import pytest
from ebooklib import epub


@pytest.fixture
def create_test_epub(tmp_path):
    """创建测试用的EPUB文件."""

    def _create_epub(content: str = "<p>Test content</p>") -> Path:
        # 创建一个新的EPUB文件
        book = epub.EpubBook()

        # 设置元数据
        book.set_identifier("id123456")
        book.set_title("Test Book")
        book.set_language("en")

        # 添加一个章节
        c1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
        c1.content = content
        book.add_item(c1)

        # 添加导航
        nav = epub.EpubNav()
        ncx = epub.EpubNcx()
        book.add_item(nav)
        book.add_item(ncx)

        # 定义目录结构 - 使用简单的 Link 列表
        book.toc = [epub.Link("chap_01.xhtml", "Chapter 1", "chapter1")]

        # 定义线性阅读顺序
        book.spine = ["nav", "chap_01.xhtml"]

        # 生成EPUB文件
        epub_path = tmp_path / "test.epub"
        epub.write_epub(str(epub_path), book)

        return epub_path

    return _create_epub

    return _create_epub
