import pytest

from engine.item.placeholder import PlaceholderManager


class TestPlaceholderManager:
    def test_create_placeholder(self):
        """测试创建占位符"""
        mgr = PlaceholderManager()
        ph = mgr.create_placeholder("<p>")
        assert ph == "[id0]"
        assert mgr.tag_map["[id0]"] == "<p>"
        assert mgr.counter == 1

    def test_create_multiple_placeholders(self):
        """测试创建多个占位符"""
        mgr = PlaceholderManager()
        mgr.create_placeholder("<p>")
        mgr.create_placeholder("</p>")
        mgr.create_placeholder("<span>")
        assert mgr.counter == 3
        assert len(mgr.tag_map) == 3

    def test_get_local_tag_map(self):
        """测试获取局部tag_map - 映射到顺序索引"""
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
            "[id2]": "<span>",
            "[id3]": "</span>",
        }
        mgr.counter = 4
        # global_indices=[0,2] means: get 1st and 3rd items, remap to [id0],[id1]
        local_map = mgr.get_local_tag_map([0, 2])
        assert "[id0]" in local_map
        assert local_map["[id0]"] == "<p>"
        assert "[id1]" in local_map  # Remapped from [id2] to [id1]
        assert local_map["[id1]"] == "<span>"

    def test_restore_to_global_basic(self):
        """测试基本全局索引恢复"""
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
        }
        mgr.counter = 2
        text = "[id0]Hello[id1]"
        result = mgr.restore_to_global(text, [0, 1])
        assert result == "[id0]Hello[id1]"

    def test_restore_to_global_reordered(self):
        """测试乱序恢复"""
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
        }
        mgr.counter = 2
        text = "[id1]Bonjour[id0]"
        result = mgr.restore_to_global(text, [0, 1])
        assert "[id0]" in result
        assert "[id1]" in result

    def test_remove_all_placeholders(self):
        """测试移除所有占位符"""
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
        }
        mgr.counter = 2
        text = "[id0]Hello[id1] World"
        result = mgr.remove_all_placeholders(text)
        assert result == "Hello World"
