from engine.item.chunker import count_tokens, chunk_html, add_context_to_chunks, ChunkState


class TestCountTokens:
    """测试 count_tokens 函数"""

    def test_count_tokens_basic(self):
        assert count_tokens("Hello World") > 0
        assert count_tokens("") == 0

    def test_count_tokens_chinese(self):
        assert count_tokens("你好世界") > 0


class TestChunkHtml:
    """测试 chunk_html 函数"""

    def test_empty_html(self):
        assert chunk_html("") == []
        assert chunk_html("   ") == []

    def test_single_element(self):
        html = "<p>Hello World</p>"
        chunks = chunk_html(html, token_limit=100)
        assert len(chunks) == 1
        assert chunks[0].original == html

    def test_multiple_elements(self):
        html = "<p>First</p><p>Second</p><p>Third</p>"
        chunks = chunk_html(html, token_limit=1000)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.tokens > 0

    def test_chunks_produce_results(self):
        """Phase 2 chunker produces non-empty chunk list."""
        # Use bare paragraphs (not wrapped in div) so each p is a separate element child
        html = ''.join(f'<p>Para {i}. Text.</p>' for i in range(30))
        chunks = chunk_html(html, token_limit=100)  # 30 paragraphs, ~6 tokens each, limit 100 -> 5+ chunks
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.tokens > 0

    def test_xpath_is_set(self):
        html = "<div><p>Hello</p></div>"
        chunks = chunk_html(html, token_limit=1000)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.xpath is not None
            assert chunk.xpath.startswith("/")


class TestAddContextToChunks:
    """测试 add_context_to_chunks 函数 (现在只是透传)"""

    def test_empty_list(self):
        assert add_context_to_chunks([]) == []

    def test_returns_same_chunks(self):
        """add_context_to_chunks 现在是透传，直接返回相同的 chunks"""
        chunks = [
            ChunkState(xpath="/div[1]", original="<p>Hello</p>", tokens=5)
        ]
        result = add_context_to_chunks(chunks)
        assert result == chunks
