import re

import pytest

from engine.item.chunker import Chunker
from engine.schemas.translator import TranslationStatus


class TestChunker:
    """
    测试 Chunker 类的核心功能和边界情况。
    """

    @pytest.fixture
    def chunker(self):
        """为每个测试提供一个 Chunker 实例。"""
        return Chunker(limit=30)

    # --- 1. 初始化和参数验证测试 ---

    def test_init_validation(self):
        """测试 Chunker 实例化时的参数验证。"""
        with pytest.raises(ValueError, match="limit must be a positive integer token count"):
            Chunker(limit=0)
        with pytest.raises(ValueError, match="limit must be a positive integer token count"):
            Chunker(limit=-100)
        with pytest.raises(ValueError, match="limit must be a positive integer token count"):
            Chunker(limit="abc")  # type: ignore

    def test_short_html(self, chunker):
        """测试 HTML 内容短于限制的情况，应返回单个 Chunk。"""
        html = "<h1>Hello World!</h1><p>This is a short paragraph.</p>"
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert chunks[0].original == html
        assert chunks[0].tokens <= chunker.limit
        assert chunks[0].name == "1"
        assert chunks[0].status == TranslationStatus.PENDING

    def test_long_html(self, chunker):
        """测试 HTML 内容长于限制的情况，应被分割为多个 Chunk。"""
        long_text = "This is a very long paragraph designed to exceed the token limit. " * 5
        html = f"<p>{long_text}</p>"
        chunks = chunker.chunk(html)

        assert len(chunks) > 1
        reconstructed_html = "".join([chunk.original for chunk in chunks])
        assert reconstructed_html == html

        for chunk in chunks:
            assert chunk.tokens <= chunker.limit

    # --- 3. 分割点逻辑测试 ---

    def test_split_at_tag_end(self):
        """测试分割点优先在闭合标签末尾。"""
        html_part1 = "<div><p>This is a short part that fits the limit."
        html_part2 = "</p><span>Another part.</span></div>"

        temp_chunker = Chunker(limit=Chunker().get_token_count(html_part1 + "</p>"))
        html = html_part1 + html_part2
        chunks = temp_chunker.chunk(html)

        assert len(chunks) == 2
        assert chunks[0].original == html_part1 + "</p>"
        assert chunks[1].original == "<span>Another part.</span></div>"
        # 验证 name 格式
        assert chunks[0].name == "1"
        assert chunks[1].name == "2"

    def test_split_without_tag_end(self, chunker):
        """测试在 token 限制内没有闭合标签时，按字符分割。"""
        long_text = "This_text_must_be_split_without_a_tag"

        small_chunker = Chunker(limit=2)
        chunks = small_chunker.chunk(long_text)

        assert len(chunks) > 1
        assert chunks[0].original.strip() != ""
        assert chunks[0].tokens <= small_chunker.limit

        reconstructed = "".join([c.original for c in chunks])
        assert reconstructed == long_text
        # 验证 name 格式
        for i, chunk in enumerate(chunks):
            assert chunk.name == str(i + 1)

    # --- 4. 边界条件测试 ---

    def test_empty_or_whitespace_html(self, chunker):
        """测试输入为空或只包含空格的 HTML。"""
        assert chunker.chunk("") == []
        assert chunker.chunk("   \n   \t") == []

    def test_single_long_word(self):
        """测试单个超长单词，应被强制分割。"""
        long_word = "a" * 100
        html = f"<p>{long_word}</p>"

        chunker = Chunker(limit=10)
        chunks = chunker.chunk(html)

        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.tokens <= chunker.limit
            # 验证 name 格式
            assert re.match(r"^\d+$", chunk.name)  # 确保 name 是纯数字

    def test_multiple_small_tags(self, chunker):
        """测试多个小标签被正确地合并到大块中。"""
        html = "<span>a</span><span>b</span><span>c</span><span>d</span><span>e</span>"

        large_chunker = Chunker(limit=chunker.get_token_count(html))
        chunks = large_chunker.chunk(html)

        assert len(chunks) == 1
        assert chunks[0].original == html
        assert chunks[0].name == "1"

    # --- 5. Chunk 对象数据完整性测试 ---

    def test_chunk_data_integrity(self):
        """测试生成的 Chunk 对象的属性是否正确。"""
        html = "<div><p>Hello</p><p>World</p></div>"

        chunker = Chunker(limit=10)
        chunks = chunker.chunk(html)

        assert len(chunks) == 2

        assert chunks[0].name == "1"
        assert chunks[0].original == "<div><p>Hello</p>"
        assert chunks[0].translated is None
        assert chunks[0].tokens > 0

        assert chunks[1].name == "2"
        assert chunks[1].original == "<p>World</p></div>"
        assert chunks[1].translated is None
        assert chunks[1].tokens > 0

    def test_reconstruct_html(self, chunker):
        """测试所有 chunk 的内容可以重新拼接回原始 HTML。"""
        html = """
        <html>
          <body>
            <h1>This is a heading.</h1>
            <p>This is a paragraph with some content.</p>
            <div>
              <span>This span is long enough to be a separate chunk.</span>
              <p>This is a nested paragraph.</p>
            </div>
            <ul><li>item 1</li><li>item 2</li></ul>
          </body>
        </html>
        """
        chunks = Chunker(limit=15).chunk(html)
        reconstructed_html = "".join([chunk.original for chunk in chunks])

        assert re.sub(r"\s+", "", reconstructed_html) == re.sub(r"\s+", "", html)
