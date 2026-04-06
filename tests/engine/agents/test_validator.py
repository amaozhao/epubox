import pytest

from engine.agents.validator import validate_placeholders


class TestValidatePlaceholders:
    def test_validate_empty_tag_map(self):
        """测试空tag_map直接通过"""
        assert validate_placeholders("Hello World", {}) == (True, "")

    def test_validate_mismatch(self):
        """测试占位符不匹配"""
        text = "[id0]Hello[id1] World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "缺少" in msg

    def test_validate_out_of_order(self):
        """测试顺序验证 - 顺序错误"""
        text = "[id0]A[id2]B[id1]C[id3]D"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>", "[id3]": "</span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "不匹配" in msg

    def test_validate_missing_placeholder(self):
        """测试缺失占位符"""
        text = "[id0]Hello World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "缺少" in msg

    def test_validate_success(self):
        """测试验证成功"""
        text = "[id0]Hello[id1] World[id2]"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is True
        assert msg == ""
