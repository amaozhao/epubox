from engine.agents.verifier import (
    EnglishResidualDecision,
    classify_untranslated_english_texts,
    find_untranslated_english_texts,
    validate_translated_html,
)


class TestValidateTranslatedHtmlTagErrors:
    def test_validate_catches_tag_crossing(self):
        """测试能检测同类型标签交叉（BeautifulSoup 会自动修复的情况）"""
        original = "<p><strong>Hello</strong></p>"
        translated = "<p><strong>你好</p></strong>"  # 错误：strong 和 p 交叉
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "标签" in error or "交错" in error

    def test_validate_catches_unclosed_tags(self):
        """测试能检测未闭合标签"""
        original = "<p>A</p><p>B</p>"
        translated = "<p>甲<p>乙</p>"  # 错误：第一个 p 未闭合
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid

    def test_validate_accepts_correct_html(self):
        """测试正确的 HTML 能通过验证"""
        original = "<p><strong>Hello</strong></p>"
        translated = "<p><strong>你好</strong></p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid

    def test_validate_catches_unescaped_ampersand(self):
        """测试能检测未转义的 & 字符（XML 格式错误）"""
        original = "<p>Tom & Jerry</p>"
        translated = "<p>汤姆 & 杰瑞</p>"  # 错误：& 未转义为 &amp;
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "XML 格式错误" in error

    def test_validate_accepts_html_entity_nbsp(self):
        """测试 &nbsp; 等 HTML 实体应通过验证（BS4 已解码，regex 不拦截合法实体）"""
        original = "<p>Hello</p>"
        translated = "<p>Hello&nbsp;World</p>"  # BS4 将 &nbsp; 解码为 \xa0
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, f"HTML 实体不应被误判: {error}"

    def test_validate_accepts_escaped_ampersand(self):
        """测试已转义的 &amp; 应通过验证"""
        original = "<p>Tom &amp; Jerry</p>"
        translated = "<p>汤姆 &amp; 杰瑞</p>"  # &amp; 已转义
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, f"已转义 &amp; 应通过: {error}"

    def test_validate_rejects_unicode_original_echo(self):
        """测试 unicode 原样回显也会被识别为未翻译"""
        original = "<p>你好世界</p>"
        translated = "<p>你好世界</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "疑似未翻译" in error

    def test_validate_accepts_symbol_only_noop(self):
        """测试只有数字和占位符的原样回显会作为合法 no-op 接受"""
        original = "<p>2024 [PRE:0] !!!</p>"
        translated = "<p>2024 [PRE:0] !!!</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid
        assert error == "accepted_as_is"

    def test_validate_accepts_technical_ascii_command_noop(self):
        """测试技术型 ASCII 命令原样回显会作为合法 no-op 接受"""
        original = "<p>python main.py translate book.epub --limit 1200 --language Chinese</p>"
        translated = "<p>python main.py translate book.epub --limit 1200 --language Chinese</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid
        assert error == "accepted_as_is"

    def test_validate_rejects_residual_untranslated_english_sentence(self):
        """测试译文中残留自然英文句子时会被 chunk 校验拦截。"""
        original = "<p>This source paragraph should be translated.</p>"
        translated = "<p>这是中文说明。This sentence remains untranslated and should fail validation.</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "疑似残留未翻译英文" in error

    def test_classifier_marks_short_english_title_as_review_not_failure(self):
        """测试短英文标题进入人工复核区，不直接阻断。"""
        findings = classify_untranslated_english_texts("<p>Application Layer</p>")

        assert len(findings) == 1
        assert findings[0].decision == EnglishResidualDecision.REVIEW
        assert findings[0].text == "Application Layer"
        assert find_untranslated_english_texts("<p>Application Layer</p>") == []

    def test_classifier_marks_complete_english_sentence_as_failure(self):
        """测试完整英文句子进入失败区，兼容旧接口返回命中文本。"""
        html = "<p>The client software will automatically retrieve the new URL.</p>"

        findings = classify_untranslated_english_texts(html)

        assert len(findings) == 1
        assert findings[0].decision == EnglishResidualDecision.FAIL
        assert find_untranslated_english_texts(html) == [
            "The client software will automatically retrieve the new URL."
        ]

    def test_classifier_ignores_bibliographic_citations_in_chinese_text(self):
        """测试中文段落中的作者年份引用不会进入复核区。"""
        html = (
            "<p>实现时，控制器语义必须被纳入考量 [Panda 2013; Ferguson 2021]。"
            "相关方案可参考 [Lamport 1989, Lampson 1996]。"
            "现代控制器（如 ONOS [ONOS 2025] 和 ORION [Ferguson 2021]）"
            "强调构建逻辑集中式但物理分布式的平台。</p>"
        )

        assert classify_untranslated_english_texts(html) == []

    def test_classifier_ignores_angle_bracket_protocol_message_names(self):
        """测试中文段落中的协议消息名不会进入复核区。"""
        html = (
            "<p>管理服务器与受管设备交换 &lt;hello&gt; 消息，随后使用 "
            "&lt;rpc&gt; 和 &lt;rpc-response&gt; 消息进行交互，"
            "并通过 NETCONF &lt;notification&gt; 与 &lt;session-close&gt; 完成通知和关闭。</p>"
        )

        assert classify_untranslated_english_texts(html) == []

    def test_validate_allows_common_technical_english_terms(self):
        """测试常见技术名词保留英文不会被误判为漏译。"""
        original = "<p>Use AWS CodePipeline with GitHub Actions and Docker for DevOps workflows.</p>"
        translated = "<p>我们将使用 AWS CodePipeline、GitHub Actions 和 Docker 构建 DevOps 工作流。</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_allows_repeated_fast_open_cookie_terms(self):
        """测试重复保留协议术语 Fast Open Cookie 不会被误判为英文漏译。"""
        original = (
            "<p>TCP Fast Open lets clients send a Fast-Open Cookie with application data "
            "when reconnecting to the server.</p>"
        )
        translated = (
            "<p>在传统的三次握手过程中，客户端还可以请求服务器提供一个快速打开"
            "（Fast-Open）Cookie，该 Cookie 会编码未来连接所需的信息。"
            "下次客户端建立连接时，它会在首个消息中携带该 Fast Open Cookie "
            "及应用层数据。服务器检查 Fast Open Cookie 后继续处理请求。</p>"
        )

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_allows_inline_english_term_with_chinese_parent_context(self):
        """测试中文上下文中的短英文术语节点不会被当作整句漏译。"""
        original = "<p>The RAN uses a <b>disaggregated architecture</b> for open interfaces.</p>"
        translated = "<p>RAN 采用 <b>disaggregated architecture</b> 架构，以支持开放接口。</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_rejects_inline_english_sentence_with_chinese_parent_context(self):
        """测试中文上下文中的完整英文句子仍会被拦截。"""
        original = "<p>The RAN uses a <b>disaggregated architecture</b> for open interfaces.</p>"
        translated = (
            "<p>RAN 采用 <b>This sentence remains untranslated and should fail validation.</b> "
            "以支持开放接口。</p>"
        )

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "疑似残留未翻译英文" in error

    def test_validate_allows_code_url_and_file_names(self):
        """测试代码、URL 和文件名中的英文不会被漏译扫描误杀。"""
        original = (
            "<p>Run <code>terraform apply</code> and see https://docs.aws.amazon.com "
            "for <code>main.tf</code> and <code>terraform.tfvars</code>.</p>"
        )
        translated = (
            "<p>运行 <code>terraform apply</code>，参考 https://docs.aws.amazon.com，"
            "并保留 <code>main.tf</code> 和 <code>terraform.tfvars</code> 文件名。</p>"
        )

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_allows_english_inside_pre_blocks(self):
        """测试 pre/code 代码块中的英文命令不会被漏译扫描误杀。"""
        original = "<pre><code>terraform apply\naws s3 ls</code></pre><p>Then verify the result.</p>"
        translated = "<pre><code>terraform apply\naws s3 ls</code></pre><p>然后验证结果。</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_accepts_adjacent_code_swap_within_single_element(self):
        """测试同一元素内相邻 CODE 互换顺序会被视为可接受。"""
        original = "<p>Use [CODE:10] [CODE:11] here</p>"
        translated = "<p>使用 [CODE:11] [CODE:10] 这里</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, error

    def test_validate_accepts_non_adjacent_code_reorder_within_single_element(self):
        """测试同一元素内的非相邻 CODE 重排也会被接受。"""
        original = "<p>Run [CODE:31], [CODE:32], and [CODE:33]</p>"
        translated = "<p>在 [CODE:33] 所在目录中运行 [CODE:31] 和 [CODE:32]</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert is_valid, error

    def test_validate_rejects_code_cross_element_move(self):
        """测试 CODE 跨顶层元素迁移仍然会被拒绝。"""
        original = "<p>[CODE:10]</p><p>[CODE:11] [CODE:12]</p>"
        translated = "<p>[CODE:11]</p><p>[CODE:10] [CODE:12]</p>"
        is_valid, error = validate_translated_html(original, translated)
        assert not is_valid
        assert "CODE 占位符归属/数量不一致" in error

    def test_validate_rejects_attribute_boundary_corruption(self):
        """测试模型改坏标签属性边界时会在 chunk 校验阶段被拦截。"""
        original = (
            '<figure><img alt="The nine-dots puzzle challenges players to join the nine dots." '
            'id="Business0001483" src="../images/Page-087_1.jpg"/></figure>'
        )
        translated = (
            '<figure><img alt="九点连线谜题要求玩家连接九个点。” id="Business0001483" '
            'src="../images/Page-087_1.jpg"/></figure>'
        )

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "属性" in error
