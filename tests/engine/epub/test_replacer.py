
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
