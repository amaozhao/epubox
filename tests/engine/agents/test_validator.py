
from engine.agents.validator import validate_html_pairing, validate_placeholders


class TestValidatePlaceholders:
    """Test the legacy validate_placeholders function (always returns True now)"""

    def test_validate_empty_tag_map(self):
        """测试空tag_map直接通过"""
        assert validate_placeholders("Hello World", {}) == (True, "")

    def test_validate_always_passes(self):
        """Legacy function always returns True since we no longer use placeholders"""
        text = "[id0]Hello[id1] World"
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<span>"}
        valid, msg = validate_placeholders(text, tag_map)
        assert valid is True
        assert msg == ""


class TestValidateHtmlPairing:
    """Test the new HTML tag pairing validation"""

    def test_validate_identical_html(self):
        """HTML with same structure passes"""
        original = "<p>Hello</p>"
        translated = "<p>你好</p>"
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True
        assert msg == ""

    def test_validate_tag_mismatch(self):
        """HTML with different tag types now passes if translated HTML is structurally valid.

        Under the new architecture, we trust the LLM tried its best to preserve tags.
        We only verify that the translated HTML has valid structure, not that it
        exactly matches the original tag names.
        """
        original = "<p>Hello</p>"
        translated = "<div>Hello</div>"  # p tag changed to div - but structurally valid
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True  # New architecture: structurally valid = pass

    def test_validate_missing_close_tag(self):
        """HTML with missing closing tag fails"""
        original = "<p>Hello</p>"
        translated = "<p>Hello"  # missing closing tag
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_extra_tag(self):
        """HTML with extra closing tag fails"""
        original = "<p>Hello</p>"
        translated = "<p>Hello</p></div>"  # extra closing tag
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_nested_tags(self):
        """Nested HTML with proper structure passes"""
        original = "<div><p>Hello <strong>World</strong></p></div>"
        translated = "<div><p>你好 <strong>世界</strong></p></div>"
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True

    def test_validate_epub_namespace(self):
        """HTML with epub: prefix attributes passes validation (epub namespace issue)"""
        original = '<nav epub:type="toc" role="doc-toc"><h2>Contents</h2></nav>'
        translated = '<nav epub:type="toc" role="doc-toc"><h2>目录</h2></nav>'
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True
        assert msg == ""

    def test_validate_epub_pagebreak(self):
        """HTML with epub:type pagebreak passes validation"""
        original = '<div epub:type="pagebreak" id="page1" aria-label="1"/>'
        translated = '<div aria-label="1" epub:type="pagebreak" id="page1"/>'
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True

    def test_validate_multiple_root_elements(self):
        """HTML with multiple root elements (like <li></li><li></li>) passes validation"""
        # This is common in EPUB nav chunks that split at <li> boundaries
        original = '<li><a href="p1">1</a></li><li><a href="p2">2</a></li>'
        translated = '<li><a href="p1">一</a></li><li><a href="p2">二</a></li>'
        valid, msg = validate_html_pairing(original, translated)
        assert valid is True

    def test_validate_multiple_root_elements_with_extra_tag(self):
        """HTML with multiple root elements AND extra closing tag fails"""
        original = '<li><a href="p1">1</a></li>'
        translated = '<li><a href="p1">一</a></li></div>'  # extra closing tag
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_unclosed_p_tag_in_snippet(self):
        """验证代码片段中意外的闭合标签被正确检测（LLM 返回了无效 HTML）"""
        # LLM 正常返回，但返回的内容结构无效
        # 原文：<span class="hljs-string">"https://..."</span>)</p><p class="snippet
        # 翻译：get(<span class="hljs-string">"https://..."</span>)</p><p class="snippet
        # 翻译多了 "get(" 并且有意外的 </p> 闭合标签
        original = '<span class="hljs-string">"https://api.chucknorris.io/jokes/random"</span>)</p><p class="snippet'
        translated = 'get(<span class="hljs-string">"https://api.chucknorris.io/jokes/random"</span>)</p><p class="snippet'
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False

    def test_validate_mismatched_snippet_content(self):
        """验证代码片段内容不匹配时被检测（LLM 生成了错误内容）"""
        # 真实的错误场景：原文是 "data = requests.get(url)"，翻译变成了 "res = requests.get"
        original = '<p class="snippet-code">    data = requests.get(url)</p>'
        translated = '<p class="snippet-code">    res = requests.get(url)</p>'  # 内容被改变了
        valid, msg = validate_html_pairing(original, translated)
        # 这种情况下如果 XML 结构正确，应该通过
        assert valid is True  # 因为标签是配对的，只是文本内容不同

    def test_validate_content_truncation(self):
        """验证 LLM 删减内容时被检测到"""
        # LLM 翻译时长段落被压缩：原文 19 个 <p>，译文只有 15 个
        original = '<p>Paragraph 1</p>' * 19
        translated = '<p>Paragraph 1</p>' * 15
        valid, msg = validate_html_pairing(original, translated)
        assert valid is False
        assert "内容被删减" in msg

    def test_validate_content_truncation_small_diff(self):
        """验证小幅度内容差异不被误判"""
        # 原文 5 个 <p>，译文 4 个（差距只有 1 个，在容差范围内）
        original = '<p>Para 1</p>' * 5
        translated = '<p>Para 1</p>' * 4
        valid, msg = validate_html_pairing(original, translated)
        # 差距 1 个，在容差 2 以内，应该通过
        assert valid is True
