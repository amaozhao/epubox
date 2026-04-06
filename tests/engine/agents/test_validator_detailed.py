"""Tests for detailed HTML validation with context"""
import pytest
from engine.agents.validator import validate_html_with_context, validate_html_pairing


class TestValidateHtmlWithContext:
    """测试带上下文的 HTML 验证"""

    def test_valid_html_returns_true(self):
        """有效的 HTML 应该返回 True"""
        original = '<p>Hello <strong>world</strong></p>'
        translated = '<p>你好 <strong>世界</strong></p>'

        valid, error = validate_html_with_context(original, translated)
        assert valid is True
        assert error == ""

    def test_unclosed_tag_returns_detailed_error(self):
        """未闭合标签应返回详细错误信息"""
        original = '<p>Hello <span>world</p>'
        translated = '<p>你好 <span>世界</p>'  # 缺少 </span>

        valid, error = validate_html_with_context(original, translated)
        assert valid is False
        assert "unclosed" in error.lower() or "span" in error.lower()

    def test_tag_mismatch_returns_detailed_error(self):
        """标签不匹配应返回详细错误信息"""
        original = '<p>Hello <span>world</span></p>'
        translated = '<p>你好 <span>世界</p></span>'  # 顺序错误

        valid, error = validate_html_with_context(original, translated)
        assert valid is False
        assert "标签不匹配" in error or "mismatch" in error.lower()

    def test_error_includes_original_snippet(self):
        """错误信息应包含原文片段"""
        original = '<p class="snippet-code">some code here</p>'
        translated = '<p class="snippet-code">一些代码</p>'

        valid, error = validate_html_with_context(original, translated)
        assert valid is True  # 这个例子是有效的
        assert error == ""


class TestValidateHtmlPairingIntegration:
    """测试 validate_html_pairing 和 validate_html_with_context 的一致性"""

    def test_valid_html_both_agree(self):
        """有效 HTML 两个函数应该一致"""
        original = '<p>Hello</p>'
        translated = '<p>你好</p>'

        valid_simple, _ = validate_html_pairing(original, translated)
        valid_detailed, _ = validate_html_with_context(original, translated)

        assert valid_simple == valid_detailed == True

    def test_invalid_html_both_detect(self):
        """无效 HTML 两个函数都应该检测到"""
        original = '<p>Hello</p>'
        translated = '<p>Hello'  # 缺少闭合标签

        valid_simple, _ = validate_html_pairing(original, translated)
        valid_detailed, _ = validate_html_with_context(original, translated)

        assert valid_simple is False
        assert valid_detailed is False
