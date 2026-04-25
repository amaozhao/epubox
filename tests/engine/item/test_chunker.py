import warnings
from unittest.mock import patch

from bs4 import XMLParsedAsHTMLWarning

from engine.item.chunker import DomChunker, count_tokens


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

    def test_split_on_secondary_placeholder_limit(self):
        """测试在 token 充足时，仍会因二级占位符数量超限而切 chunk。"""
        html = (
            "<html><body>"
            "<p>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</p>"
            "<p>beta [CODE:4] [CODE:5] [CODE:6] [CODE:7]</p>"
            "<p>gamma [CODE:8] [CODE:9] [CODE:10] [CODE:11]</p>"
            "</body></html>"
        )
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=8)
        chunks = chunker.chunk(html)

        assert len(chunks) == 2
        assert chunks[0].original.count("[CODE:") == 8
        assert chunks[1].original.count("[CODE:") == 4

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

    def test_oversized_table_recurses_to_rows(self):
        """测试超限 table 会递归拆到更细粒度，而不是整表超限保留。"""
        html = (
            "<html><body><table>"
            "<tr><td>Cell 1 text</td><td>Cell 2 text</td></tr>"
            "<tr><td>Cell 3 text</td><td>Cell 4 text</td></tr>"
            "</table></body></html>"
        )
        chunker = DomChunker(token_limit=10)
        chunks = chunker.chunk(html)
        assert len(chunks) > 1
        assert max(chunk.tokens for chunk in chunks) <= 10

    def test_oversized_list_recurses_to_items(self):
        """测试超限 ol/ul 会递归拆到 li，而不是整列表超限保留。"""
        html = (
            "<html><body><ol>"
            "<li><p>First item with enough text</p><p>Extra detail A</p></li>"
            "<li><p>Second item with enough text</p><p>Extra detail B</p></li>"
            "<li><p>Third item with enough text</p><p>Extra detail C</p></li>"
            "</ol></body></html>"
        )
        chunker = DomChunker(token_limit=12)
        chunks = chunker.chunk(html)
        assert len(chunks) > 1
        assert max(chunk.tokens for chunk in chunks) <= 12

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
        assert len(chunks) == 1
        assert chunks[0].chunk_mode == "nav_text"
        assert "[NAVTXT:0]" in chunks[0].original
        assert len(chunks[0].nav_targets) == 1

    def test_large_nav_file_respects_token_limit(self):
        """测试大导航文件会按 token_limit 切分为多个 nav_text chunk。"""
        nav_points = "".join(
            f'<navPoint id="ch{i}"><navLabel><text>Very long chapter title number {i} with extra words</text></navLabel></navPoint>'
            for i in range(40)
        )
        html = f"<ncx><navMap>{nav_points}</navMap></ncx>"
        chunker = DomChunker(token_limit=80)
        chunks = chunker.chunk(html, is_nav_file=True)

        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.chunk_mode == "nav_text"
            assert chunk.tokens <= 80
            assert chunk.nav_targets

    def test_nav_file_splits_on_unit_limit_even_when_token_limit_is_high(self):
        """测试导航文件在 token 很充足时也会按单块最大条目数切分，降低模型漏 marker 风险。"""
        nav_points = "".join(
            f'<navPoint id="ch{i}"><navLabel><text>Chapter {i}</text></navLabel></navPoint>' for i in range(95)
        )
        html = f"<ncx><navMap>{nav_points}</navMap></ncx>"
        chunker = DomChunker(token_limit=5000)

        chunks = chunker.chunk(html, is_nav_file=True)

        assert len(chunks) > 1
        assert sum(len(chunk.nav_targets) for chunk in chunks) == 95
        assert all(len(chunk.nav_targets) <= chunker.nav_unit_limit for chunk in chunks)

    def test_nav_file_splits_short_titles_before_reaching_48_markers(self):
        """测试即使标题很短，默认导航分块也不会把 48 条 marker 塞进同一个 chunk。"""
        nav_points = "".join(
            f'<navPoint id="ch{i}"><navLabel><text>Chapter {i}</text></navLabel></navPoint>' for i in range(48)
        )
        html = f"<ncx><navMap>{nav_points}</navMap></ncx>"
        chunker = DomChunker(token_limit=5000)

        chunks = chunker.chunk(html, is_nav_file=True)

        assert len(chunks) > 1
        assert sum(len(chunk.nav_targets) for chunk in chunks) == 48
        assert all(len(chunk.nav_targets) < 48 for chunk in chunks)

    def test_nav_xhtml_collects_multiple_nav_sections(self):
        """测试 nav.xhtml 中多个 nav 容器的文本都会被纳入导航分块。"""
        html = """
        <html><body>
          <nav epub:type="toc"><ol><li><a href="#c1">Chapter 1</a></li></ol></nav>
          <nav epub:type="landmarks"><ol><li><a href="#cover">Cover</a></li></ol></nav>
        </body></html>
        """
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)

        assert len(chunks) == 1
        assert "Chapter 1" in chunks[0].original
        assert "Cover" in chunks[0].original
        assert len(chunks[0].nav_targets) == 2

    def test_embedded_toc_nav_in_regular_document_uses_nav_text_chunks(self):
        """测试普通章节文件中的目录型 <nav class='toc'> 也走 nav_text 分块。"""
        html = """
        <html><body>
          <h1>Front Matter</h1>
          <nav class="toc">
            <div><a href="c1.xhtml"><span class="label">Chapter 1</span></a></div>
            <div><a href="c2.xhtml"><span class="label">Chapter 2</span></a></div>
          </nav>
          <p>Preface text.</p>
        </body></html>
        """
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=False)

        assert any(chunk.chunk_mode == "nav_text" for chunk in chunks)
        nav_chunk = next(chunk for chunk in chunks if chunk.chunk_mode == "nav_text")
        assert "[NAVTXT:0] Chapter 1" in nav_chunk.original
        assert "[NAVTXT:1] Chapter 2" in nav_chunk.original
        assert len(nav_chunk.nav_targets) == 2
        assert all("<nav" not in chunk.original for chunk in chunks if chunk.chunk_mode == "html_fragment")

    def test_large_index_nav_in_regular_document_is_recursively_split(self):
        """测试大型 index nav 不会被整块保留，而是递归拆成多个 html_fragment chunk。"""
        entries = "".join(
            f'<p class="ix1"><span class="IX-Header">Entry {i}</span><a href="c{i}.xhtml#idx{i}"><span class="IX-Header">{i}</span></a></p>'
            for i in range(120)
        )
        html = f'<html><body><nav epub:type="index" id="index-nav"><section><h1>A</h1>{entries}</section></nav></body></html>'
        chunker = DomChunker(token_limit=120)

        chunks = chunker.chunk(html, is_nav_file=False)

        assert len(chunks) > 1
        assert all(chunk.chunk_mode == "html_fragment" for chunk in chunks)
        assert max(chunk.tokens for chunk in chunks) <= 120
        assert all("<nav" not in chunk.original for chunk in chunks)

    def test_toc_ncx_skips_xml_declaration_text(self):
        """测试 toc.ncx 分块时不会把 XML 声明当作可翻译导航文本。"""
        html = """<?xml version='1.0' encoding='utf-8'?><ncx><navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"""
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)

        assert len(chunks) == 1
        assert "xml version" not in chunks[0].original
        assert "[NAVTXT:0] Chapter 1" in chunks[0].original
        assert len(chunks[0].nav_targets) == 1

    def test_toc_ncx_chunking_avoids_xml_parsed_as_html_warning(self):
        """测试 NCX 分块不会触发 BeautifulSoup 的 XMLParsedAsHTMLWarning。"""
        html = """<?xml version='1.0' encoding='utf-8'?><ncx><navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"""
        chunker = DomChunker(token_limit=1000)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            chunks = chunker.chunk(html, is_nav_file=True)

        assert chunks
        assert not any(issubclass(w.category, XMLParsedAsHTMLWarning) for w in caught)

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

    def test_recursive_on_secondary_placeholder_limit(self):
        """测试 token 未超限时，容器也会因占位符超限递归到子元素。"""
        html = (
            "<html><body><div>"
            "<p>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</p>"
            "<p>beta [CODE:4] [CODE:5] [CODE:6] [CODE:7]</p>"
            "</div></body></html>"
        )
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=4)
        chunks = chunker.chunk(html)

        assert len(chunks) == 2
        assert all(chunk.original.count("[CODE:") == 4 for chunk in chunks)
        assert all(chunk.xpaths == [f"/html/body/div/p[{i}]"] for i, chunk in enumerate(chunks, start=1))

    def test_oversized_leaf_element_is_preserved_when_not_splittable(self):
        """测试无法继续细分的超限叶子元素不会在递归时丢失内容。"""
        html = "<html><body><p>This paragraph is intentionally long and plain text only.</p></body></html>"
        chunker = DomChunker(token_limit=3)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "intentionally long" in chunks[0].original

    def test_placeholder_heavy_leaf_is_preserved_when_not_splittable(self):
        """测试无法继续细分的占位符密集叶子元素不会因占位符超限而丢失。"""
        html = "<html><body><p>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</p></body></html>"
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=2)
        chunks = chunker.chunk(html)

        assert len(chunks) == 1
        assert chunks[0].original == "<p>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</p>"

    def test_atomic_container_ignores_secondary_placeholder_limit(self):
        """测试原子容器即使占位符超限也保持整体不拆分。"""
        html = "<html><body><figure>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</figure></body></html>"
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=2)
        chunks = chunker.chunk(html)

        assert len(chunks) == 1
        assert chunks[0].original == "<figure>alpha [CODE:0] [CODE:1] [CODE:2] [CODE:3]</figure>"

    def test_title_block_respects_secondary_placeholder_limit(self):
        """测试 title block 也参与占位符上限切分。"""
        html = (
            "<html><head><title>Title [CODE:0] [CODE:1] [CODE:2]</title></head>"
            "<body><p>Body [CODE:3]</p></body></html>"
        )
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=2)
        chunks = chunker.chunk(html)

        assert len(chunks) == 2
        assert chunks[0].original == "<title>Title [CODE:0] [CODE:1] [CODE:2]</title>"
        assert chunks[1].original == "<p>Body [CODE:3]</p>"

    def test_mixed_secondary_placeholders_all_count_toward_limit(self):
        """测试 PRE/CODE/STYLE 混合时会一起计入占位符上限。"""
        html = (
            "<html><body>"
            "<p>alpha [PRE:0] [CODE:0]</p>"
            "<p>beta [STYLE:0] [CODE:1]</p>"
            "<p>gamma [CODE:2]</p>"
            "</body></html>"
        )
        chunker = DomChunker(token_limit=1000, secondary_placeholder_limit=4)
        chunks = chunker.chunk(html)

        assert len(chunks) == 2
        assert chunks[0].original.count("[PRE:") == 1
        assert chunks[0].original.count("[STYLE:") == 1
        assert chunks[0].original.count("[CODE:") == 2
        assert chunks[1].original == "<p>gamma [CODE:2]</p>"

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
        """测试 tiktoken 模型未找到时的 fallback。"""
        with patch("engine.item.chunker.tiktoken.encoding_for_model", side_effect=KeyError):
            tokens = count_tokens("hello world")
            assert tokens > 0

    def test_count_tokens_network_failure_fallback(self):
        """测试 tiktoken 词表下载失败时会回退到离线估算。"""
        with patch("engine.item.chunker.tiktoken.encoding_for_model", side_effect=RuntimeError("offline")):
            with patch("engine.item.chunker.tiktoken.get_encoding", side_effect=RuntimeError("offline")):
                tokens = count_tokens("hello world")
                assert tokens == 2

    def test_count_tokens_offline_fallback(self):
        """测试 tiktoken 资源不可用时仍能进行本地估算。"""
        with (
            patch("engine.item.chunker._get_tokenizer", return_value=None),
        ):
            tokens = count_tokens("hello world")
            assert tokens == 2

    def test_whitespace_only_children_skipped(self):
        """测试空白文本节点被跳过（覆盖 line 107）"""
        # 元素之间有换行和空格的 HTML
        html = "<html><body>\n  <p>Text</p>\n  </body></html>"
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html)
        assert len(chunks) == 1
        assert "Text" in chunks[0].original
