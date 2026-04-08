
from engine.agents.html_validator import HtmlValidator, validate_html_structure


class TestHtmlValidatorSelfClosing:
    """测试自闭合标签的处理"""

    def test_br_tag(self):
        """br 标签是自闭合的，不应该影响栈"""
        validator = HtmlValidator()
        html = "<p>Hello<br/>World</p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []
        assert validator.stack == []

    def test_img_tag(self):
        """img 标签是自闭合的"""
        validator = HtmlValidator()
        html = "<div><img src='test.jpg'/></div>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_hr_tag(self):
        """hr 标签是自闭合的"""
        validator = HtmlValidator()
        html = "<section><p>Text</p><hr/><p>More</p></section>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_input_tag(self):
        """input 标签是自闭合的"""
        validator = HtmlValidator()
        html = "<form><input type='text'/><input type='submit'/></form>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []


class TestHtmlValidatorMatched:
    """测试正确嵌套的 HTML"""

    def test_simple_matched(self):
        """简单的配对标签"""
        validator = HtmlValidator()
        html = "<div><p>Hello</p><span>World</span></div>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_nested_tags(self):
        """嵌套标签"""
        validator = HtmlValidator()
        html = "<div><p><em>Hello</em></p></div>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_deeply_nested(self):
        """深度嵌套"""
        validator = HtmlValidator()
        html = "<html><body><div><section><article><p><em><strong>Text</strong></em></p></article></section></div></body></html>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_mixed_inline_and_block(self):
        """混合块级和内联标签"""
        validator = HtmlValidator()
        html = "<p><em>italic</em> and <strong>bold</strong> and <code>code</code></p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []


class TestHtmlValidatorMismatched:
    """测试不匹配的标签"""

    def test_unexpected_close_tag(self):
        """意外的闭合标签"""
        validator = HtmlValidator()
        html = "<p>Hello</div>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert len(errors) > 0
        # 当栈顶是 <p> 但遇到 </div>，报 tag_mismatch
        assert errors[0]["type"] in ("unexpected_close", "tag_mismatch")

    def test_mismatched_tags(self):
        """标签不匹配"""
        validator = HtmlValidator()
        html = "<div><p></div></p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert len(errors) > 0
        assert errors[0]["type"] == "tag_mismatch"

    def test_mismatched_deeply_nested(self):
        """深度嵌套的不匹配"""
        validator = HtmlValidator()
        html = "<div><section><p><em></p></em></section></div>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert len(errors) > 0

    def test_unclosed_tag(self):
        """未闭合标签 - 叶子标签未闭合是错误"""
        validator = HtmlValidator()
        html = "<div><p>Hello<span>World"
        valid, errors = validator.validate_chunk(html, 0, "test")
        # 叶子标签未闭合是错误
        assert valid is False
        # p 和 span 是叶子标签，应该报错
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] in ("p", "span") for e in errors)


class TestHtmlValidatorChunkBoundary:
    """测试跨 chunk 边界的标签追踪"""

    def test_chunk_boundary_correct(self):
        """跨 chunk 边界但正确配对"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <div> 和 <em> - div 是容器标签，em 是叶子标签
        html1 = "<div><p>Hello <em>"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        # 叶子标签 em 未闭合是错误
        assert valid1 is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] == "em" for e in errors1)

        # Chunk 1: 先闭合 </em> 再闭合 </p> 再闭合 </div>
        html2 = "text</em></p></div>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        # 全部闭合，应该没有错误
        assert valid2 is True

    def test_chunk_boundary_with_paragraph(self):
        """跨 chunk 边界的段落标签"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <div>（容器标签）
        html1 = "<div><p>Hello"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        # p 是叶子标签，未闭合是错误
        assert valid1 is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] == "p" for e in errors1)

        # Chunk 1: 闭合 </p> 和 </div>
        html2 = " World</p></div>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True

    def test_chunk_boundary_mismatched(self):
        """跨 chunk 边界但标签不匹配"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <p> 和 <em>
        html1 = "<p>Hello <em>"
        validator.validate_chunk(html1, 0, "chunk0")
        assert ("em", 0) in validator.stack
        assert ("p", 0) in validator.stack

        # Chunk 1: 错误地闭合了 </p> 而不是 </em>
        html2 = "text</p> world</p>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is False
        assert errors2[0]["type"] == "tag_mismatch"

    def test_merged_validation(self):
        """测试合并后的验证"""
        validator = HtmlValidator()

        chunks = [
            "<p>Hello <em>",
            "text</em></p>"
        ]
        chunk_names = ["chunk0", "chunk1"]

        valid, errors = validator.validate_merged(chunks, chunk_names)
        assert valid is True
        assert errors == []

    def test_merged_validation_fails(self):
        """测试合并后验证失败"""
        validator = HtmlValidator()

        chunks = [
            "<p>Hello <em>",
            "text</p>"  # 错误：应该是 </em>
        ]
        chunk_names = ["chunk0", "chunk1"]

        valid, errors = validator.validate_merged(chunks, chunk_names)
        assert valid is False
        assert len(errors) > 0

    def test_merged_validation_unclosed(self):
        """测试合并后有未闭合标签"""
        validator = HtmlValidator()

        # 使用容器标签来测试合并后的未闭合检查
        chunks = [
            "<div><table><tr>",  # 容器标签未闭合
            "text</tr></div>"
        ]
        chunk_names = ["chunk0", "chunk1"]

        valid, errors = validator.validate_merged(chunks, chunk_names)
        assert valid is False
        # 检查错误类型是 unclosed_container_tags
        assert any(e["type"] == "unclosed_container_tags" for e in errors)


class TestValidateHtmlStructure:
    """测试 validate_html_structure 辅助函数"""

    def test_valid_html(self):
        """有效 HTML 返回 True"""
        html = "<div><p>Hello</p></div>"
        valid, errors = validate_html_structure(html)
        assert valid is True
        assert errors == []

    def test_invalid_html(self):
        """无效 HTML 返回 False"""
        html = "<div><p></div></p>"
        valid, errors = validate_html_structure(html)
        assert valid is False
        assert len(errors) > 0

    def test_error_message_format(self):
        """错误信息格式正确"""
        html = "<div><p></div></p>"
        valid, errors = validate_html_structure(html)
        assert valid is False
        # 错误信息应该描述性的
        assert "标签不匹配" in errors[0] or "不匹配" in errors[0]


class TestHtmlValidatorWithPlaceholders:
    """测试带有占位符的 HTML"""

    def test_placeholder_not_affect_validation(self):
        """占位符 [id0] 等不应该影响验证"""
        validator = HtmlValidator()
        html = "<p>Hello [id0] World</p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []

    def test_placeholder_between_tags(self):
        """在标签之间的占位符"""
        validator = HtmlValidator()
        html = "<p><em>Hello</em>[id0]<span>World</span></p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        assert errors == []


class TestLeafTagUnclosedDetection:
    """Phase 5: 测试叶子标签未闭合检测"""

    def test_li_unclosed_is_error(self):
        """li 标签未闭合是错误"""
        validator = HtmlValidator()
        html = "<ol><li>Item 1<li>Item 2"  # 缺少 </li>
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] == "li" for e in errors)

    def test_h1_unclosed_is_error(self):
        """h1 标签未闭合是错误"""
        validator = HtmlValidator()
        html = "<h1>Heading<h2>Another"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] in ("h1", "h2") for e in errors)

    def test_em_unclosed_is_error(self):
        """em 标签未闭合是错误"""
        validator = HtmlValidator()
        html = "<p>Hello <em>world"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] == "em" for e in errors)

    def test_container_tag_unclosed_is_ok(self):
        """容器标签（div）未闭合是正常的"""
        validator = HtmlValidator()
        html = "<div><p>Hello</p>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        # div 是容器标签，未闭合是正常的
        assert valid is True
        # p 已闭合，div 未闭合（跨 chunk），所以栈应该只有 div
        assert ("div", 0) in validator.stack

    def test_nav_unclosed_is_ok(self):
        """nav 标签未闭合是正常的（ol 已闭合，只有 nav 未闭合）"""
        validator = HtmlValidator()
        html = "<nav><ol><li>Item</li></ol>"
        valid, errors = validator.validate_chunk(html, 0, "test")
        assert valid is True
        # nav 是容器标签未闭合，ol 已闭合，li 已闭合
        assert ("nav", 0) in validator.stack
        assert ("ol", 0) not in validator.stack


class TestContainerTagCrossChunk:
    """Phase 5: 测试容器标签跨 chunk 正常"""

    def test_nav_cross_chunk_is_valid(self):
        """nav 标签跨 chunk 是正常的"""
        validator = HtmlValidator()

        # Chunk 0: 打开 nav 和 ol，li 未闭合是叶子标签错误
        html1 = "<nav><ol><li>Item 1"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        # li 是叶子标签，未闭合是错误
        assert valid1 is False
        assert ("nav", 0) in validator.stack
        assert ("ol", 0) in validator.stack
        assert ("li", 0) in validator.stack

        # Chunk 1: 闭合
        html2 = "</li></ol></nav>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        assert validator.stack == []

    def test_table_cross_chunk_is_valid(self):
        """table 标签跨 chunk 是正常的"""
        validator = HtmlValidator()

        # Chunk 0: 打开 table，td 打开后立即闭合，只留 table 和 tr
        html1 = "<table><tr><td>Cell</td>"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        assert valid1 is True
        # table 和 tr 是容器标签未闭合
        assert ("table", 0) in validator.stack
        assert ("tr", 0) in validator.stack

        # Chunk 1: 闭合 - td closes tr closes table
        html2 = "</tr></table>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        assert validator.stack == []

    def test_ncx_cross_chunk_is_valid(self):
        """NCX 标签跨 chunk 是正常的"""
        validator = HtmlValidator()

        # Chunk 0: 打开 ncx 和 navMap
        html1 = "<?xml version=\"1.0\"?><ncx><navMap><navPoint>"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        assert valid1 is True
        assert ("ncx", 0) in validator.stack
        assert ("navmap", 0) in validator.stack
        assert ("navpoint", 0) in validator.stack

        # Chunk 1: 闭合
        html2 = "</navPoint></navMap></ncx>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        assert validator.stack == []

    def test_merged_ncx_valid(self):
        """合并后的 NCX 内容验证通过"""
        validator = HtmlValidator()

        chunks = [
            "<?xml version=\"1.0\"?><ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>",
        ]
        chunk_names = ["ncx_chunk"]

        valid, errors = validator.validate_merged(chunks, chunk_names)
        assert valid is True
        assert errors == []

    def test_leaf_tag_across_chunk_is_error(self):
        """叶子标签（如 p）跨 chunk 未闭合是错误"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <p> 但不闭合
        html1 = "<div><p>Hello"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        assert valid1 is False
        assert any(e["type"] == "unclosed_leaf_tag" and e["tag"] == "p" for e in errors1)

        # Chunk 1: 闭合 </p>
        html2 = " World</p></div>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True

    def test_container_tag_closed_in_later_chunk(self):
        """容器标签（div）在后续 chunk 中正确闭合"""
        validator = HtmlValidator()

        # Chunk 0: 打开 div，p 已闭合（section 不是容器所以是叶子标签错误）
        html1 = "<section><div><p>Para 1</p>"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        # section 不是容器标签，未闭合是错误
        assert valid1 is False
        assert ("section", 0) in validator.stack
        assert ("div", 0) in validator.stack

        # Chunk 1: 继续并闭合
        html2 = "<p>Para 2</p></div></section>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        assert validator.stack == []

