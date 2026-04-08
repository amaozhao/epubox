from unittest.mock import patch

import pytest

from engine.item.chunker import DomChunker, Block, count_tokens


class TestDomChunker:
    """测试 DomChunker 类"""

    def test_basic_chunk(self):
        """测试基本分块：短 HTML 应返回单个 chunk"""
        html = "<html><body><p>Hello World</p></body></html>"
        chunker = DomChunker(token_limit=100)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "<p>Hello World</p>" in chunks[0].original
        assert len(chunks[0].xpaths) == 1

    def test_greedy_merge(self):
        """测试贪心合并：多个小元素合并到 token_limit"""
        html = "<html><body><p>A</p><p>B</p><p>C</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        # token_limit 足够大，所有元素应合并为一个 chunk
        assert len(chunks) == 1
        assert len(chunks[0].xpaths) == 3

    def test_split_on_limit(self):
        """测试超限分割：总 token 超过 limit 时分割"""
        # 创建足够多的内容
        paragraphs = "".join(f"<p>Paragraph {i} with some longer text content here</p>" for i in range(20))
        html = f"<html><body>{paragraphs}</body></html>"
        chunker = DomChunker(token_limit=50)
        chunks = chunker.chunk(html)
        assert len(chunks) > 1
        # 每个 chunk 都应有 xpaths
        for chunk in chunks:
            assert len(chunk.xpaths) >= 1

    def test_skip_img(self):
        """测试跳过 img 标签"""
        html = "<html><body><p>Text</p><img src='test.png'/><p>More</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "img" not in chunks[0].original

    def test_skip_pure_placeholder(self):
        """测试跳过纯 PreCode 占位符"""
        html = "<html><body><p>Text</p>[PRE:0]<p>More</p></body></html>"
        # BeautifulSoup 会将 [PRE:0] 作为文本节点，_should_skip 返回 True
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        # [PRE:0] 是裸文本节点，应被跳过
        assert len(chunks) == 1

    def test_atomic_tags_not_split(self):
        """测试 ATOMIC_TAGS（table/ul/ol）不被拆分"""
        html = "<html><body><table><tr><td>Cell 1</td><td>Cell 2</td></tr><tr><td>Cell 3</td><td>Cell 4</td></tr></table></body></html>"
        chunker = DomChunker(token_limit=10)  # 很小的 limit
        chunks = chunker.chunk(html)
        # table 应该完整保留在一个 chunk 中
        assert len(chunks) == 1
        assert "<table>" in chunks[0].original

    def test_title_collection(self):
        """测试 <title> 被收集到 chunk 中"""
        html = "<html><head><title>My Book</title></head><body><p>Content</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "<title>My Book</title>" in chunks[0].original
        assert any("title" in xpath for xpath in chunks[0].xpaths)

    def test_nav_file(self):
        """测试导航文件分块"""
        html = '<ncx><navMap><navPoint id="ch1"><navLabel><text>Chapter 1</text></navLabel><content src="ch1.xhtml"/></navPoint></navMap></ncx>'
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)
        assert len(chunks) >= 1

    def test_empty_html(self):
        """测试空 HTML"""
        html = "<html><body></body></html>"
        chunker = DomChunker(token_limit=100)
        chunks = chunker.chunk(html)
        assert len(chunks) == 0

    def test_xpaths_correct(self):
        """测试 xpath 路径正确性"""
        html = "<html><body><h1>Title</h1><p>First</p><p>Second</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        xpaths = chunks[0].xpaths
        assert "/html/body/h1" in xpaths
        assert "/html/body/p[1]" in xpaths
        assert "/html/body/p[2]" in xpaths

    def test_recursive_oversized(self):
        """测试超限元素递归到子元素"""
        html = "<html><body><div><p>Short 1</p><p>Short 2</p></div></body></html>"
        chunker = DomChunker(token_limit=10)  # 很小，div 超限
        chunks = chunker.chunk(html)
        # div 超限但非 ATOMIC，应递归到 p 级别
        assert len(chunks) >= 1

    def test_skip_no_text_content(self):
        """测试跳过无文本内容的元素"""
        html = "<html><body><div></div><p>Text</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "<p>Text</p>" in chunks[0].original

    def test_empty_title(self):
        """测试空 title 不被收集"""
        html = "<html><head><title></title></head><body><p>Text</p></body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "<title>" not in chunks[0].original

    def test_count_tokens_fallback(self):
        """测试 tiktoken 模型未找到时的 fallback（覆盖 lines 16-17）"""
        with patch("engine.item.chunker.tiktoken.encoding_for_model", side_effect=KeyError):
            tokens = count_tokens("hello world")
            assert tokens > 0

    def test_whitespace_only_children_skipped(self):
        """测试空白文本节点被跳过（覆盖 line 107）"""
        # 元素之间有换行和空格的 HTML
        html = "<html><body>\n  <p>Text</p>\n  </body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "Text" in chunks[0].original
