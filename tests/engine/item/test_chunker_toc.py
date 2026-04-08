"""Tests for toc.xhtml chunking - uses actual EPUB content"""
import os
import pytest

from engine.item.tree import parse_html
from engine.item.chunker import chunk_html


FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "fixtures", "toc_xhtml_original.txt")


class TestTocXhtmlChunking:
    """Test chunking of actual toc.xhtml content"""

    @pytest.fixture
    def toc_content(self):
        """Load actual toc.xhtml content from fixture"""
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_toc_chunking_preserves_nav_structure(self, toc_content):
        """验证 toc.xhtml nav 元素被正确 chunk"""
        root = parse_html(toc_content)

        # 找到 body
        body = None
        for child in root.children:
            if child.tag == "html":
                for grandchild in child.children:
                    if grandchild.tag == "body":
                        body = grandchild
                        break

        assert body is not None

        # 找到所有 nav 元素
        navs = [c for c in body.children if c.is_element_node() and c.tag == "nav"]
        assert len(navs) == 3, f"Expected 3 navs, got {len(navs)}"

        # nav[2] 是 page-list nav
        page_list_nav = navs[1]
        assert page_list_nav.attributes.get("epub:type") == "page-list"

        # 测试 chunking - 验证能产生多个 chunk
        chunks = chunk_html(page_list_nav.to_html(), token_limit=500)
        assert len(chunks) >= 2, f"Expected multiple chunks from large nav, got {len(chunks)}"

        # 验证所有 chunk 的 token 数都在限制内
        for chunk in chunks:
            assert chunk.tokens <= 500, f"Chunk {chunk.xpath} has {chunk.tokens} tokens (over 500)"

    def test_toc_chunking_handles_page_list(self, toc_content):
        """验证 page-list nav 的 chunking"""
        root = parse_html(toc_content)

        body = None
        for child in root.children:
            if child.tag == "html":
                for grandchild in child.children:
                    if grandchild.tag == "body":
                        body = grandchild
                        break

        navs = [c for c in body.children if c.is_element_node() and c.tag == "nav"]
        page_list_nav = navs[1]

        # page-list nav 包含 68 个页面链接
        chunks = chunk_html(page_list_nav.to_html(), token_limit=500)

        # 合并所有 chunks
        merged_content = "".join(c.original for c in chunks)

        # 验证所有 68 个页面链接都存在
        for i in range(1, 69):
            assert f'href="#page{i}"' in merged_content or f'href="B38396_Nano-Book_Ver2.xhtml#page{i}"' in merged_content, f"Missing page {i}"