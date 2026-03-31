import pytest

from engine.agents.aligner import (
    _adjust_boundary,
    _align,
    _find_positions,
    _insert,
    _remove_placeholders,
)


class TestAligner:
    def test_find_positions(self):
        """测试占位符位置查找"""
        text = "[id0]Hello[id1] World[id2]"
        positions = _find_positions(text, ["[id0]", "[id1]", "[id2]"])
        assert len(positions) == 3
        assert positions[0] == (0, 5, "[id0]")  # [id0] is 5 chars
        assert positions[1] == (10, 15, "[id1]")  # Hello = 5 chars, so 5+5=10, 10+5=15
        assert positions[2] == (21, 26, "[id2]")  # " World" = 6 chars

    def test_find_positions_partial(self):
        """测试部分占位符匹配"""
        text = "[id0]Hello[id1]"
        positions = _find_positions(text, ["[id0]", "[id1]", "[id2]"])
        assert len(positions) == 2

    def test_remove_placeholders(self):
        """测试移除占位符"""
        text = "[id0]Hello[id1] World[id2]"
        result = _remove_placeholders(text, ["[id0]", "[id1]", "[id2]"])
        assert result == "Hello World"

    def test_remove_placeholders_empty(self):
        """测试移除空文本中的占位符"""
        result = _remove_placeholders("", ["[id0]"])
        assert result == ""

    def test_adjust_boundary_word(self):
        """测试词边界校正"""
        text = "Hello World"
        # 位置在 "World" 的 W 上 (pos=6), 需要校正到最近的词边界
        pos = 6
        adjusted = _adjust_boundary(text, pos)
        # 最近边界是左边的空格 (pos=5)
        assert adjusted == 5

    def test_adjust_boundary_middle(self):
        """测试在单词中间的校正"""
        text = "Hello World"
        # 位置在 "orl" 中的 r 上
        pos = 8
        adjusted = _adjust_boundary(text, pos)
        # 应该校正到词边界
        assert text[adjusted] in " \t\n\r.,;:!?，" or adjusted == 0 or adjusted == len(text)

    def test_adjust_boundary_edge_cases(self):
        """测试边界情况"""
        text = "Hello"
        assert _adjust_boundary(text, 0) == 0
        assert _adjust_boundary(text, -1) == 0
        assert _adjust_boundary(text, 100) == len(text)

    def test_insert(self):
        """测试占位符插入"""
        text = "Hello World"
        insertions = [(6, "[id0]")]
        result = _insert(text, insertions)
        assert result == "Hello [id0]World"

    def test_insert_multiple_at_same_position(self):
        """测试在同一位置插入多个占位符"""
        text = "Hello World"
        insertions = [(6, "[id0]"), (6, "[id1]")]
        result = _insert(text, insertions)
        # 按索引排序后插入
        assert "[id0]" in result
        assert "[id1]" in result

    def test_insert_reverse_order(self):
        """测试逆序插入"""
        text = "Hello World"
        insertions = [(6, "[id1]"), (0, "[id0]")]
        result = _insert(text, insertions)
        assert result.startswith("[id0]")

    def test_align_basic(self):
        """测试基本对齐"""
        original = "[id0]Hello[id1]"
        translated = "Bonjour"
        placeholders = ["[id0]", "[id1]"]
        result = _align(original, translated, placeholders)
        assert "[id0]" in result
        assert "[id1]" in result
