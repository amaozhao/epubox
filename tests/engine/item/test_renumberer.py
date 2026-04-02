import pytest

from engine.item.renumberer import Renumberer


class TestRenumberer:
    def test_renumber_basic(self):
        """测试基本重编号"""
        renumberer = Renumberer()
        text = "[id0]Hello[id1][id2]"
        tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
            "[id2]": "<span>",
        }
        result = renumberer.renumber(text, tag_map)
        assert result["text"] == "[id0]Hello[id1][id2]"
        assert result["indices"] == [0, 1, 2]
        assert "[id0]" in result["tag_map"]

    def test_renumber_with_gaps(self):
        """测试有间隔的索引"""
        renumberer = Renumberer()
        text = "[id5]Hello[id10][id15]"
        tag_map = {
            "[id5]": "<p>",
            "[id10]": "</p>",
            "[id15]": "<span>",
        }
        result = renumberer.renumber(text, tag_map)
        assert result["indices"] == [5, 10, 15]
        assert result["tag_map"]["[id0]"] == "<p>"

    def test_renumber_empty(self):
        """测试空文本"""
        renumberer = Renumberer()
        result = renumberer.renumber("", {})
        assert result["text"] == ""
        assert result["indices"] == []
        assert result["tag_map"] == {}

    def test_renumber_single_placeholder(self):
        """测试单个占位符"""
        renumberer = Renumberer()
        text = "[id0]Hello"
        tag_map = {"[id0]": "<p>"}
        result = renumberer.renumber(text, tag_map)
        assert result["text"] == "[id0]Hello"
        assert result["indices"] == [0]
        assert result["tag_map"]["[id0]"] == "<p>"
