
from engine.item.nav import (
    NavParser,
    is_nav_file,
    extract_nav_points,
    preserve_content_attrs,
    rebuild_nav,
)


class TestIsNavFile:
    """测试 is_nav_file 函数"""

    def test_ncx_format(self):
        """测试 NCX 格式识别"""
        html = '<?xml version="1.0" encoding="UTF-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">'
        assert is_nav_file(html) is True

    def test_xhtml_nav_format(self):
        """测试 XHTML nav 格式识别（standalone nav.xhtml）"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol><li>Chapter 1</li></ol></nav>'
        assert is_nav_file(html) is True

    def test_html_body_nav_format(self):
        """测试 XHTML nav 格式识别（html>body>nav）"""
        html = '<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol><li>Chapter 1</li></ol></nav></body></html>'
        assert is_nav_file(html) is True

    def test_regular_html_not_nav(self):
        """测试普通 HTML 不是 nav 文件"""
        html = "<html><body><p>Hello</p></body></html>"
        assert is_nav_file(html) is False

    def test_plain_text_not_nav(self):
        """测试纯文本不是 nav 文件"""
        assert is_nav_file("Just plain text") is False
        assert is_nav_file("") is False

    def test_partial_nav_element_no_structure(self):
        """测试有 nav 标签但没有 ol>li 结构"""
        html = "<nav>No list structure</nav>"
        assert is_nav_file(html) is False


class TestExtractNavPoints:
    """测试 extract_nav_points 函数"""

    def test_ncx_single_navpoint(self):
        """测试 NCX 格式单个 navPoint"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        points = extract_nav_points(html)
        assert len(points) == 1
        assert points[0]["text"] == "Chapter 1"
        assert points[0]["index"] == 0

    def test_ncx_multiple_navpoints(self):
        """测试 NCX 格式多个 navPoint"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
    <navPoint id="np2">
      <navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        points = extract_nav_points(html)
        assert len(points) == 2
        assert points[0]["text"] == "Chapter 1"
        assert points[1]["text"] == "Chapter 2"

    def test_xhtml_nav_single_item(self):
        """测试 XHTML nav 格式单个条目"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol><li><a href="ch1.xhtml">Introduction</a></li></ol></nav>'
        points = extract_nav_points(html)
        assert len(points) == 1
        assert points[0]["text"] == "Introduction"

    def test_xhtml_nav_multiple_items(self):
        """测试 XHTML nav 格式多个条目"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">Chapter One</a></li>
    <li><a href="ch2.xhtml">Chapter Two</a></li>
    <li><a href="ch3.xhtml">Chapter Three</a></li>
  </ol>
</nav>'''
        points = extract_nav_points(html)
        assert len(points) == 3
        assert [p["text"] for p in points] == ["Chapter One", "Chapter Two", "Chapter Three"]

    def test_nested_nav_items(self):
        """测试嵌套的 nav 条目"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">Part 1</a>
      <ol>
        <li><a href="ch1.xhtml#sec1">Section 1</a></li>
        <li><a href="ch1.xhtml#sec2">Section 2</a></li>
      </ol>
    </li>
  </ol>
</nav>'''
        points = extract_nav_points(html)
        # 嵌套的 li>a 文本节点也应该被提取
        assert len(points) == 3
        assert [p["text"] for p in points] == ["Part 1", "Section 1", "Section 2"]

    def test_html_body_nav_format(self):
        """测试 html>body>nav 格式"""
        html = '''<html xmlns="http://www.w3.org/1999/xhtml">
<body>
  <nav><ol>
    <li><a href="intro.xhtml">Preface</a></li>
  </ol></nav>
</body>
</html>'''
        points = extract_nav_points(html)
        assert len(points) == 1
        assert points[0]["text"] == "Preface"

    def test_empty_nav(self):
        """测试空的 nav 结构"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol></ol></nav>'
        points = extract_nav_points(html)
        assert points == []

    def test_navpoint_without_text(self):
        """测试 navPoint 没有 text 节点"""
        html = '''<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Title Text</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        points = extract_nav_points(html)
        assert len(points) == 1
        assert points[0]["text"] == "Title Text"


class TestPreserveContentAttrs:
    """测试 preserve_content_attrs 函数"""

    def test_ncx_preserves_text_nodes(self):
        """测试 NCX 格式保留文本节点"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
    <navPoint id="np2">
      <navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        attrs = preserve_content_attrs(html)
        assert len(attrs) == 2
        assert attrs[0] == "Chapter 1"
        assert attrs[1] == "Chapter 2"

    def test_xhtml_nav_preserves_text_nodes(self):
        """测试 XHTML nav 格式保留文本节点"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">First</a></li>
    <li><a href="ch2.xhtml">Second</a></li>
  </ol>
</nav>'''
        attrs = preserve_content_attrs(html)
        assert len(attrs) == 2
        assert attrs[0] == "First"
        assert attrs[1] == "Second"

    def test_html_body_nav_preserves_text_nodes(self):
        """测试 html>body>nav 格式保留文本节点"""
        html = '''<html xmlns="http://www.w3.org/1999/xhtml">
<body>
  <nav><ol>
    <li><a href="intro.xhtml">Preface</a></li>
  </ol></nav>
</body>
</html>'''
        attrs = preserve_content_attrs(html)
        assert len(attrs) == 1
        assert attrs[0] == "Preface"

    def test_empty_returns_empty_dict(self):
        """测试空 nav 返回空字典"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol></ol></nav>'
        attrs = preserve_content_attrs(html)
        assert attrs == {}

    def test_nested_nav_items(self):
        """测试嵌套 nav 条目的文本节点保留"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">Part 1</a>
      <ol>
        <li><a href="ch1.xhtml#sec1">Section 1</a></li>
      </ol>
    </li>
  </ol>
</nav>'''
        attrs = preserve_content_attrs(html)
        assert len(attrs) == 2
        assert attrs[0] == "Part 1"
        assert attrs[1] == "Section 1"


class TestRebuildNav:
    """测试 rebuild_nav 函数"""

    def test_ncx_rebuild_with_translations(self):
        """测试 NCX 格式重建"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
    <navPoint id="np2">
      <navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        translations = {0: "第一章", 1: "第二章"}
        result = rebuild_nav(html, translations)
        assert "第一章" in result
        assert "第二章" in result
        assert "Chapter 1" not in result
        assert "Chapter 2" not in result

    def test_xhtml_nav_rebuild_with_translations(self):
        """测试 XHTML nav 格式重建"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">First</a></li>
    <li><a href="ch2.xhtml">Second</a></li>
  </ol>
</nav>'''
        translations = {0: "第一", 1: "第二"}
        result = rebuild_nav(html, translations)
        assert "第一" in result
        assert "第二" in result
        assert "First" not in result
        assert "Second" not in result

    def test_html_body_nav_rebuild(self):
        """测试 html>body>nav 格式重建"""
        html = '''<html xmlns="http://www.w3.org/1999/xhtml">
<body>
  <nav><ol>
    <li><a href="intro.xhtml">Preface</a></li>
  </ol></nav>
</body>
</html>'''
        translations = {0: "序言"}
        result = rebuild_nav(html, translations)
        assert "序言" in result
        assert "Preface" not in result

    def test_partial_translation(self):
        """测试部分翻译"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">First</a></li>
    <li><a href="ch2.xhtml">Second</a></li>
  </ol>
</nav>'''
        translations = {0: "第一"}
        result = rebuild_nav(html, translations)
        assert "第一" in result
        assert "Second" in result  # 未翻译的保持原样
        assert "First" not in result

    def test_empty_translations(self):
        """测试空翻译字典"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol><li><a href="ch.xhtml">Original</a></li></ol></nav>'
        result = rebuild_nav(html, {})
        assert "Original" in result

    def test_ncx_preserves_structure(self):
        """测试 NCX 重建后结构完整"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''
        translations = {0: "第一章"}
        result = rebuild_nav(html, translations)
        assert "<ncx" in result
        assert "<navMap>" in result or "<navMap" in result
        assert "<navPoint" in result
        assert '<content src="chapter1.xhtml"/>' in result

    def test_xhtml_nav_preserves_structure(self):
        """测试 XHTML nav 重建后结构完整"""
        html = '<nav xmlns="http://www.w3.org/1999/xhtml"><ol><li><a href="ch.xhtml">Title</a></li></ol></nav>'
        translations = {0: "标题"}
        result = rebuild_nav(html, translations)
        assert "<nav" in result
        assert "<ol>" in result
        assert '<a href="ch.xhtml">' in result


class TestNavParserClass:
    """测试 NavParser 类的完整流程"""

    def test_full_flow_ncx(self):
        """测试 NCX 完整流程：提取 -> 翻译 -> 重建"""
        html = '''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <navMap>
    <navPoint id="np1">
      <navLabel><text>Chapter 1</text></navLabel>
      <content src="chapter1.xhtml"/>
    </navPoint>
    <navPoint id="np2">
      <navLabel><text>Chapter 2</text></navLabel>
      <content src="chapter2.xhtml"/>
    </navPoint>
  </navMap>
</ncx>'''

        parser = NavParser()
        assert parser.is_nav_file(html) is True

        points = parser.extract_nav_points(html)
        assert len(points) == 2

        attrs = parser.preserve_content_attrs(html)
        assert len(attrs) == 2
        assert attrs[0] == "Chapter 1"
        assert attrs[1] == "Chapter 2"

        translations = {0: "第一章", 1: "第二章"}
        result = parser.rebuild_nav(html, translations)
        assert "第一章" in result
        assert "第二章" in result
        assert "Chapter 1" not in result
        assert "Chapter 2" not in result

    def test_full_flow_xhtml_nav(self):
        """测试 XHTML nav 完整流程"""
        html = '''<nav xmlns="http://www.w3.org/1999/xhtml">
  <ol>
    <li><a href="ch1.xhtml">First Chapter</a></li>
    <li><a href="ch2.xhtml">Second Chapter</a></li>
  </ol>
</nav>'''

        parser = NavParser()
        assert parser.is_nav_file(html) is True

        points = parser.extract_nav_points(html)
        assert len(points) == 2

        attrs = parser.preserve_content_attrs(html)
        assert len(attrs) == 2
        assert attrs[0] == "First Chapter"
        assert attrs[1] == "Second Chapter"

        translations = {0: "第一章", 1: "第二章"}
        result = parser.rebuild_nav(html, translations)
        assert "第一章" in result
        assert "第二章" in result

    def test_non_nav_file(self):
        """测试非 nav 文件"""
        html = "<html><body><p>Hello World</p></body></html>"
        parser = NavParser()
        assert parser.is_nav_file(html) is False
