
from bs4 import BeautifulSoup

from engine.item.precode import (
    PreCodeExtractor,
    attempt_recovery,
    validate_placeholders,
)


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

    def test_extract_inline_tt_wrapper_as_code(self):
        """测试段落中的 span>tt 技术片段会按 CODE 保护。"""
        html = '<p>Use <span class="calibre2"><tt class="calibre4">BaseTool</tt></span> here.</p>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>Use [CODE:0] here.</p>"
        assert extractor.preserved_code == ['<span class="calibre2"><tt class="calibre4">BaseTool</tt></span>']

    def test_extract_inline_code_like_run_merges_adjacent_tt_wrappers(self):
        """测试相邻内联技术片段会与安全分隔符合并为一个 CODE 占位符。"""
        html = (
            '<p>Tools: <span class="calibre2"><tt class="calibre4">ReadFileTool</tt></span> / '
            '<span class="calibre2"><tt class="calibre4">WriteFileTool</tt></span></p>'
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>Tools: [CODE:0]</p>"
        assert extractor.preserved_code == [
            '<span class="calibre2"><tt class="calibre4">ReadFileTool</tt></span> / '
            '<span class="calibre2"><tt class="calibre4">WriteFileTool</tt></span>'
        ]

    def test_extract_inline_semantic_code_tags_as_code(self):
        """测试 kbd/samp/var 这类内联语义标签也会按 CODE 保护。"""
        html = "<p>Press <kbd>Ctrl</kbd> and inspect <samp>stdout</samp> in <var>PATH</var>.</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>Press [CODE:0] and inspect [CODE:1] in [CODE:2].</p>"
        assert extractor.preserved_code == ["<kbd>Ctrl</kbd>", "<samp>stdout</samp>", "<var>PATH</var>"]

    def test_extract_plain_prose_span_is_not_protected_as_code(self):
        """测试普通 prose span 不会被误判为 CODE。"""
        html = '<p><span class="bold">Important:</span> This is prose content.</p>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == html
        assert extractor.preserved_code == []

    def test_extract_adjacent_code_run_with_safe_separator(self):
        """测试相邻 code + 安全分隔符会合并为一个占位符。"""
        html = "<p>Use <code>Ctrl</code>/<code>Cmd</code> to continue.</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>Use [CODE:0] to continue.</p>"
        assert extractor.preserved_code == ["<code>Ctrl</code>/<code>Cmd</code>"]

    def test_extract_code_run_does_not_merge_across_natural_language(self):
        """测试 code 之间存在自然语言时不会合并。"""
        html = "<p><code>kill</code> sends the <code>SIGTERM</code> signal.</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0] sends the [CODE:1] signal.</p>"
        assert extractor.preserved_code == ["<code>kill</code>", "<code>SIGTERM</code>"]

    def test_extract_code_run_merges_three_codes(self):
        """测试三个连续 code 片段会合并为单一占位符。"""
        html = "<p><code>Ctrl</code>/<code>Alt</code>/<code>Del</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0]</p>"
        assert extractor.preserved_code == ["<code>Ctrl</code>/<code>Alt</code>/<code>Del</code>"]

    def test_extract_code_run_merges_with_whitespace_and_newlines(self):
        """测试分隔符包含空白和换行时仍可合并。"""
        html = "<p><code>user</code> \n : \n <code>group</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0]</p>"
        assert extractor.preserved_code == ["<code>user</code> \n : \n <code>group</code>"]

    def test_extract_code_run_preserves_attributes(self):
        """测试合并后的 code-run 会保留各个 code 标签属性。"""
        html = '<p><code class="k">Ctrl</code>/<code data-key="cmd">Cmd</code></p>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0]</p>"
        assert 'class="k"' in extractor.preserved_code[0]
        assert 'data-key="cmd"' in extractor.preserved_code[0]

    def test_extract_code_run_does_not_merge_across_inline_html_separator(self):
        """测试中间插入 HTML 节点时不会合并 code-run。"""
        html = "<p><code>A</code><span>/</span><code>B</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0]<span>/</span>[CODE:1]</p>"
        assert extractor.preserved_code == ["<code>A</code>", "<code>B</code>"]

    def test_extract_code_run_keeps_multiple_runs_separate(self):
        """测试同一父节点中的多个独立 code-run 不会串成一个。"""
        html = "<p><code>Ctrl</code>/<code>Cmd</code> or <code>Alt</code>/<code>Opt</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0] or [CODE:1]</p>"
        assert extractor.preserved_code == [
            "<code>Ctrl</code>/<code>Cmd</code>",
            "<code>Alt</code>/<code>Opt</code>",
        ]

    def test_extract_code_run_at_start_and_end_of_parent(self):
        """测试 code-run 位于父节点起始或结尾位置时仍能正确提取。"""
        html = "<p><code>Ctrl</code>/<code>Cmd</code> to continue with <code>Esc</code>/<code>Exit</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>[CODE:0] to continue with [CODE:1]</p>"
        assert extractor.preserved_code == [
            "<code>Ctrl</code>/<code>Cmd</code>",
            "<code>Esc</code>/<code>Exit</code>",
        ]

    def test_extract_single_style(self):
        """测试单个 style 标签提取"""
        html = "<style>body { color: red; }</style><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[STYLE:0]<p>text</p>"
        assert extractor.preserved_style == ["<style>body { color: red; }</style>"]

    def test_extract_code_like_blockquote_with_tt_descendants_as_pre(self):
        """测试 syntax-highlight 风格的 blockquote 代码块会整体按 PRE 保护。"""
        html = (
            '<blockquote>'
            '<span class="calibre5"><tt class="calibre4"><span class="tok">print</span></tt></span>'
            '<span class="calibre5"><tt class="calibre4"><span class="tok">(</span></tt></span>'
            '<span class="calibre5"><tt class="calibre4"><span class="tok">x</span></tt></span>'
            '</blockquote><p>text</p>'
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert extractor.preserved_pre[0].startswith("<blockquote>")
        assert extractor.preserved_code == []

    def test_extract_code_like_container_by_class_keyword_as_pre(self):
        """测试带 highlight/listing 等类名的代码容器会整体按 PRE 保护。"""
        html = '<div class="syntax-highlight"><span>const x = 1;</span></div><p>text</p>'
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert 'class="syntax-highlight"' in extractor.preserved_pre[0]

    def test_extract_code_like_container_by_scoring_without_metadata(self):
        """测试无显式 code 类名时，也能通过评分器识别典型代码块。"""
        html = (
            '<blockquote>'
            '<span>async</span><br/>'
            '<span>def</span><span> main():</span><br/>'
            '<span>    return 1</span>'
            '</blockquote><p>text</p>'
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert extractor.preserved_pre[0].startswith("<blockquote>")

    def test_extract_code_like_table_by_scoring_as_pre(self):
        """测试带有高密度代码 token 的表格会整体按 PRE 保护。"""
        html = (
            "<table>"
            "<tr><td><tt>BaseTool</tt></td><td><tt>execute()</tt></td></tr>"
            "<tr><td><tt>call_tool()</tt></td><td><tt>inputSchema</tt></td></tr>"
            "</table><p>text</p>"
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert extractor.preserved_pre[0].startswith("<table>")

    def test_extract_code_like_ordered_list_by_scoring_as_pre(self):
        """测试由代码步骤组成的有序列表会整体按 PRE 保护。"""
        html = (
            "<ol>"
            "<li><tt>import</tt> <tt>BaseTool</tt></li>"
            "<li><tt>def</tt> <tt>execute()</tt></li>"
            "<li><tt>return</tt> <tt>result</tt></li>"
            "</ol><p>text</p>"
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]<p>text</p>"
        assert extractor.preserved_pre[0].startswith("<ol>")

    def test_code_like_scoring_penalizes_prose_heavy_container(self):
        """测试 prose 主导的容器即使有 span，也不会被误判为代码块。"""
        html = (
            "<blockquote>"
            "<span>This is a long explanatory sentence about agent orchestration.</span>"
            "<span>Another descriptive sentence continues the prose discussion.</span>"
            "</blockquote><p>text</p>"
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == html
        assert extractor.preserved_pre == []

    def test_extract_regular_blockquote_prose_is_not_protected(self):
        """测试普通 prose blockquote 不会被误判为代码块。"""
        html = "<blockquote><p>This is a quote.</p></blockquote><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<blockquote><p>This is a quote.</p></blockquote><p>text</p>"
        assert extractor.preserved_pre == []
        assert extractor.preserved_code == []

    def test_extract_regular_prose_table_is_not_protected(self):
        """测试普通 prose 表格不会被误判为代码块。"""
        html = (
            "<table>"
            "<tr><th>Term</th><th>Meaning</th></tr>"
            "<tr><td>Agent</td><td>A software component that performs tasks.</td></tr>"
            "</table><p>text</p>"
        )
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == html
        assert extractor.preserved_pre == []

    def test_score_code_like_container_reports_strong_and_weak_signals(self):
        """测试评分器会综合 metadata / tt / symbols 等信号。"""
        html = (
            '<blockquote class="listing">'
            '<span><tt>print</tt></span><span><tt>(x)</tt></span><span><tt>;</tt></span>'
            '</blockquote>'
        )
        extractor = PreCodeExtractor()
        soup = BeautifulSoup(html, "html.parser")
        score, reasons = extractor._score_code_like_container(soup.blockquote)

        assert score >= 5
        assert any(reason.startswith("metadata:") for reason in reasons)
        assert any(reason.startswith("tt:") for reason in reasons)

    def test_extract_nested_pre_code(self):
        """测试嵌套的 pre>code 提取"""
        html = "<pre><code>nested</code></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        assert extractor.preserved_pre == ["<pre><code>nested</code></pre>"]
        assert extractor.preserved_code == []

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

        assert "[PRE:0]" in result
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
        assert extractor.preserved_code == []

    def test_extract_nested_style_in_pre(self):
        """测试嵌套的 style 在 pre 中"""
        html = "<pre><style>.x { color: red; }</style></pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "[PRE:0]"
        assert "<style>" in extractor.preserved_pre[0]
        assert extractor.preserved_style == []

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

    def test_restore_shuffled_merged_code_runs(self):
        """测试合并后的 code-run 占位符打乱后也能正确恢复。"""
        html = "[CODE:1]<p>Hello</p>[CODE:0]"
        extractor = PreCodeExtractor()
        extractor.preserved_code = [
            "<code>Ctrl</code>/<code>Cmd</code>",
            "<code>Alt</code>/<code>Opt</code>",
        ]
        result = extractor.restore(html)

        assert result == "<code>Alt</code>/<code>Opt</code><p>Hello</p><code>Ctrl</code>/<code>Cmd</code>"

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


class TestValidatePlaceholders:
    """测试验证功能"""

    def test_validate_valid(self):
        """测试验证通过"""
        html = "[PRE:0][CODE:0][id0]你好[id1]"
        assert validate_placeholders(html, 1, 1) is True

    def test_validate_missing_pre(self):
        """测试缺少 pre 占位符"""
        html = "[CODE:0][id0]你好[id1]"
        assert validate_placeholders(html, 1, 1) is False

    def test_validate_missing_code(self):
        """测试缺少 code 占位符"""
        html = "[PRE:0][id0]你好[id1]"
        assert validate_placeholders(html, 1, 1) is False

    def test_validate_extra_pre(self):
        """测试多余的 pre 占位符"""
        html = "[PRE:0][PRE:1][CODE:0][id0]你好[id1]"
        assert validate_placeholders(html, 1, 1) is False

    def test_validate_multiple(self):
        """测试多个占位符"""
        html = "[PRE:0][PRE:1][CODE:0][CODE:1]"
        assert validate_placeholders(html, 2, 2) is True
        assert validate_placeholders(html, 2, 1) is False


class TestAttemptRecovery:
    """测试容错恢复"""

    def test_recovery_semicolon(self):
        """测试修复分号"""
        html = "[PRE;0]<p>[CODE;1]</p>"
        result = attempt_recovery(html, [], [])
        assert "[PRE:0]" in result
        assert "[CODE:1]" in result

    def test_recovery_extra_space(self):
        """测试修复多余空格"""
        html = "[PRE: 0]<p>[CODE: 1]</p>"
        result = attempt_recovery(html, [], [])
        assert "[PRE:0]" in result
        assert "[CODE:1]" in result

    def test_recovery_mixed(self):
        """测试混合修复"""
        html = "[PRE; 0]<p>[CODE; 1]</p>"
        result = attempt_recovery(html, [], [])
        assert "[PRE:0]" in result
        assert "[CODE:1]" in result

    def test_recovery_unrecoverable(self):
        """测试不可恢复的情况（丢失括号）"""
        html = "PRE:0"  # 丢失左括号
        result = attempt_recovery(html, [], [])
        # 应该保持不变
        assert "PRE:0" in result

    def test_recovery_preserves_content(self):
        """测试恢复后内容不变"""
        html = "[PRE;0]Hello[CODE;1]"
        result = attempt_recovery(html, [], [])
        # Hello 应该不受影响
        assert "Hello" in result


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


class TestReplaceWithSimplified:
    """
    验证 replace_with(placeholder) 与 replace_with(BeautifulSoup(placeholder)) 行为一致。

    这些测试确保简化后的实现产生与简化前完全相同的结果。
    """

    def test_basic_pre_replacement(self):
        """基本 pre 替换：占位符作为纯文本插入，不被解析为 HTML"""
        html = "<p>before</p><pre>code content</pre><p>after</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[PRE:0]" in result
        assert "<pre>" not in result
        assert "<p>before</p>" in result
        assert "<p>after</p>" in result

    def test_basic_code_replacement(self):
        """基本 code 替换：占位符正确插入"""
        html = "<p>before</p><code>x = 1</code><p>after</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[CODE:0]" in result
        assert "<code>" not in result
        assert "<p>before</p>" in result
        assert "<p>after</p>" in result

    def test_basic_style_replacement(self):
        """基本 style 替换：占位符正确插入"""
        html = "<style>.cls { color: red; }</style><p>text</p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[STYLE:0]" in result
        assert "<style>" not in result
        assert "<p>text</p>" in result

    def test_nested_tags_pre_contains_code(self):
        """嵌套标签：pre 中含 code，pre 整体以 [PRE:0] 替换。
        pre 作为原子块整体保护，内层 code 不再单独记账。
        """
        html = "<div><pre><code>nested code</code></pre></div>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        # 最终输出：整个 pre 被替换为单一占位符
        assert result == "<div>[PRE:0]</div>"
        assert extractor.pre_count == 1
        # 内层 code 不再重复保存
        assert extractor.code_count == 0
        # pre 保存的是原始字符串（含嵌套 code）
        assert "<code>nested code</code>" in extractor.preserved_pre[0]

    def test_nested_tags_code_contains_style(self):
        """嵌套标签：code 命中后整体保护，内层 style 不再单独记账。"""
        html = "<p>Use <code><style>.x{}</style>const x = 1;</code></p>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert result == "<p>Use [CODE:0]</p>"
        assert extractor.code_count == 1
        assert extractor.style_count == 0
        assert "<style>.x{}</style>" in extractor.preserved_code[0]

    def test_multiple_tags_all_types(self):
        """多个标签（所有类型）：每个都有独立占位符"""
        html = "<style>a{}</style><pre>code1</pre><code>inline</code><pre>code2</pre><style>b{}</style>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        assert "[STYLE:0]" in result
        assert "[PRE:0]" in result
        assert "[CODE:0]" in result
        assert "[PRE:1]" in result
        assert "[STYLE:1]" in result
        assert extractor.pre_count == 2
        assert extractor.code_count == 1
        assert extractor.style_count == 2

    def test_roundtrip_pre(self):
        """往返测试 pre：提取后恢复，结果与原始 HTML 一致（经 BS4 解析后）"""
        from bs4 import BeautifulSoup

        html = "<div><p>Hello</p><pre>def f(): pass</pre></div>"
        extractor = PreCodeExtractor()
        extracted = extractor.extract(html)
        restored = extractor.restore(extracted)

        # 用 BS4 规范化比较，避免空白差异
        original_normalized = str(BeautifulSoup(html, "html.parser"))
        assert restored == original_normalized

    def test_roundtrip_code(self):
        """往返测试 code：提取后恢复，结果与原始一致"""
        from bs4 import BeautifulSoup

        html = "<p>Use <code>print()</code> here.</p>"
        extractor = PreCodeExtractor()
        extracted = extractor.extract(html)
        restored = extractor.restore(extracted)

        original_normalized = str(BeautifulSoup(html, "html.parser"))
        assert restored == original_normalized

    def test_roundtrip_merged_code_run(self):
        """往返测试合并后的 code-run：提取后恢复，结果与原始一致。"""
        from bs4 import BeautifulSoup

        html = "<p>Use <code>Ctrl</code>/<code>Cmd</code> here.</p>"
        extractor = PreCodeExtractor()
        extracted = extractor.extract(html)
        restored = extractor.restore(extracted)

        original_normalized = str(BeautifulSoup(html, "html.parser"))
        assert restored == original_normalized

    def test_roundtrip_style(self):
        """往返测试 style：提取后恢复，结果与原始一致"""
        from bs4 import BeautifulSoup

        html = "<style>body { margin: 0; }</style><p>content</p>"
        extractor = PreCodeExtractor()
        extracted = extractor.extract(html)
        restored = extractor.restore(extracted)

        original_normalized = str(BeautifulSoup(html, "html.parser"))
        assert restored == original_normalized

    def test_placeholder_is_plain_text_not_parsed(self):
        """占位符为纯文本，不被 BS4 误解析为标签"""
        html = "<pre>x</pre>"
        extractor = PreCodeExtractor()
        result = extractor.extract(html)

        # [PRE:0] 中的方括号不会被解析为 HTML 标签
        assert "[PRE:0]" in result
        # 不应出现任何多余标签包裹
        assert "<" not in result.replace("<html>", "").replace("<body>", "").replace("</html>", "").replace(
            "</body>", ""
        )
