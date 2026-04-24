from engine.agents.verifier import get_tag_name, is_self_closing, verify_final_html, verify_html_integrity


class TestVerifyHtmlIntegrity:
    def test_well_formed_html(self):
        """测试正确闭合的HTML"""
        html = "<div><p>Hello</p><span>World</span></div>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is True
        assert errors == []

    def test_unclosed_tag(self):
        """测试未闭合标签"""
        html = "<div><p>Hello</div>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is False
        assert len(errors) > 0

    def test_mismatched_tags(self):
        """测试交错标签"""
        html = "<div><p></div></p>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is False
        assert len(errors) > 0

    def test_self_closing_tags(self):
        """测试自闭合标签"""
        html = "<div><br/><img src='x'/><hr></div>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is True
        assert errors == []

    def test_empty_string(self):
        """测试空字符串"""
        is_valid, errors = verify_html_integrity("")
        assert is_valid is True
        assert errors == []

    def test_comment_tag(self):
        """测试注释标签"""
        html = "<div><!-- comment --><p>Hello</p></div>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is True
        assert errors == []

    def test_doctype_tag(self):
        """测试DOCTYPE标签"""
        html = "<!DOCTYPE html><div><p>Hello</p></div>"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is True
        assert errors == []

    def test_unclosed_bracket(self):
        """测试未闭合括号"""
        html = "<div><p>Hello"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is False
        assert len(errors) > 0


class TestIsSelfClosing:
    def test_common_self_closing(self):
        """测试常见自闭合标签"""
        assert is_self_closing("<br/>") is True
        assert is_self_closing("<br />") is True
        assert is_self_closing("<img/>") is True
        assert is_self_closing("<hr>") is True
        assert is_self_closing("<meta charset='utf-8'/>") is True

    def test_regular_tag(self):
        """测试普通标签"""
        assert is_self_closing("<div>") is False
        assert is_self_closing("</div>") is False
        assert is_self_closing("<p>") is False

    def test_xhtml_style(self):
        """测试XHTML风格"""
        assert is_self_closing("<input type='text'/>") is True
        assert is_self_closing("<col width='100'/>") is True


class TestGetTagName:
    def test_simple_tags(self):
        """测试简单标签"""
        assert get_tag_name("<div>") == "div"
        assert get_tag_name("</div>") == "div"
        assert get_tag_name("<p>") == "p"
        assert get_tag_name("</p>") == "p"

    def test_tags_with_attributes(self):
        """测试带属性的标签"""
        assert get_tag_name("<img src='test.jpg'>") == "img"
        assert get_tag_name("<a href='http://example.com'>") == "a"
        assert get_tag_name("<input type='text' name='foo'/>") == "input"

    def test_uppercase_tags(self):
        """测试大写标签名"""
        assert get_tag_name("<DIV>") == "div"
        assert get_tag_name("<P class='foo'>") == "p"

    def test_invalid_tags(self):
        """测试无效标签"""
        assert get_tag_name("< >") is None
        assert get_tag_name("</>") is None
        assert get_tag_name("<>") is None


class TestVerifyHtmlIntegrityEdgeCases:
    """覆盖 verify_html_integrity 的边界分支"""

    def test_unclosed_angle_bracket(self):
        """测试 < 后没有 >（覆盖 lines 29-30）"""
        html = "<div><p"
        is_valid, errors = verify_html_integrity(html)
        assert is_valid is False
        assert "标签未闭合" in errors[0]

    def test_closing_tag_empty_name(self):
        """测试结束标签名为空（覆盖 lines 48-49）"""
        # </ > 以 </ 开头但标签名为空，get_tag_name 返回 None，被跳过
        html = "<div></ ></div>"
        is_valid, errors = verify_html_integrity(html)
        # </ > 被跳过，</div> 正常 pop div，结果有效
        assert is_valid is True

    def test_unmatched_closing_tag(self):
        """测试未匹配的结束标签（覆盖 line 59）"""
        # </span> 不在栈中，记录错误但继续；最终返回 True（栈为空）
        html = "</span>"
        is_valid, errors = verify_html_integrity(html)
        assert "未匹配的结束标签" in errors[0]


class TestVerifyFinalHtmlEdgeCases:
    """覆盖 verify_final_html 的边界分支"""

    def test_invalid_xml(self):
        """测试 XML 格式错误（覆盖 lines 153-154）"""
        html = "<html><body><p>unclosed"
        is_valid, error = verify_final_html("", html)
        assert is_valid is False
        assert "XML 格式错误" in error
