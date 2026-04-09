import pytest

from engine.agents.verifier import validate_translated_html


class TestValidateTranslatedHtmlTagErrors:
    def test_validate_catches_tag_crossing(self):
        """测试能检测同类型标签交叉（BeautifulSoup 会自动修复的情况）"""
        original = "<p><strong>Hello</strong></p>"
        translated = "<p><strong>你好</p></strong>"  # 错误：strong 和 p 交叉
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "标签" in error or "交错" in error

    def test_validate_catches_unclosed_tags(self):
        """测试能检测未闭合标签"""
        original = "<p>A</p><p>B</p>"
        translated = "<p>甲<p>乙</p>"  # 错误：第一个 p 未闭合
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid

    def test_validate_accepts_correct_html(self):
        """测试正确的 HTML 能通过验证"""
        original = "<p><strong>Hello</strong></p>"
        translated = "<p><strong>你好</strong></p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid
