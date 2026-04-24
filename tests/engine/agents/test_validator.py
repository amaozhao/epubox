from engine.agents.validator import (
    extract_placeholder_indices,
    validate_placeholder_positions,
    validate_placeholders,
)


class TestValidatePlaceholders:
    def test_validate_empty_tag_map(self):
        """测试空tag_map直接通过"""
        assert validate_placeholders("Hello World", {}) == (True, "")

    def test_validate_matching_count(self):
        """测试数量验证失败"""
        text = "[id0]Hello[id1] World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "缺少" in msg

    def test_validate_out_of_order(self):
        """测试顺序验证 - 数量匹配但顺序错误"""
        # 4 placeholders with same count but wrong order
        text = "[id0]A[id2]B[id1]C[id3]D"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>", "[id3]": "</span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "顺序" in msg

    def test_validate_missing_placeholder(self):
        """测试缺失占位符 - 数量检查先于缺失检查"""
        text = "[id0]Hello World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is False
        assert "缺少" in msg  # 数量不匹配先触发

    def test_validate_success(self):
        """测试验证成功"""
        text = "[id0]Hello[id1] World[id2]"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is True
        assert msg == ""


class TestValidatePlaceholderPositions:
    def test_position_mismatch_count(self):
        """测试位置数量不匹配"""
        original = "[id0]Hello[id1]"
        translated = "[id0]Bonjour"
        local_tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        valid, _, errors = validate_placeholder_positions(original, translated, local_tag_map)
        assert valid is False
        assert len(errors) > 0

    def test_position_index_mismatch(self):
        """测试位置索引不匹配"""
        original = "[id0]Hello[id1]"
        translated = "[id1]Bonjour[id0]"
        local_tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        valid, _, errors = validate_placeholder_positions(original, translated, local_tag_map)
        assert valid is False
        assert any("索引不匹配" in e for e in errors)

    def test_position_success(self):
        """测试位置验证成功"""
        original = "[id0]Hello[id1]"
        translated = "[id0]Bonjour[id1]"
        local_tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        valid, _, errors = validate_placeholder_positions(original, translated, local_tag_map)
        assert valid is True
        assert errors == []


class TestExtractPlaceholderIndices:
    def test_extract_basic(self):
        """测试基本提取"""
        text = "[id0]Hello[id1][id2]"
        indices = extract_placeholder_indices(text)
        assert indices == [0, 1, 2]

    def test_extract_no_placeholders(self):
        """测试无占位符"""
        text = "Hello World"
        indices = extract_placeholder_indices(text)
        assert indices == []

    def test_extract_out_of_order(self):
        """测试乱序提取"""
        text = "[id5]Hello[id2][id10]"
        indices = extract_placeholder_indices(text)
        assert indices == [5, 2, 10]
