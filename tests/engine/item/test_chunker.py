from engine.item.chunker import count_tokens, chunk_html, add_context_to_chunks, ChunkState


def get_content_chunks(chunks):
    """Filter out prefix/suffix chunks to get only content chunks."""
    return [c for c in chunks if c.xpath not in ("prefix", "suffix")]


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
        """单个元素测试（现在返回 prefix + content + suffix）"""
        html = "<p>Hello World</p>"
        chunks = chunk_html(html, token_limit=100)
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) == 1
        assert chunks[0].original == html or content_chunks[0].original == html

    def test_multiple_elements(self):
        html = "<p>First</p><p>Second</p><p>Third</p>"
        chunks = chunk_html(html, token_limit=1000)
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) >= 1
        for chunk in chunks:
            assert chunk.tokens > 0

    def test_chunks_produce_results(self):
        """Phase 2 chunker produces non-empty chunk list."""
        # Use bare paragraphs (not wrapped in div) so each p is a separate element child
        html = ''.join(f'<p>Para {i}. Text.</p>' for i in range(30))
        chunks = chunk_html(html, token_limit=100)  # 30 paragraphs, ~6 tokens each, limit 100 -> 5+ chunks
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) > 1
        for chunk in chunks:
            assert chunk.tokens > 0

    def test_xpath_is_set(self):
        html = "<div><p>Hello</p></div>"
        chunks = chunk_html(html, token_limit=1000)
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) >= 1
        for chunk in content_chunks:
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


class TestContainerTagNotSplit:
    """Phase 5: 测试容器标签不拆分"""

    def test_nav_container_not_split(self):
        """nav 标签作为容器不拆分"""
        from engine.item.chunker import chunk_html, CONTAINER_TAGS
        assert "nav" in CONTAINER_TAGS
        html = "<nav><ol><li>Item 1</li><li>Item 2</li></ol></nav>"
        chunks = chunk_html(html, token_limit=10)
        # nav 应该作为完整容器，整个 nav 作为一个或多个 chunk
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) >= 1
        # 每个 content chunk 的 xpath 应该包含 nav
        for chunk in content_chunks:
            assert "/nav" in chunk.xpath

    def test_ol_li_container_not_split_leaf(self):
        """ol/li 标签：ol 是容器，li 是叶子但累积"""
        from engine.item.chunker import chunk_html, _is_container_tag, _is_leaf_tag
        assert _is_container_tag("ol") is True
        assert _is_leaf_tag("li") is True
        html = "<ol><li>Item 1</li><li>Item 2</li><li>Item 3</li></ol>"
        chunks = chunk_html(html, token_limit=1000)
        content_chunks = get_content_chunks(chunks)
        # 整个 ol 作为一个 content chunk
        assert len(content_chunks) == 1
        assert "<ol>" in content_chunks[0].original
        assert "</ol>" in content_chunks[0].original

    def test_div_container_under_limit_kept_intact(self):
        """div 作为容器在限制内时保持完整"""
        from engine.item.chunker import chunk_html, _is_container_tag
        assert _is_container_tag("div") is True
        html = "<div><p>Para 1</p><p>Para 2</p></div>"
        # Use high limit so div fits intact
        chunks = chunk_html(html, token_limit=1000)
        content_chunks = get_content_chunks(chunks)
        # div 作为容器，在限制内时应该整个保留
        assert len(content_chunks) == 1
        assert "<div>" in content_chunks[0].original

    def test_div_container_over_limit_splits(self):
        """div 作为容器超限时递归拆分"""
        from engine.item.chunker import chunk_html
        html = "<div><p>Para 1</p><p>Para 2</p></div>"
        # Very low limit forces splitting
        chunks = chunk_html(html, token_limit=5)
        content_chunks = get_content_chunks(chunks)
        # div exceeds limit, so it should be recursively split
        assert len(content_chunks) > 1


class TestLeafTagGreedyAccumulation:
    """Phase 5: 测试叶子标签贪心累积"""

    def test_li_greedy_accumulates(self):
        """li 标签贪心累积到 token_limit"""
        from engine.item.chunker import chunk_html, _is_leaf_tag
        assert _is_leaf_tag("li") is True
        html = "<li>Item 1</li>" * 10
        chunks = chunk_html(html, token_limit=500)
        content_chunks = get_content_chunks(chunks)
        # 多个 li 累积成较少的 chunks
        assert len(content_chunks) < 10

    def test_p_greedy_accumulates(self):
        """p 标签贪心累积"""
        from engine.item.chunker import chunk_html, _is_leaf_tag
        assert _is_leaf_tag("p") is True
        html = "<p>Short paragraph.</p>" * 20
        chunks = chunk_html(html, token_limit=200)
        content_chunks = get_content_chunks(chunks)
        # 20 个 p 共 140 tokens under 200 limit, so they fit in 1 chunk
        assert len(content_chunks) == 1
        for chunk in content_chunks:
            assert chunk.tokens > 0

    def test_span_greedy_accumulates(self):
        """span 标签贪心累积"""
        from engine.item.chunker import chunk_html, _is_leaf_tag
        assert _is_leaf_tag("span") is True
        html = "<span>text</span>" * 50
        chunks = chunk_html(html, token_limit=100)
        content_chunks = get_content_chunks(chunks)
        assert len(content_chunks) > 1


class TestNcxTagHandling:
    """Phase 5: 测试 NCX 文件标签处理"""

    def test_ncx_is_container(self):
        """ncx 标签是容器"""
        from engine.item.chunker import _is_container_tag, CONTAINER_TAGS
        assert "ncx" in CONTAINER_TAGS
        assert _is_container_tag("ncx") is True

    def test_navmap_is_container(self):
        """navMap 标签是容器"""
        from engine.item.chunker import _is_container_tag, CONTAINER_TAGS
        assert "navmap" in CONTAINER_TAGS
        assert _is_container_tag("navMap") is True

    def test_navpoint_is_container(self):
        """navPoint 标签是容器"""
        from engine.item.chunker import _is_container_tag, CONTAINER_TAGS
        assert "navpoint" in CONTAINER_TAGS
        assert _is_container_tag("navPoint") is True

    def test_ncx_chunking_preserves_structure(self):
        """NCX 标签结构被正确保留"""
        from engine.item.chunker import chunk_html
        html = """<?xml version="1.0"?>
<ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"""
        chunks = chunk_html(html, token_limit=1000)
        content_chunks = get_content_chunks(chunks)
        # 整个 NCX 结构应该作为一个 content chunk
        assert len(content_chunks) == 1
        assert "<ncx>" in content_chunks[0].original
        assert "</ncx>" in content_chunks[0].original

    def test_ncx_chunking_with_large_navmap(self):
        """大型 navMap 被正确分块"""
        from engine.item.chunker import chunk_html
        nav_points = "".join(
            f"<navPoint><navLabel><text>Chapter {i}</text></navLabel></navPoint>"
            for i in range(20)
        )
        html = f"<?xml version=\"1.0\"?><ncx><navMap>{nav_points}</navMap></ncx>"
        chunks = chunk_html(html, token_limit=500)
        content_chunks = get_content_chunks(chunks)
        # navMap 容器不拆分，整个 navMap 作为一个 content chunk
        assert len(content_chunks) >= 1
        # 验证 navMap 标签完整
        for chunk in content_chunks:
            if "<navMap>" in chunk.original:
                assert "</navMap>" in chunk.original
