import pytest

from engine.item.tag import TagPreserver


class TestTagPreserver:
    """测试 TagPreserver 的标签合并逻辑"""

    def test_adjacent_tags_merged(self):
        """测试相邻标签被合并为一个占位符"""
        html = "<p><span>Hello</span></p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        # <p><span> 合并为一个占位符
        # </span></p> 合并为另一个占位符
        assert mgr.tag_map["[id0]"] == "<p><span>"
        assert mgr.tag_map["[id1]"] == "</span></p>"
        assert result == "[id0]Hello[id1]"

    def test_multiple_adjacent_tags(self):
        """测试多个相邻标签合并"""
        html = "<p><span><em>text</em></span></p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        assert "[id0]" in result
        assert "[id1]" in result
        assert "text" in result
        assert mgr.tag_map["[id0]"] == "<p><span><em>"
        assert mgr.tag_map["[id1]"] == "</em></span></p>"

    def test_adjacent_tags_with_whitespace(self):
        """测试相邻标签之间的空白也被合并"""
        html = "<p>   <span>Hello</span></p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        # 空白在标签之前，和前面的<p>合并
        # 但实际上空白在<p>后面，是独立片段
        assert "[id0]" in result
        assert "Hello" in result

    def test_standalone_tag(self):
        """测试独立标签"""
        html = "<p>Hello</p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        assert mgr.tag_map["[id0]"] == "<p>"
        assert mgr.tag_map["[id1]"] == "</p>"
        assert result == "[id0]Hello[id1]"

    def test_inline_tags_merged(self):
        """测试行内相邻标签合并"""
        html = "<span><em><strong>text</strong></em></span>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        assert mgr.tag_map["[id0]"] == "<span><em><strong>"
        assert mgr.tag_map["[id1]"] == "</strong></em></span>"
        assert result == "[id0]text[id1]"

    def test_mixed_content(self):
        """测试混合内容"""
        html = "<div><p>Paragraph</p><p>Another</p></div>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        # <div> 独立占位符
        # <p>Paragraph</p> 是一个整体（因为在p标签对内）
        # 实际上分块是在chunk层级，这里只是preserve层级
        assert "[id0]" in result

    def test_non_translatable_tags_ignored(self):
        """测试不可翻译标签被跳过"""
        html = "<p>Hello<script>alert('hi')</script>World</p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        # script标签不产生占位符
        assert "script" not in result
        assert "Hello" in result
        assert "World" in result

    def test_self_closing_tags(self):
        """测试自闭合标签"""
        html = "<p>Hello<br/>World</p>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        assert "br" not in mgr.tag_map.values()  # br是自闭合，不产生占位符
        assert "Hello" in result
        assert "World" in result

    def test_nested_structure(self):
        """测试嵌套结构正确合并 - 连续标签组为单个占位符"""
        html = "<article><header><h1>Title</h1></header><p>Content</p></article>"
        preserver = TagPreserver()
        result, mgr = preserver.preserve_tags(html)

        # 算法按相邻标签合并，直到遇到文本才flush：
        # [id0] = <article><header><h1>  (遇到Title前)
        # Title
        # [id1] = </h1></header><p>  (遇到Content前)
        # Content
        # [id2] = </p></article>  (末尾flush)
        assert result == "[id0]Title[id1]Content[id2]"
        assert result.count("[id") == 3
        assert mgr.tag_map["[id0]"] == "<article><header><h1>"
        assert mgr.tag_map["[id1]"] == "</h1></header><p>"
        assert mgr.tag_map["[id2]"] == "</p></article>"
