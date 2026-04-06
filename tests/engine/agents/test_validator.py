
from engine.agents.validator import validate_html_pairing, validate_placeholders


class TestValidatePlaceholders:
    """Test the legacy validate_placeholders function (always returns True now)"""

    def test_validate_empty_tag_map(self):
        """测试空tag_map直接通过"""
        assert validate_placeholders("Hello World", {}) == (True, "")

    def test_validate_always_passes(self):
        """Legacy function always returns True since we no longer use placeholders"""
        text = "[id0]Hello[id1] World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is True
        assert msg == ""


class TestValidateHtmlPairing:
    """Test the new HTML tag pairing validation"""

    def test_validate_identical_html(self):
        """HTML with same structure passes"""
        original = "<p>Hello</p>"
        translated = "<p>你好</p>"
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True
        assert msg == ""

    def test_validate_tag_mismatch(self):
        """HTML with different tag types now passes if translated HTML is structurally valid.

        Under the new architecture, we trust the LLM tried its best to preserve tags.
        We only verify that the translated HTML has valid structure, not that it
        exactly matches the original tag names.
        """
        original = "<p>Hello</p>"
        translated = "<div>Hello</div>"  # p tag changed to div - but structurally valid
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True  # New architecture: structurally valid = pass

    def test_validate_missing_close_tag(self):
        """HTML with missing closing tag fails"""
        original = "<p>Hello</p>"
        translated = "<p>Hello"  # missing closing tag
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_extra_tag(self):
        """HTML with extra closing tag fails"""
        original = "<p>Hello</p>"
        translated = "<p>Hello</p></div>"  # extra closing tag
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_nested_tags(self):
        """Nested HTML with proper structure passes"""
        original = "<div><p>Hello <strong>World</strong></p></div>"
        translated = "<div><p>你好 <strong>世界</strong></p></div>"
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True
