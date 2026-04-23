import warnings

from bs4 import XMLParsedAsHTMLWarning

from engine.agents.verifier import validate_translated_html, verify_final_html
from engine.epub.replacer import DomReplacer
from engine.item.chunker import DomChunker
from engine.schemas import Chunk, EpubItem
from engine.schemas.translator import TranslationStatus


class TestDomReplacer:
    """测试 DomReplacer 类"""

    def test_basic_restore(self):
        """测试基本恢复：单个 chunk 的 xpath 替换"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
        )
        chunk = Chunk(
            name="test0001",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "<p>你好</p>" in result
        assert "<p>Hello</p>" not in result

    def test_multiple_xpaths(self):
        """测试多 xpath 替换"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><h1>Title</h1><p>First</p><p>Second</p></body></html>",
        )
        chunk = Chunk(
            name="test0002",
            original="<h1>Title</h1>\n<p>First</p>\n<p>Second</p>",
            translated="<h1>标题</h1>\n<p>第一</p>\n<p>第二</p>",
            status=TranslationStatus.COMPLETED,
            tokens=20,
            xpaths=["/html/body/h1", "/html/body/p[1]", "/html/body/p[2]"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "标题" in result
        assert "第一" in result
        assert "第二" in result

    def test_skip_untranslated(self):
        """测试跳过 UNTRANSLATED 的 chunk"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
        )
        chunk = Chunk(
            name="test0003",
            original="<p>Hello</p>",
            translated="<p>Hello</p>",
            status=TranslationStatus.TRANSLATION_FAILED,
            tokens=10,
            xpaths=["/html/body/p"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        # UNTRANSLATED 应被跳过，保留原文
        assert "<p>Hello</p>" in result

    def test_precode_restore(self):
        """测试 PreCode 占位符恢复"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Text</p><pre>code()</pre></body></html>",
            preserved_pre=["<pre>code()</pre>"],
        )
        chunk = Chunk(
            name="test0004",
            original="<p>Text</p>",
            translated="<p>文本</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "文本" in result

    def test_no_chunks(self):
        """测试无 chunks 时返回原内容"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
            chunks=[],
        )
        replacer = DomReplacer()
        result = replacer.restore(item)
        assert result == item.content

    def test_translated_elements_fewer_than_xpaths(self):
        """测试翻译后元素数少于 xpath 数时，整块回写应放弃，避免半替换。"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>A</p><p>B</p></body></html>",
        )
        chunk = Chunk(
            name="test0010",
            original="<p>A</p>\n<p>B</p>",
            translated="<p>甲</p>",  # 只有 1 个元素，但 xpaths 有 2 个
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p[1]", "/html/body/p[2]"],
        )
        item.chunks = [chunk]
        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "甲" not in result
        assert "A" in result
        assert "B" in result

    def test_partial_xpath_failure_marks_chunk_writeback_failed(self):
        """测试 xpath 回写失败时会显式标记为 WRITEBACK_FAILED。"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>A</p><p>B</p></body></html>",
        )
        chunk = Chunk(
            name="test0010b",
            original="<p>A</p>\n<p>B</p>",
            translated="<p>甲</p><p>乙</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p[1]", "/html/body/div"],
        )
        item.chunks = [chunk]
        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "甲" not in result
        assert "乙" not in result
        assert "A" in result
        assert "B" in result
        assert chunk.status == TranslationStatus.WRITEBACK_FAILED

    def test_restore_strips_internal_writeback_tracking_attributes(self):
        """回写过程中使用的内部跟踪属性不应泄露到最终 HTML。"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
        )
        chunk = Chunk(
            name="tracked001",
            original="<p>Hello</p>",
            translated='<p data-epubox-wb-id="external">你好</p>',
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)

        assert result is not None
        assert "data-epubox-wb-id" not in result

    def test_restore_skips_broader_chunk_when_xpaths_overlap_descendants(self):
        """祖先/后代 xpath 冲突时，优先保留更具体的 chunk，避免后续找不到节点。"""
        item = EpubItem(
            id="ch-overlap.xhtml",
            path="/tmp/ch-overlap.xhtml",
            content="<html><body><section><p>Alpha</p><p>Beta</p></section></body></html>",
        )
        broad_chunk = Chunk(
            name="broad001",
            original="<section><p>Alpha</p><p>Beta</p></section>",
            translated="<section><p>甲</p><p>乙</p></section>",
            status=TranslationStatus.COMPLETED,
            tokens=20,
            xpaths=["/html/body/section"],
        )
        narrow_chunk = Chunk(
            name="narrow001",
            original="<p>Beta</p>",
            translated="<p>乙-细化</p>",
            status=TranslationStatus.COMPLETED,
            tokens=8,
            xpaths=["/html/body/section/p[2]"],
        )
        item.chunks = [broad_chunk, narrow_chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)

        assert result is not None
        assert "Alpha" in result
        assert "乙-细化" in result
        assert broad_chunk.status == TranslationStatus.WRITEBACK_FAILED
        assert narrow_chunk.status == TranslationStatus.COMPLETED

    def test_xpath_not_found(self):
        """测试 xpath 未找到时 warning（覆盖 line 90）"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
        )
        chunk = Chunk(
            name="test0011",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/div"],  # 不存在的 xpath
        )
        item.chunks = [chunk]
        replacer = DomReplacer()
        result = replacer.restore(item)
        assert "Hello" in result  # 原文保留

    def test_verify_failure_logged(self):
        """测试最终验证失败时阻止写入并标记为 WRITEBACK_FAILED。"""
        item = EpubItem(
            id="ch1.xhtml",
            path="/tmp/ch1.xhtml",
            content="<html><body><p>Hello</p></body></html>",
        )
        chunk = Chunk(
            name="test0012",
            original="<p>Hello</p>",
            translated="<p>[PRE:0] 你好</p>",  # 残留占位符会验证失败
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/p"],
        )
        item.chunks = [chunk]
        replacer = DomReplacer()
        result = replacer.restore(item)
        assert result is None
        assert item.translated is None
        assert chunk.status == TranslationStatus.WRITEBACK_FAILED

    def test_nav_text_writeback_success(self):
        """导航文本模式按 marker 精确回写到文本节点。"""
        html = (
            "<ncx><navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint>"
            "<navPoint id='ch2'><navLabel><text>Chapter 2</text></navLabel></navPoint></navMap></ncx>"
        )
        item = EpubItem(id="toc.ncx", path="/tmp/toc.ncx", content=html)
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)
        assert len(chunks) == 1

        chunk = chunks[0]
        chunk.translated = chunk.original.replace("Chapter 1", "第1章").replace("Chapter 2", "第2章")
        chunk.status = TranslationStatus.COMPLETED
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        assert result is not None
        assert "第1章" in result
        assert "第2章" in result

    def test_nav_text_restore_avoids_xml_parsed_as_html_warning(self):
        """导航文本回写不会触发 XMLParsedAsHTMLWarning。"""
        html = "<ncx><navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"
        item = EpubItem(id="toc.ncx", path="/tmp/toc.ncx", content=html)
        chunker = DomChunker(token_limit=1000)
        chunk = chunker.chunk(html, is_nav_file=True)[0]
        chunk.translated = chunk.original.replace("Chapter 1", "第1章")
        chunk.status = TranslationStatus.COMPLETED
        item.chunks = [chunk]

        replacer = DomReplacer()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = replacer.restore(item)

        assert result is not None
        assert not any(issubclass(w.category, XMLParsedAsHTMLWarning) for w in caught)

    def test_nav_text_marker_mismatch_fails_writeback(self):
        """导航文本模式 marker 不一致时应拒绝回写并标记失败。"""
        html = "<ncx><navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"
        item = EpubItem(id="toc.ncx", path="/tmp/toc.ncx", content=html)
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)
        chunk = chunks[0]
        chunk.translated = chunk.original.replace("[NAVTXT:0]", "[NAVTXT:9]").replace("Chapter 1", "第1章")
        chunk.status = TranslationStatus.COMPLETED
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)
        assert result is not None
        assert "Chapter 1" in result
        assert chunk.status == TranslationStatus.WRITEBACK_FAILED

    def test_nav_text_writeback_supports_prefixed_case_insensitive_xpath(self):
        """导航回写兼容旧 checkpoint 中带 namespace 前缀且大小写不同的 xpath。"""
        html = (
            "<?xml version='1.0' encoding='utf-8'?>"
            "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/'>"
            "<docTitle><text>Original Title</text></docTitle>"
            "<navMap><navPoint id='ch1'><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap>"
            "</ncx>"
        )
        item = EpubItem(id="toc.ncx", path="/tmp/toc.ncx", content=html)
        chunk = Chunk(
            name="nav-prefixed",
            original="[NAVTXT:0] Original Title\n[NAVTXT:1] Chapter 1",
            translated="[NAVTXT:0] 中文书名\n[NAVTXT:1] 第1章",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            chunk_mode="nav_text",
            xpaths=[],
            nav_targets=[
                {
                    "marker": "[NAVTXT:0]",
                    "xpath": "/ncx:ncx/ncx:doctitle/ncx:text",
                    "text_index": 0,
                    "original_text": "Original Title",
                },
                {
                    "marker": "[NAVTXT:1]",
                    "xpath": "/ncx:ncx/ncx:navmap/ncx:navpoint/ncx:navlabel/ncx:text",
                    "text_index": 0,
                    "original_text": "Chapter 1",
                },
            ],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)

        assert result is not None
        assert "中文书名" in result
        assert "第1章" in result
        assert chunk.status == TranslationStatus.COMPLETED

    def test_nav_text_writeback_preserves_inline_structure(self):
        """导航文本模式只替换文本节点，不破坏锚点等内联结构。"""
        html = "<html><body><nav><ol><li><a href='#c1'><span id='toc-link-1'></span>Chapter 1</a></li></ol></nav></body></html>"
        item = EpubItem(id="nav.xhtml", path="/tmp/nav.xhtml", content=html)
        chunker = DomChunker(token_limit=1000)
        chunks = chunker.chunk(html, is_nav_file=True)

        chunk = chunks[0]
        chunk.translated = chunk.original.replace("Chapter 1", "第1章")
        chunk.status = TranslationStatus.COMPLETED
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)

        assert result is not None
        assert "第1章" in result
        assert 'id="toc-link-1"' in result

    def test_writeback_uses_preprocessed_dom_when_code_placeholders_shift_sibling_indexes(self):
        """预处理移除 code-like 容器后，回写仍应命中原始目标节点。"""
        html = (
            "<html><body><section>"
            "<div class='highlight'><tt>x</tt></div>"
            "<div><p>Hello there more text here for chunking split</p></div>"
            "</section></body></html>"
        )
        item = EpubItem(
            id="ch-shift.xhtml",
            path="/tmp/ch-shift.xhtml",
            content=html,
            preserved_pre=['<div class="highlight"><tt>x</tt></div>'],
            preserved_code=[],
            preserved_style=[],
        )
        chunk = Chunk(
            name="shift001",
            original="<p>Hello there more text here for chunking split</p>",
            translated="<p>你好，这里是一段会触发递归分块的文本</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
            xpaths=["/html/body/section/div/p"],
        )
        item.chunks = [chunk]

        replacer = DomReplacer()
        result = replacer.restore(item)

        assert result is not None
        assert "你好，这里是一段会触发递归分块的文本" in result
        assert "<tt>x</tt>" in result
        assert chunk.status == TranslationStatus.COMPLETED


class TestValidateTranslatedHtml:
    """测试翻译结果验证"""

    def test_valid(self):
        """测试有效的翻译"""
        is_valid, _ = validate_translated_html("<p>Hello</p>", "<p>你好</p>")
        assert is_valid

    def test_element_count_mismatch(self):
        """测试元素数量不一致"""
        # 现在用 <div> 包裹后是有效 XML，所以元素数量检查生效
        is_valid, error = validate_translated_html("<p>A</p>", "<p>A</p><p>B</p>")
        assert not is_valid
        assert "元素数量不一致" in error

    def test_tag_name_mismatch(self):
        """测试标签名不一致"""
        is_valid, error = validate_translated_html("<p>A</p>", "<div>A</div>")
        assert not is_valid
        assert "标签不一致" in error

    def test_placeholder_preserved(self):
        """测试占位符完整保留"""
        is_valid, _ = validate_translated_html("<p>[PRE:0] text</p>", "<p>[PRE:0] 文本</p>")
        assert is_valid

    def test_placeholder_missing(self):
        """测试占位符丢失"""
        is_valid, error = validate_translated_html("<p>[PRE:0] text</p>", "<p>文本</p>")
        assert not is_valid
        assert "占位符" in error

    def test_placeholder_index_changed(self):
        """测试占位符索引变化也会被视为无效。"""
        is_valid, error = validate_translated_html("<p>[PRE:0] text</p>", "<p>[PRE:1] 文本</p>")
        assert not is_valid
        assert "占位符" in error

    def test_placeholder_order_reports_mismatch_positions(self):
        """测试占位符顺序错误时会报告具体错位位置。"""
        original = "<p>Alpha [CODE:1]</p><p>Beta [CODE:2] [CODE:3]</p>"
        translated = "<p>甲 [CODE:2]</p><p>乙 [CODE:1] [CODE:3]</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "CODE 占位符归属/数量不一致" in error
        assert "元素1 位置1" in error
        assert "原始 [CODE:1]" in error
        assert "翻译 [CODE:2]" in error

    def test_adjacent_code_swap_within_same_element_is_accepted(self):
        """测试同一顶层元素内的相邻 CODE 换位会被接受。"""
        original = "<p>[CODE:1] [CODE:2] text</p>"
        translated = "<p>[CODE:2] [CODE:1] 文本</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_multiple_adjacent_code_swaps_within_same_element_are_accepted(self):
        """测试同一顶层元素内多组不重叠的相邻 CODE 换位会被接受。"""
        original = "<p>[CODE:1] [CODE:2] [CODE:3] [CODE:4] text</p>"
        translated = "<p>[CODE:2] [CODE:1] [CODE:4] [CODE:3] 文本</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_non_adjacent_code_reorder_within_same_element_is_accepted(self):
        """测试同一顶层元素内的非相邻重排也会被接受。"""
        original = "<p>[CODE:31], [CODE:32], and [CODE:33]</p>"
        translated = "<p>在 [CODE:33] 所在目录中运行 [CODE:31] 和 [CODE:32]</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_code_swap_across_top_level_elements_is_rejected(self):
        """测试跨顶层元素的 CODE 换位仍然会被拒绝。"""
        original = "<p>[CODE:1]</p><p>[CODE:2]</p>"
        translated = "<p>[CODE:2]</p><p>[CODE:1]</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "CODE 占位符归属/数量不一致" in error

    def test_code_missing_within_same_element_is_rejected(self):
        """测试同一元素内缺失 CODE 占位符仍然会被拒绝。"""
        original = "<p>[CODE:1] [CODE:2] text</p>"
        translated = "<p>[CODE:2] 文本</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "CODE 占位符归属/数量不一致" in error

    def test_adjacent_pre_swap_is_still_rejected(self):
        """测试 PRE 占位符依旧要求严格顺序，不允许相邻换位。"""
        original = "<p>[PRE:1] [PRE:2] text</p>"
        translated = "<p>[PRE:2] [PRE:1] 文本</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "PRE 占位符顺序不一致" in error


class TestVerifyFinalHtml:
    """测试最终 HTML 验证"""

    def test_valid_xml(self):
        """测试有效的 XHTML"""
        html = "<html><body><p>Hello</p></body></html>"
        is_valid, _ = verify_final_html(html, html)
        assert is_valid

    def test_residual_placeholder(self):
        """测试残留占位符检测"""
        html = "<html><body><p>[PRE:0]</p></body></html>"
        is_valid, error = verify_final_html("", html)
        assert not is_valid
        assert "残留占位符" in error
