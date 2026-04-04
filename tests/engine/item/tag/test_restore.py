import pytest

from engine.item.placeholder import PlaceholderManager
from engine.item.tag import TagRestorer


class TestTagRestorer:
    """测试 TagRestorer 的标签恢复逻辑"""

    def test_restore_basic(self):
        """测试基本恢复"""
        translated = "[id0]你好[id1]"
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        mgr.counter = 2

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "<p>你好</p>"

    def test_restore_merged_tags(self):
        """测试恢复合并的标签"""
        translated = "[id0]Title[id1]Content[id2]"
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<article><header><h1>",
            "[id1]": "</h1></header><p>",
            "[id2]": "</p></article>"
        }
        mgr.counter = 3

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "<article><header><h1>Title</h1></header><p>Content</p></article>"

    def test_restore_reverse_order(self):
        """测试按逆序恢复避免替换冲突"""
        # 如果先替换[id0]，可能影响[id1]的位置
        translated = "[id1]Bonjour[id0]"
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        mgr.counter = 2

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "</p>Bonjour<p>"

    def test_restore_all_merged(self):
        """测试完全合并的标签恢复"""
        translated = "[id0]Hello World[id1]"
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<div><p><span>", "[id1]": "</span></p></div>"}
        mgr.counter = 2

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "<div><p><span>Hello World</span></p></div>"

    def test_restore_empty_placeholder(self):
        """测试空占位符映射"""
        translated = "Plain text"
        mgr = PlaceholderManager()
        mgr.tag_map = {}
        mgr.counter = 0

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "Plain text"

    def test_restore_multiple_placeholders(self):
        """测试多个占位符顺序恢复"""
        translated = "[id0]First[id1]Second[id2]Third[id3]"
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p><p>",
            "[id2]": "</p><p>",
            "[id3]": "</p>"
        }
        mgr.counter = 4

        restorer = TagRestorer()
        result = restorer.restore_tags(translated, mgr.tag_map)

        assert result == "<p>First</p><p>Second</p><p>Third</p>"
