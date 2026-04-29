from engine.agents.verifier import (
    EnglishResidualDecision,
    classify_untranslated_english_texts,
    find_untranslated_english_texts,
    normalize_translated_html_attributes,
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

    def test_classifier_ignores_bibliographic_reference_entries(self):
        """测试完整参考文献条目不会被论文题名误判为残留英文句子。"""
        html = (
            "<p>Aminzadegan, S., Shahriari, M., Mehranfar, F., &amp; Abramović, B. (2022). "
            "Factors affecting the emission of pollutants in different types of transportation: "
            "A literature review.</p>"
        )

        assert classify_untranslated_english_texts(html) == []
        assert find_untranslated_english_texts(html) == []

    def test_validate_accepts_unchanged_bibliographic_reference_entry(self):
        """测试参考文献条目原样保留时会作为合法 no-op 接受。"""
        reference = (
            "<p>Aminzadegan, S., Shahriari, M., Mehranfar, F., &amp; Abramović, B. (2022). "
            "Factors affecting the emission of pollutants in different types of transportation: "
            "A literature review.</p>"
        )

        is_valid, error = validate_translated_html(reference, reference)

        assert is_valid, error
        assert error == "accepted_as_is"

    def test_classifier_ignores_non_year_bibliographic_reference_entries(self):
        """测试无年份的书目条目不会因英文书名和出版社被误判为漏译。"""
        html = (
            "<p>Kalbach, Jim: The Jobs to Be Done Playbook: Align Your Markets, "
            "Organizations, and Strategy Around Customer Needs. Two Waves Books.</p>"
        )

        assert classify_untranslated_english_texts(html) == []
        assert find_untranslated_english_texts(html) == []

    def test_validate_allows_translated_reference_with_original_english_title(self):
        """测试已翻译参考文献可保留原文书名和出版社信息。"""
        original = (
            "<p>Kalbach, Jim: The Jobs to Be Done Playbook: Align Your Markets, "
            "Organizations, and Strategy Around Customer Needs. Two Waves Books.</p>"
        )
        translated = (
            "<p>卡尔巴赫（Jim Kalbach）：《客户之需：Jobs to Be Done 实战指南》"
            "（The Jobs to Be Done Playbook: Align Your Markets, Organizations, "
            "and Strategy Around Customer Needs）。Two Waves Books。</p>"
        )

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_classifier_ignores_bibliographic_reference_with_translated_title(self):
        """测试作者年份保留、题名已翻译的参考文献不会进入复核区。"""
        html = (
            "<p>Abbas, A. H., Habelalmateen, M. I., Jurdi, S., Audah, L., &amp; Alduais, N. A. M. "
            "(2019). 基于 GPS 的定位监控系统及其地理围栏功能。</p>"
        )

        assert classify_untranslated_english_texts(html) == []

    def test_validate_allows_institution_original_names_in_parentheses(self):
        """测试中文译名后的英文机构原名括注不会被误判为漏译整句。"""
        original = (
            "<p>The authors thank Dr. D.Y. Patil School of Science and Technology and "
            "Dr. D.Y. Patil Vidyapeeth for seed funding support.</p>"
        )
        translated = (
            "<p>作者谨对印度浦那市 D.Y. 帕提尔科学技术学院"
            "（Dr. D.Y. Patil School of Science and Technology）及 D.Y. 帕提尔维迪亚佩特大学"
            "（Dr. D.Y. Patil Vidyapeeth）通过种子资金项目所提供的资助表示衷心感谢。</p>"
        )

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error
        assert find_untranslated_english_texts(translated) == []

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
            "<p>RAN 采用 <b>This sentence remains untranslated and should fail validation.</b> 以支持开放接口。</p>"
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

    def test_validate_accepts_safe_translated_aria_label(self):
        """测试 aria-label 这类可本地化辅助文本属性被翻译时不会误判为结构损坏。"""
        original = (
            '<p>See <a aria-label="D.O.I. link to this document." '
            'href="https://dx.doi.org/10.1201/9781003773504-91">DOI</a>.</p>'
        )
        translated = (
            '<p>参见 <a aria-label="D.O.I. 链接到本文档。" '
            'href="https://dx.doi.org/10.1201/9781003773504-91">DOI</a>。</p>'
        )

        is_valid, error = validate_translated_html(original, translated)

        assert is_valid, error

    def test_validate_accepts_safe_translated_alt_and_title_attributes(self):
        """测试 alt/title 辅助文本属性可安全本地化。"""
        cases = [
            (
                '<figure><img alt="Publisher logo." src="../images/pub.jpg"/></figure>',
                '<figure><img alt="出版商标志。" src="../images/pub.jpg"/></figure>',
            ),
            (
                '<p><abbr title="Application Programming Interface">API</abbr></p>',
                '<p><abbr title="应用程序编程接口">API</abbr></p>',
            ),
        ]

        for original, translated in cases:
            is_valid, error = validate_translated_html(original, translated)
            assert is_valid, error

    def test_validate_rejects_unsafe_translated_localizable_attribute(self):
        """测试可本地化属性含危险标记或占位符时仍然拒绝。"""
        original = '<p><a aria-label="D.O.I. link to this document." href="https://example.com">DOI</a></p>'
        translated = '<p><a aria-label="D.O.I. 链接 [CODE:0]" href="https://example.com">DOI</a></p>'

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "属性" in error

    def test_normalize_translated_html_attributes_restores_structural_attributes(self):
        """测试结构属性被模型翻译或改写时会按原文恢复。"""
        original = (
            '<p><a id="doi-link" class="xref" href="https://example.com/original" '
            'aria-label="D.O.I. link to this document.">DOI</a></p>'
        )
        translated = (
            '<p><a id="doi-cn" class="changed" href="https://example.com/translated" '
            'aria-label="D.O.I. 链接到本文档。">DOI</a></p>'
        )

        normalized = normalize_translated_html_attributes(original, translated)
        is_valid, error = validate_translated_html(original, normalized)

        assert is_valid, error
        assert 'id="doi-link"' in normalized
        assert 'class="xref"' in normalized
        assert 'href="https://example.com/original"' in normalized
        assert 'aria-label="D.O.I. 链接到本文档。"' in normalized

    def test_normalize_translated_html_attributes_removes_model_added_attributes(self):
        """测试模型新增的属性会被移除，避免把未知属性写入 EPUB。"""
        original = '<p><a href="https://example.com">DOI</a></p>'
        translated = '<p><a href="https://example.com" onclick="alert(1)" data-extra="x">数字对象标识</a></p>'

        normalized = normalize_translated_html_attributes(original, translated)
        is_valid, error = validate_translated_html(original, normalized)

        assert is_valid, error
        assert "onclick" not in normalized
        assert "data-extra" not in normalized

    def test_attribute_normalization_does_not_mask_missing_tags(self):
        """测试属性规范化不能掩盖标签缺失、子标签数量变化等真实结构错误。"""
        original = '<p><span>Alpha</span><a href="https://example.com">Beta</a></p>'
        translated = '<p><a href="https://example.com">贝塔</a></p>'

        normalized = normalize_translated_html_attributes(original, translated)
        is_valid, error = validate_translated_html(original, normalized)

        assert not is_valid
        assert "子标签数量不一致" in error

    def test_validate_reports_child_count_mismatch_as_structure_error(self):
        """测试子标签数量变化应报告为结构错误，而不是伪装成属性错误。"""
        original = '<p>Alpha</p><p><span>One</span><a id="idx"></a><a id="idx2"></a>Two</p>'
        translated = "<p>甲</p><p><span>一</span>二</p>"

        is_valid, error = validate_translated_html(original, translated)

        assert not is_valid
        assert "标签结构不一致" in error
        assert "子标签数量不一致" in error
        assert "标签属性不一致" not in error
