
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
        """未闭合标签 - validate_chunk 不检查栈是否为空，只检查标签匹配"""
        validator = HtmlValidator()
        html = "<div><p>Hello<span>World"
        valid, errors = validator.validate_chunk(html, 0, "test")
        # validate_chunk 不会检查未闭合标签（因为跨 chunk 可能正常）
        # 只检查标签是否匹配
        assert valid is True  # 没有标签冲突，只是未闭合
        # 栈中应该有未闭合的标签
        assert len(validator.stack) == 3  # div, p, span


class TestHtmlValidatorChunkBoundary:
    """测试跨 chunk 边界的标签追踪"""

    def test_chunk_boundary_correct(self):
        """跨 chunk 边界但正确配对"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <p> 和 <em>
        html1 = "<p>Hello <em>"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        assert valid1 is True
        # 栈中应该有 p 和 em
        assert ("em", 0) in validator.stack
        assert ("p", 0) in validator.stack

        # Chunk 1: 先闭合 </em> 再闭合 </p>
        html2 = "text</em></p>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        # 栈应该清空（两个都闭合了）
        assert validator.stack == []

    def test_chunk_boundary_with_paragraph(self):
        """跨 chunk 边界的段落标签"""
        validator = HtmlValidator()

        # Chunk 0: 打开 <p>
        html1 = "<p>Hello"
        valid1, errors1 = validator.validate_chunk(html1, 0, "chunk0")
        assert valid1 is True
        assert ("p", 0) in validator.stack

        # Chunk 1: 闭合 </p>
        html2 = " World</p>"
        valid2, errors2 = validator.validate_chunk(html2, 1, "chunk1")
        assert valid2 is True
        assert validator.stack == []

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

        chunks = [
            "<p>Hello <em>",  # <em> 未闭合
            "text</p>"
        ]
        chunk_names = ["chunk0", "chunk1"]

        valid, errors = validator.validate_merged(chunks, chunk_names)
        assert valid is False
        # 检查错误类型是 unclosed_tags
        assert any(e["type"] == "unclosed_tags" for e in errors)


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
