from engine.item.precode import PreCodeExtractor


class TestPreCodeExtractor:
    """测试 PreCodeExtractor 类"""

    def test_extract_single_pre(self):
        """测试单个 pre 标签提取"""
        html = "<pre>code</pre><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert extractor.preserved_pre == ["<pre>code</pre>"]

    def test_extract_single_code(self):
        """测试单个 code 标签提取"""
        html = "<p>text</p><code>x=1</code>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>text</p>[CODE:0]"
        assert extractor.preserved_code == ["<code>x=1</code>"]

    def test_extract_single_style(self):
        """测试单个 style 标签提取"""
        html = "<style>body { color: red; }</style><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[STYLE:0]<p>text</p>"
        assert extractor.preserved_style == ["<style>body { color: red; }</style>"]

    def test_extract_nested_pre_code(self):
        """测试嵌套的 pre>code 提取"""
        html = "<pre><code>nested</code></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        assert extractor.preserved_pre == ["<pre><code>nested</code></pre>"]

    def test_extract_multiple(self):
        """测试多个 pre 和 code 提取"""
        html = "<pre>code1</pre><code>x=1</code><pre>code2</pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[PRE:0]" in result
        assert "[CODE:0]" in result
        assert "[PRE:1]" in result
        assert extractor.preserved_pre == ["<pre>code1</pre>", "<pre>code2</pre>"]
        assert extractor.preserved_code == ["<code>x=1</code>"]

    def test_extract_order_pre_then_code(self):
        """测试提取顺序：先 pre，后 code"""
        html = "<pre>pre1</pre><code>code1</code>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        # pre 先被提取
        assert result.startswith("[PRE:0]")
        # code 后被提取
        assert "[CODE:0]" in result

    def test_extract_preserves_attributes(self):
        """测试提取保留标签属性"""
        html = '<pre class="python" id="test">code</pre>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert '[PRE:0]' in result
        assert 'class="python"' in extractor.preserved_pre[0]
        assert 'id="test"' in extractor.preserved_pre[0]

    def test_extract_empty_pre(self):
        """测试空 pre 标签提取"""
        html = "<pre></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        assert extractor.preserved_pre[0] == "<pre></pre>"

    def test_extract_deep_nesting(self):
        """测试深层嵌套"""
        html = "<pre><div><code>nested</code></div></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        # 整体作为字符串保存
        assert "<pre>" in extractor.preserved_pre[0]
        assert "<code>" in extractor.preserved_pre[0]

    def test_extract_nested_style_in_pre(self):
        """测试嵌套的 style 在 pre 中"""
        html = "<pre><style>.x { color: red; }</style></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        assert "<style>" in extractor.preserved_pre[0]

    def test_extract_multiple_styles(self):
        """测试多个 style 标签提取"""
        html = "<style>a{}</style><style>b{}</style><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[STYLE:0]" in result
        assert "[STYLE:1]" in result
        assert extractor.preserved_style == ["<style>a{}</style>", "<style>b{}</style>"]

    def test_extract_style_preserves_attributes(self):
        """测试 style 标签提取保留属性"""
        html = '<style type="text/css">.cls { color: blue; }</style>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[STYLE:0]" in result
        assert 'type="text/css"' in extractor.preserved_style[0]

    def test_extract_pre_code_style_together(self):
        """测试 pre、code、style 同时存在"""
        html = "<pre>code</pre><style>.x{}</style><code>x=1</code>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[PRE:0]" in result
        assert "[STYLE:0]" in result
        assert "[CODE:0]" in result


class TestRestore:
    """测试恢复功能"""

    def test_restore_pre(self):
        """测试恢复 pre 标签"""
        html = "[PRE:0]<p>翻译后</p>"
        extractor = PreCodeExtractor()
        extractor.preserved_pre = ["<pre>original</pre>"]
        result = extractor.restore(html)

        assert result == "<pre>original</pre><p>翻译后</p>"

    def test_restore_code(self):
        """测试恢复 code 标签"""
        html = "[CODE:0]<p>翻译后</p>"
        extractor = PreCodeExtractor()
        extractor.preserved_code = ["<code>x=1</code>"]
        result = extractor.restore(html)

        assert result == "<code>x=1</code><p>翻译后</p>"

    def test_restore_order_pre_then_code(self):
        """测试恢复顺序：先 code，后 pre"""
        html = "[PRE:0][CODE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_pre = ["<pre>P</pre>"]
        extractor.preserved_code = ["<code>C</code>"]
        result = extractor.restore(html)

        assert result == "<pre>P</pre><code>C</code>"

    def test_restore_shuffled_placeholders(self):
        """测试 LLM 打乱占位符顺序后的正确恢复"""
        # LLM 可能输出 [PRE:1] 在 [PRE:0] 之前
        html = "[PRE:1]<p>Hello</p>[PRE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_pre = ["<pre>A</pre>", "<pre>B</pre>"]  # [PRE:0]=A, [PRE:1]=B
        result = extractor.restore(html)

        # 应该正确恢复：A 对应 [PRE:0]，B 对应 [PRE:1]
        assert result == "<pre>B</pre><p>Hello</p><pre>A</pre>"

    def test_restore_shuffled_code(self):
        """测试 code 占位符打乱后的恢复"""
        html = "[CODE:1]<p>Hello</p>[CODE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_code = ["<code>A</code>", "<code>B</code>"]
        result = extractor.restore(html)

        assert result == "<code>B</code><p>Hello</p><code>A</code>"

    def test_restore_style(self):
        """测试恢复 style 标签"""
        html = "[STYLE:0]<p>翻译后</p>"
        extractor = PreCodeExtractor()
        extractor.preserved_style = ["<style>body { color: red; }</style>"]
        result = extractor.restore(html)

        assert result == "<style>body { color: red; }</style><p>翻译后</p>"

    def test_restore_pre_code_style_order(self):
        """测试恢复顺序：先 style，后 code，后 pre"""
        html = "[PRE:0][CODE:0][STYLE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_pre = ["<pre>P</pre>"]
        extractor.preserved_code = ["<code>C</code>"]
        extractor.preserved_style = ["<style>S</style>"]
        result = extractor.restore(html)

        assert result == "<pre>P</pre><code>C</code><style>S</style>"

    def test_restore_shuffled_style(self):
        """测试 style 占位符打乱后的恢复"""
        html = "[STYLE:1]<p>Hello</p>[STYLE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_style = ["<style>A</style>", "<style>B</style>"]
        result = extractor.restore(html)

        assert result == "<style>B</style><p>Hello</p><style>A</style>"


class TestFullFlow:
    """完整流程测试"""

    def test_full_flow(self):
        """测试完整的提取-恢复流程"""
        html = """
        <div>
            <pre>
            function test() {
                return 1;
            }
            </pre>
            <p>Hello</p>
        </div>
        """

        extractor = PreCodeExtractor()

        # 1. 提取
        step1 = extractor.extract(html)
        assert "[PRE:0]" in step1
        assert "<pre>" not in step1

        # 2. 恢复
        step2 = extractor.restore(step1)
        assert "<pre>" in step2
        assert "<p>" in step2
