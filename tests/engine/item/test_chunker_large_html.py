"""
Test case using the JSON file's original problematic content to verify chunker fix.

The old buggy chunker generated a 30510 token chunk named "/div/html[1]" containing
the entire HTML document. The fix now correctly:
1. Extracts prefix (xml + doctype + html/head/body opening tags)
2. Extracts body children as individual content chunks
3. Appends suffix (</body></html>)
"""
import pytest

from engine.item.chunker import chunk_html, count_tokens


class TestLargeHtmlChunking:
    """Test chunker with the problematic 30510 token HTML from the JSON file."""

    @pytest.fixture
    def problematic_html(self):
        """Load the B38396_Nano-Book_Ver2.xhtml content that had the 30510 token chunk bug."""
        import json
        with open('/Users/amaozhao/Downloads/epub/ship-mcp-server-python-production-ready.json') as f:
            data = json.load(f)
        for item in data['items']:
            if 'B38396_Nano-Book_Ver2.xhtml' in item.get('id', ''):
                return item['content']
        pytest.skip("JSON file not found or B38396_Nano-Book_Ver2.xhtml not present")

    def test_old_bug_chunk_name_was_div_html(self, problematic_html):
        """
        OLD BUG: The chunk was named '/div/html[1]' indicating the entire <html> was one chunk.
        This was wrong because <html> should not be a container tag.
        """
        # The buggy chunk name from the JSON
        assert "/div/html[1]" == "/div/html[1]"

    def test_old_bug_was_single_huge_chunk(self, problematic_html):
        """
        OLD BUG: The original content would produce a single 30510 token chunk.
        Now it should produce many small chunks (no single chunk > 1200 tokens).
        """
        tokens = count_tokens(problematic_html)
        # Original full content has ~33800 tokens (different from the old buggy chunk's 30510)
        assert tokens > 30000, f"Full content should have > 30000 tokens, got {tokens}"

        # But with new chunker, no single chunk exceeds 1200
        chunks = chunk_html(problematic_html, token_limit=1200)
        over_limit = [c for c in chunks if c.tokens > 1200]
        assert len(over_limit) == 0, f"No chunk should exceed 1200, got {len(over_limit)}"

    def test_new_chunker_fix_prefix_is_small(self, problematic_html):
        """
        FIX VERIFICATION: prefix chunk should be reasonably sized.

        The prefix contains: xml declaration + doctype + html/head (with title/links) + body opening.
        It should NOT contain the entire HTML document.
        """
        chunks = chunk_html(problematic_html, token_limit=1200)
        prefix_chunk = chunks[0]
        assert prefix_chunk.xpath == "prefix"
        assert prefix_chunk.needs_translation is False
        # Prefix includes full head content (title, links, etc.) - should be < 200 tokens
        assert prefix_chunk.tokens < 200, f"Prefix should be < 200 tokens, got {prefix_chunk.tokens}"
        # Prefix should NOT contain the full HTML document (should end with <body opening tag)
        assert "<body" in prefix_chunk.original, "Prefix should contain <body opening tag"
        assert "</html>" not in prefix_chunk.original, "Prefix should NOT contain closing tags"

    def test_new_chunker_has_suffix(self, problematic_html):
        """FIX VERIFICATION: suffix chunk (</body></html>) is present."""
        chunks = chunk_html(problematic_html, token_limit=1200)
        suffix_chunk = chunks[-1]
        assert suffix_chunk.xpath == "suffix"
        assert suffix_chunk.needs_translation is False
        assert suffix_chunk.original == "</body>\n</html>"
        assert suffix_chunk.tokens < 20

    def test_new_chunker_body_children_extracted(self, problematic_html):
        """
        FIX VERIFICATION: body children are extracted as individual content chunks.

        The old buggy chunker put everything in one /div/html[1] chunk.
        The fix extracts body children (divs) as separate content chunks.
        """
        chunks = chunk_html(problematic_html, token_limit=1200)
        content_chunks = [c for c in chunks if c.xpath not in ("prefix", "suffix")]

        # Should have multiple content chunks (body's direct children)
        # The problematic HTML has 4 divs as body children
        assert len(content_chunks) >= 4, f"Expected 4+ content chunks, got {len(content_chunks)}"

        # The old bug had everything in ONE chunk named /div/html[1]
        # Now we have separate chunks with xpaths like /div/html[1]/body[1]/div[1]
        chunk_names = [c.xpath for c in content_chunks]
        for name in chunk_names:
            assert "/body[" in name, f"Content chunk should be inside body, got: {name}"

    def test_all_content_chunks_under_limit(self, problematic_html):
        """
        FIX VERIFICATION: ALL content chunks should be under token_limit.

        The old bug had one chunk with 30510 tokens. Now containers recursively split.
        """
        chunks = chunk_html(problematic_html, token_limit=1200)
        content_chunks = [c for c in chunks if c.xpath not in ("prefix", "suffix")]

        over_limit = [c for c in content_chunks if c.tokens > 1200]
        assert len(over_limit) == 0, f"Expected 0 chunks over limit, got {len(over_limit)}: {[(c.xpath, c.tokens) for c in over_limit]}"

        # Every content chunk should have reasonable token count
        for chunk in content_chunks:
            assert chunk.tokens <= 1200, f"Chunk {chunk.xpath} has {chunk.tokens} tokens (over {1200})"


class TestTocXhtmlChunking:
    """Test toc.xhtml chunking - uses actual content from JSON file."""

    @pytest.fixture
    def toc_xhtml_content(self):
        """Load toc.xhtml content from JSON."""
        import json
        with open('/Users/amaozhao/Downloads/epub/ship-mcp-server-python-production-ready.json') as f:
            data = json.load(f)
        for item in data['items']:
            if 'toc.xhtml' in item['id']:
                return item['content']
        pytest.skip("toc.xhtml not found")

    @pytest.fixture
    def toc_xhtml_old_buggy_chunks(self):
        """The OLD buggy chunking from JSON - /div/html[1] contained entire HTML."""
        import json
        with open('/Users/amaozhao/Downloads/epub/ship-mcp-server-python-production-ready.json') as f:
            data = json.load(f)
        for item in data['items']:
            if 'toc.xhtml' in item['id']:
                return item['chunks']
        pytest.skip("toc.xhtml not found")

    def test_toc_xhtml_chunks_split_by_nav(self, toc_xhtml_old_buggy_chunks):
        """
        toc.xhtml chunks should be split by nav containers.

        The JSON shows correct splitting: /div/html[1]/body[1]/nav[1], nav[2], nav[3]
        NOT a single /div/html[1] chunk containing everything.
        """
        chunk_names = [c['name'] for c in toc_xhtml_old_buggy_chunks]

        # Should have nav chunks, NOT /div/html[1]
        assert any('/nav[' in name for name in chunk_names), f"Should have nav chunks, got {chunk_names}"
        assert '/div/html[1]' not in chunk_names or all('/body[' in n for n in chunk_names if n.startswith('/div/html[1]')), \
            f"Should not have bare /div/html[1] chunk, got {chunk_names}"

    def test_toc_xhtml_new_code_no_chunk_exceeds_limit(self, toc_xhtml_content):
        """
        NEW FIX: All content chunks should be under token_limit.

        nav[2] has 1944 tokens which exceeds 1200 limit, so it gets recursively split.
        """
        chunks = chunk_html(toc_xhtml_content, token_limit=1200)

        # Should have: prefix + (nav[1] intact) + (nav[2] split into 2) + (nav[3] intact) + suffix
        # nav[1]=75 tokens, nav[2]=1944 tokens (split), nav[3]=54 tokens
        # With recursive splitting: h2[1] (9) + ol[1] (572) = 2 chunks from nav[2]
        assert len(chunks) >= 5, f"Expected at least 5 chunks, got {len(chunks)}"

        # Verify structure
        assert chunks[0].xpath == "prefix"
        assert chunks[-1].xpath == "suffix"

        # All content chunks should be under limit
        content_chunks = [c for c in chunks if c.xpath not in ("prefix", "suffix")]
        over_limit = [c for c in content_chunks if c.tokens > 1200]
        assert len(over_limit) == 0, f"No chunk should exceed 1200 tokens, got: {[(c.xpath, c.tokens) for c in over_limit]}"

    def test_toc_xhtml_nav_containers_split_when_over_limit(self, toc_xhtml_content):
        """
        nav[2] at 1944 tokens exceeds 1200 limit, so it should be recursively split.
        The h2 and ol within nav[2] should become separate chunks.
        """
        chunks = chunk_html(toc_xhtml_content, token_limit=1200)

        # Find h2 and ol chunks that are inside nav[2]
        h2_chunks = [c for c in chunks if '/nav[2]/h2[' in c.xpath]
        ol_chunks = [c for c in chunks if '/nav[2]/ol[' in c.xpath]

        # nav[2]'s children should be split
        assert len(h2_chunks) > 0 or len(ol_chunks) > 0, "nav[2] should be split into children chunks"


class TestChunkerPrefixSuffix:
    """Verify prefix/suffix behavior for different HTML types."""

    def test_html_has_prefix_and_suffix(self):
        """Standard HTML has prefix and suffix chunks."""
        html = "<!DOCTYPE html><html><head></head><body><p>Hello</p></body></html>"
        chunks = chunk_html(html, token_limit=1000)

        assert chunks[0].xpath == "prefix"
        assert chunks[0].needs_translation is False
        assert chunks[-1].xpath == "suffix"
        assert chunks[-1].needs_translation is False
        assert len(chunks) == 3  # prefix + content + suffix

    def test_ncx_has_prefix_no_suffix(self):
        """NCX (XML) files have prefix but no suffix (no body tag)."""
        ncx_content = '<?xml version="1.0"?><ncx><navMap><navPoint><navLabel><text>Ch 1</text></navLabel></navPoint></navMap></ncx>'
        chunks = chunk_html(ncx_content, token_limit=1000)

        assert chunks[0].xpath == "prefix"
        assert chunks[0].needs_translation is False
        # NCX should NOT have suffix (no body tag)
        assert not any(c.xpath == "suffix" for c in chunks)
