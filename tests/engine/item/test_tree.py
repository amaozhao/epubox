
from engine.item.tree import (
    TreeNode,
    decode_entities,
    encode_entities,
    find_by_xpath,
    parse_html,
)


class TestTreeNode:
    def test_dataclass_fields(self):
        """TreeNode has all required fields."""
        node = TreeNode(
            tag="p",
            attributes={"class": "foo"},
            children=[],
            parent=None,
            text="hello",
        )
        assert node.tag == "p"
        assert node.attributes == {"class": "foo"}
        assert node.children == []
        assert node.parent is None
        assert node.text == "hello"

    def test_is_text_node(self):
        node = TreeNode(tag="", attributes={}, children=[], parent=None, text="hello")
        assert node.is_text_node() is True

        elem = TreeNode(tag="p", attributes={}, children=[], parent=None, text="")
        assert elem.is_text_node() is False

    def test_is_element_node(self):
        elem = TreeNode(tag="p", attributes={}, children=[], parent=None, text="")
        assert elem.is_element_node() is True

        text = TreeNode(tag="", attributes={}, children=[], parent=None, text="hello")
        assert text.is_element_node() is False

    def test_is_self_closing(self):
        assert TreeNode(tag="br", attributes={}, children=[], parent=None, text="").is_self_closing() is True
        assert TreeNode(tag="img", attributes={}, children=[], parent=None, text="").is_self_closing() is True
        assert TreeNode(tag="p", attributes={}, children=[], parent=None, text="").is_self_closing() is False
        assert TreeNode(tag="", attributes={}, children=[], parent=None, text="").is_self_closing() is False

    def test_collect_text_nodes(self):
        root = TreeNode(tag="div", attributes={}, children=[], parent=None, text="")
        c1 = TreeNode(tag="p", attributes={}, children=[], parent=root, text="")
        t1 = TreeNode(tag="", attributes={}, children=[], parent=c1, text="hello")
        c2 = TreeNode(tag="p", attributes={}, children=[], parent=root, text="")
        t2 = TreeNode(tag="", attributes={}, children=[], parent=c2, text="world")
        c1.children.append(t1)
        c2.children.append(t2)
        root.children.extend([c1, c2])

        texts = root._collect_text_nodes()
        assert [n.text for n in texts] == ["hello", "world"]


class TestParseHtml:
    def test_simple_element(self):
        root = parse_html("<p>hello</p>")
        assert root.tag == "div"  # wrapper
        assert len(root.children) == 1
        p = root.children[0]
        assert p.tag == "p"
        assert p.children[0].text == "hello"

    def test_nested_elements(self):
        root = parse_html("<div><p><span>nested</span></p></div>")
        div = root.children[0]
        p = div.children[0]
        span = p.children[0]
        assert span.tag == "span"
        assert span.children[0].text == "nested"

    def test_self_closing_tag(self):
        root = parse_html("<p>line1<br>line2</p>")
        p = root.children[0]
        br = p.children[1]
        assert br.tag == "br"
        assert br.is_self_closing() is True

    def test_attributes(self):
        root = parse_html('<p class="foo" id="bar">text</p>')
        p = root.children[0]
        assert p.tag == "p"
        assert p.attributes["class"] == "foo"
        assert p.attributes["id"] == "bar"

    def test_plain_text(self):
        root = parse_html("just text")
        assert root.tag == "div"
        assert len(root.children) == 1
        assert root.children[0].text == "just text"

    def test_multiple_top_level(self):
        root = parse_html("<p>one</p><p>two</p>")
        assert root.tag == "div"
        assert len(root.children) == 2
        assert root.children[0].children[0].text == "one"
        assert root.children[1].children[0].text == "two"

    def test_parent_reference(self):
        root = parse_html("<div><p>text</p></div>")
        div = root.children[0]
        p = div.children[0]
        assert p.parent is div
        assert p.children[0].parent is p

    def test_entity_in_html(self):
        root = parse_html("<p>Tom &amp; Jerry</p>")
        # html.parser decodes &amp; -> '&' in handle_data by default.
        # encode_entities() / decode_entities() handle round-tripping.
        p = root.children[0]
        text_node = p.children[0]
        assert text_node.text == "Tom & Jerry"
        assert decode_entities(text_node.text) == "Tom & Jerry"
        assert encode_entities(text_node.text) == "Tom &amp; Jerry"


class TestToHtml:
    def test_simple_element(self):
        root = parse_html("<p>hello</p>")
        html = root.to_html()
        assert "<p>hello</p>" in html

    def test_nested(self):
        root = parse_html("<div><p><span>text</span></p></div>")
        html = root.to_html()
        assert "<div>" in html
        assert "<p>" in html
        assert "<span>text</span>" in html

    def test_attributes_preserved(self):
        root = parse_html('<p class="foo" id="bar">text</p>')
        html = root.to_html()
        assert 'class="foo"' in html
        assert 'id="bar"' in html

    def test_self_closing_tag(self):
        root = parse_html("<p>line<br>end</p>")
        html = root.to_html()
        # Self-closing tags should use XHTML style <tag />
        assert "<br />" in html

    def test_entity_encoding_in_output(self):
        root = parse_html("<p>a &lt; b</p>")
        html = root.to_html()
        # to_html encodes for safe HTML output
        assert "&lt;" in html


class TestEntities:
    def test_encode_basic(self):
        assert encode_entities("a < b & c > d") == "a &lt; b &amp; c &gt; d"
        assert encode_entities("\"quotes\"") == "&quot;quotes&quot;"

    def test_encode_no_change(self):
        assert encode_entities("plain text") == "plain text"

    def test_decode_basic(self):
        assert decode_entities("a &lt; b &amp; c &gt; d") == "a < b & c > d"
        assert decode_entities("&quot;quoted&quot;") == '"quoted"'

    def test_decode_no_change(self):
        assert decode_entities("plain text") == "plain text"


class TestFindByXpath:
    def test_absolute_path(self):
        root = parse_html("<html><body><p><span>target</span></p></body></html>")
        span = find_by_xpath(root, "/html/body/p/span")
        assert span is not None
        assert span.tag == "span"
        assert span.children[0].text == "target"

    def test_first_child(self):
        root = parse_html("<html><body><p>p1</p><p>p2</p></body></html>")
        p = find_by_xpath(root, "/html/body/p[1]")
        assert p is not None
        assert p.tag == "p"

    def test_second_child(self):
        root = parse_html("<html><body><p>p1</p><p>p2</p></body></html>")
        p = find_by_xpath(root, "/html/body/p[2]")
        assert p is not None
        assert p.tag == "p"

    def test_not_found(self):
        root = parse_html("<html><body><p>text</p></body></html>")
        assert find_by_xpath(root, "/html/body/div") is None

    def test_invalid_xpath(self):
        root = parse_html("<div><p>text</p></div>")
        assert find_by_xpath(root, "//div") is None  # relative not supported

    def test_no_index_means_first(self):
        root = parse_html("<html><body><p>p1</p><p>p2</p></body></html>")
        p = find_by_xpath(root, "/html/body/p")
        assert p is not None
        assert p.tag == "p"
        # Without index, returns first match


class TestXmlDeclarationAndDoctype:
    """Test preservation of XML declaration and DOCTYPE."""

    def test_xml_declaration_preserved(self):
        """XML declaration should be preserved in to_html output."""
        original = '<?xml version="1.0" encoding="UTF-8" standalone="no"?><html><body><p>test</p></body></html>'
        root = parse_html(original)
        result = root.to_html()
        # XML declaration is preserved (may have newline after)
        assert '<?xml version="1.0" encoding="UTF-8" standalone="no"?>' in result

    def test_doctype_preserved(self):
        """DOCTYPE should be preserved in to_html output."""
        original = '<!DOCTYPE html><html><body><p>test</p></body></html>'
        root = parse_html(original)
        result = root.to_html()
        assert '<!DOCTYPE html>' in result

    def test_xml_and_doctype_together(self):
        """Both XML declaration and DOCTYPE should be preserved."""
        original = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE html>
<html><body><p>test</p></body></html>'''
        root = parse_html(original)
        result = root.to_html()
        assert '<?xml' in result
        assert '<!DOCTYPE html>' in result


class TestSelfClosingTags:
    """Test self-closing tags are properly handled."""

    def test_link_self_closing(self):
        """<link /> should be preserved as self-closing."""
        original = '<html><head><link href="style.css" rel="stylesheet"/></head><body><p>test</p></body></html>'
        root = parse_html(original)
        result = root.to_html()
        assert '<link' in result
        assert '/>' in result or '/ >' in result

    def test_br_self_closing(self):
        """<br/> should be preserved as self-closing."""
        original = '<p>line1<br/>line2</p>'
        root = parse_html(original)
        result = root.to_html()
        assert '<br />' in result

    def test_img_self_closing(self):
        """<img /> should be preserved as self-closing."""
        original = '<p><img src="test.png" alt="test"/></p>'
        root = parse_html(original)
        result = root.to_html()
        assert '<img' in result
        assert '/>' in result or '/ >' in result

    def test_hr_self_closing(self):
        """<hr/> should be preserved as self-closing."""
        original = '<p>before<hr/>after</p>'
        root = parse_html(original)
        result = root.to_html()
        assert '<hr />' in result

    def test_meta_self_closing(self):
        """<meta /> should be preserved as self-closing."""
        original = '<html><head><meta charset="utf-8"/></head><body><p>test</p></body></html>'
        root = parse_html(original)
        result = root.to_html()
        assert '<meta' in result
        assert '/>' in result or '/ >' in result

    def test_multiple_self_closing_tags(self):
        """Multiple different self-closing tags should all be preserved."""
        original = '''<html>
<head>
<link href="a.css" rel="stylesheet"/>
<meta name="keywords" content="test"/>
</head>
<body>
<img src="a.png" alt="a"/>
<hr/>
<p>text<br/></p>
</body>
</html>'''
        root = parse_html(original)
        result = root.to_html()
        assert '<link' in result and '/>' in result
        assert '<meta' in result and '/>' in result
        assert '<img' in result and '/>' in result
        assert '<hr />' in result
        assert '<br />' in result


class TestTagOrderPreservation:
    """Test that tag order is preserved through parse/serialize cycle."""

    def test_head_body_order_preserved(self):
        """<head> should come before <body> in serialized output."""
        original = '<html><head><title>Test</title></head><body><p>Hello</p></body></html>'
        root = parse_html(original)
        result = root.to_html()
        head_pos = result.find('<head>')
        body_pos = result.find('<body>')
        assert head_pos < body_pos, "head should come before body"

    def test_nested_tag_order_preserved(self):
        """Nested tag order should be preserved."""
        original = '<div><p><span>text</span></p></div>'
        root = parse_html(original)
        result = root.to_html()
        assert result.find('<p>') < result.find('</p>')
        assert result.find('<span>') < result.find('</span>')
        assert result.find('<div>') < result.find('<p>')
        assert result.find('</p>') < result.find('</div>')
