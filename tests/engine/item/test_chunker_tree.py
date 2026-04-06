from engine.item.chunker import chunk_html, chunk_tree, add_context_to_chunks
from engine.item.tree import parse_html
from engine.item.token import MAX_TOKEN_LIMIT


class TestChunkHtml:
    """Tests for chunk_html (Phase 2.1 convenience wrapper)."""

    def test_empty_html_returns_empty_list(self):
        """Empty HTML string returns []."""
        assert chunk_html("") == []
        assert chunk_html("   ") == []
        assert chunk_html("\n\t  ") == []

    def test_simple_html_under_limit_returns_single_chunk(self):
        """HTML whose content fits in MAX_TOKEN_LIMIT produces exactly one chunk."""
        html = "<p>Hello world, this is a simple paragraph.</p>"
        chunks = chunk_html(html)
        assert len(chunks) == 1
        assert chunks[0].xpath == "/div/p[1]"
        assert chunks[0].tokens > 0
        assert "Hello" in chunks[0].original

    def test_html_over_limit_splits_into_multiple_chunks(self):
        """HTML exceeding MAX_TOKEN_LIMIT splits into multiple chunks when candidates can merge."""
        paragraphs = [f"<p>Para {i}.</p>" for i in range(200)]
        html = "".join(paragraphs)
        chunks = chunk_html(html)
        assert len(chunks) > 1, "Expected multiple chunks for oversized HTML"

    def test_chunk_xpath_uses_parser_wrapper_prefix(self):
        """parse_html wraps in <div>, so xpaths start with /div."""
        html = "<p>First</p><p>Second</p>"
        chunks = chunk_html(html)
        assert len(chunks) == 1
        assert chunks[0].xpath == "/div/p[1]"

    def test_chunk_tokens_are_accurate(self):
        """tokens field reflects the token count of original."""
        html = "<p>Hello world.</p>"
        chunks = chunk_html(html)
        assert chunks[0].tokens > 0


class TestChunkTree:
    """Tests for chunk_tree (core recursive traversal + greedy merge algorithm)."""

    def test_single_element_node_returns_one_chunk(self):
        """A tree with one element node yields exactly one chunk."""
        root = parse_html("<p>Single paragraph.</p>")
        chunks = chunk_tree(root, token_limit=1000)
        assert len(chunks) == 1
        assert chunks[0].xpath == "/div/p[1]"

    def test_multiple_bare_elements_merged_by_default(self):
        """Bare sibling elements are merged into one chunk under generous limit."""
        root = parse_html("<p>First</p><p>Second</p><p>Third</p>")
        chunks = chunk_tree(root, token_limit=1000)
        assert len(chunks) == 1
        assert chunks[0].xpath == "/div/p[1]"
        assert "First" in chunks[0].original
        assert "Second" in chunks[0].original
        assert "Third" in chunks[0].original

    def test_multiple_bare_elements_split_under_tight_limit(self):
        """Bare siblings are individually chunked when token_limit forces a flush."""
        root = parse_html("".join(f"<p>{chr(65+i)}</p>" for i in range(4)))
        chunks = chunk_tree(root, token_limit=3)

        assert len(chunks) == 4
        for i, chunk in enumerate(chunks):
            assert chunk.xpath == f"/div/p[{i+1}]", f"Chunk {i} xpath: {chunk.xpath}"

    def test_deeply_nested_xpath_reflects_nesting(self):
        """Deeply nested elements produce correctly nested xpaths."""
        root = parse_html(
            "<html><body><div><section><article><p>Deeply nested text.</p></article></section></div></body></html>"
        )
        chunks = chunk_tree(root, token_limit=1000)

        assert len(chunks) == 1
        xp = chunks[0].xpath
        assert "/div/" in xp

    def test_tree_with_no_element_children_returns_empty(self):
        """A root with no element children yields empty list."""
        root = parse_html("just plain text with no tags")
        assert root.children[0].is_text_node()
        chunks = chunk_tree(root, token_limit=1000)
        assert chunks == []

    def test_oversized_single_node_is_still_chunked(self):
        """An oversized single element is force-included as one chunk (not skipped)."""
        big_text = "<p>" + ("word " * 600) + "</p>"
        root = parse_html(big_text)
        chunks = chunk_tree(root, token_limit=50)
        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.tokens > 0

    def test_token_limit_forces_split(self):
        """Bare siblings accumulate until the limit is hit, then flush."""
        root = parse_html("".join(f"<p>Para {i}.</p>" for i in range(200)))
        chunks = chunk_tree(root, token_limit=10)
        assert len(chunks) > 1, "Expected multiple chunks under tight token_limit"
        for chunk in chunks:
            assert chunk.xpath.startswith("/div/p["), f"Unexpected xpath: {chunk.xpath}"


class TestAddContextToChunks:
    """Tests for add_context_to_chunks (passthrough in new architecture)."""

    def test_empty_list_returns_empty_list(self):
        """Empty input returns empty list."""
        assert add_context_to_chunks([]) == []

    def test_returns_same_chunks(self):
        """add_context_to_chunks returns chunks unchanged (now a passthrough)."""
        html = "<p>Hello world.</p>"
        chunks = chunk_html(html)
        result = add_context_to_chunks(chunks)
        assert result == chunks


class TestChunkTreeAndHtmlIntegration:
    """Integration tests for chunk_tree and chunk_html."""

    def test_xpath_uniqueness_within_result(self):
        """All chunks in a result have unique xpaths (no duplicate paths)."""
        root = parse_html("".join(f"<p>Para {i}.</p>" for i in range(100)))
        chunks = chunk_tree(root, token_limit=5)

        xpaths = [c.xpath for c in chunks]
        assert len(xpaths) == len(set(xpaths)), "Duplicate xpaths found in chunks"

    def test_all_chunks_have_populated_fields(self):
        """Every chunk has xpath, tokens, and original populated."""
        html = "".join(f"<p>Para {i} text.</p>" for i in range(20))

        for limit in [5, 20, 50, 200]:
            chunks = chunk_html(html, token_limit=limit)
            for chunk in chunks:
                assert chunk.xpath != ""
                assert chunk.tokens > 0
                assert chunk.original != ""

    def test_html_with_span_elements_single_chunk(self):
        """HTML containing only non-block elements fits in one chunk under generous limit."""
        html = "".join(f"<span>inline {i}</span>" for i in range(10))
        root = parse_html(html)
        chunks = chunk_tree(root, token_limit=MAX_TOKEN_LIMIT)
        assert len(chunks) == 1

    def test_deeply_nested_structure_xpath_prefix(self):
        """Deeply nested structure xpath reflects the nesting path."""
        root = parse_html(
            "<html><body>"
            "<div id='outer'>"
            "<div class='inner'>"
            "<p>Level 1</p>"
            "<p>Level 2</p>"
            "</div>"
            "</div>"
            "</body></html>"
        )
        chunks = chunk_tree(root, token_limit=MAX_TOKEN_LIMIT)

        assert len(chunks) >= 1
        xp = chunks[0].xpath
        assert "/div/" in xp

    def test_mixed_content_xpath(self):
        """Mixed block/inline content produces an xpath under the parser-created wrapper."""
        html = "<article><h2>Heading</h2><p>Para one.</p><p>Para two.</p></article>"
        chunks = chunk_html(html, token_limit=MAX_TOKEN_LIMIT)
        assert len(chunks) == 1
        assert chunks[0].xpath == "/div/article[1]"

    def test_consecutive_chunks_preserve_document_order(self):
        """Chunks are returned in document order (left-to-right, top-to-bottom)."""
        root = parse_html("".join(f"<p>P{i}</p>" for i in range(50)))
        chunks = chunk_tree(root, token_limit=5)

        xpath_indices = []
        for chunk in chunks:
            idx = int(chunk.xpath.split("/p[")[1][:-1])
            xpath_indices.append(idx)

        assert xpath_indices == list(range(1, len(chunks) + 1))
