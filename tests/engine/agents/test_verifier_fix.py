
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

    def test_validate_catches_unescaped_ampersand(self):
        """测试能检测未转义的 & 字符（XML 格式错误）"""
        original = "<p>Tom & Jerry</p>"
        translated = "<p>汤姆 & 杰瑞</p>"  # 错误：& 未转义为 &amp;
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "XML 格式错误" in error

    def test_validate_accepts_html_entity_nbsp(self):
        """测试 &nbsp; 等 HTML 实体应通过验证（BS4 已解码，regex 不拦截合法实体）"""
        original = "<p>Hello</p>"
        translated = "<p>Hello&nbsp;World</p>"  # BS4 将 &nbsp; 解码为 \xa0
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, f"HTML 实体不应被误判: {error}"

    def test_validate_accepts_escaped_ampersand(self):
        """测试已转义的 &amp; 应通过验证"""
        original = "<p>Tom &amp; Jerry</p>"
        translated = "<p>汤姆 &amp; 杰瑞</p>"  # &amp; 已转义
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, f"已转义 &amp; 应通过: {error}"

    def test_validate_rejects_unicode_original_echo(self):
        """测试 unicode 原样回显也会被识别为未翻译"""
        original = "<p>你好世界</p>"
        translated = "<p>你好世界</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "疑似未翻译" in error

    def test_validate_accepts_symbol_only_noop(self):
        """测试只有数字和占位符的原样回显会作为合法 no-op 接受"""
        original = "<p>2024 [PRE:0] !!!</p>"
        translated = "<p>2024 [PRE:0] !!!</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid
        assert error == "accepted_as_is"

    def test_validate_accepts_technical_ascii_command_noop(self):
        """测试技术型 ASCII 命令原样回显会作为合法 no-op 接受"""
        original = "<p>python main.py translate book.epub --limit 1200 --language Chinese</p>"
        translated = "<p>python main.py translate book.epub --limit 1200 --language Chinese</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid
        assert error == "accepted_as_is"
