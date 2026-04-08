from engine.item.merger import Merger
from engine.schemas import Chunk, TranslationStatus


class TestMergerPlaceholderRestore:
    """测试 Merger 在新架构下的行为（无占位符）"""

    def test_translated_with_html_tags_preserved(self):
        """translated 直接包含 HTML 标签，在 merge 时应该被直接使用"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p><em>Hello</em></p>",
                translated="<p><em>你好</em></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        result = merger.merge(chunks)
        # 直接就是正确 HTML
        assert "<em>" in result
        assert "</em>" in result
        assert "你好" in result

    def test_multiple_chunks_html_preserved(self):
        """多个 chunk 的 HTML 标签都应该被直接保留"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p><em>Hello</em></p>",
                translated="<p><em>你好</em></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="c2",
                original="<p><strong>World</strong></p>",
                translated="<p><strong>世界</strong></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        result = merger.merge(chunks)
        assert "<em>" in result
        assert "</em>" in result
        assert "<strong>" in result
        assert "</strong>" in result
        assert "你好" in result
        assert "世界" in result

    def test_untranslated_chunk_uses_original(self):
        """UNTRANSLATED chunk 应该使用 original"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p><em>Hello</em></p>",
                translated="<p><em>你好</em></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="c2",
                original="<p><strong>World</strong></p>",
                translated="",  # 翻译失败
                status=TranslationStatus.UNTRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        result = merger.merge(chunks)
        # Chunk 1 使用 translated，Chunk 2 使用 original
        assert "<em>" in result
        assert "<strong>" in result
        assert "World" in result


class TestMergerHtmlValidation:
    """测试 Merger 的 HTML 验证功能（集成测试）"""

    def test_valid_html_passes_validation(self):
        """正确的 HTML 结构应该通过验证"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p>Hello</p>",
                translated="<p>你好</p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="c2",
                original="<p>World</p>",
                translated="<p>世界</p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        result = merger.merge(chunks)
        # 应该正确合并
        assert "<p>" in result
        assert "</p>" in result
        assert "你好" in result
        assert "世界" in result

    def test_chunk_boundary_tag_tracking(self):
        """测试跨 chunk 边界的标签追踪"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p>Hello <em>",
                translated="<p>你好 <em>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="c2",
                original="text</em></p>",
                translated="文本</em></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        result = merger.merge(chunks)
        # 跨 chunk 的 <em>...</em> 应该正确配对
        assert "<em>" in result
        assert "</em>" in result
        assert "<p>" in result
        assert "</p>" in result


class TestMergerStatusHandling:
    """测试 Merger 对不同翻译状态的处理"""

    def test_merge_uses_original_when_untranslated(self):
        """UNTRANSLATED 状态的 chunk 应使用 original 而非 translated"""
        merger = Merger()
        chunks = [
            Chunk(name="c1", original="<p>Hello</p>", translated="<p>你好</p>", status=TranslationStatus.TRANSLATED, tokens=5),
            Chunk(name="c2", original="<p>World</p>", translated="", status=TranslationStatus.UNTRANSLATED, tokens=5),
        ]
        result = merger.merge(chunks)
        assert result == "<p>你好</p><p>World</p>"

    def test_merge_uses_original_when_translated_empty(self):
        """translated 为空字符串时使用 original"""
        merger = Merger()
        chunks = [
            Chunk(name="c1", original="<p>Hello</p>", translated="", status=TranslationStatus.TRANSLATED, tokens=5),
        ]
        result = merger.merge(chunks)
        assert result == "<p>Hello</p>"

    def test_merge_normal_case(self):
        """正常翻译成功的 chunk 使用 translated"""
        merger = Merger()
        chunks = [
            Chunk(name="c1", original="<p>Hello</p>", translated="<p>你好</p>", status=TranslationStatus.TRANSLATED, tokens=5),
            Chunk(name="c2", original="<p>World</p>", translated="<p>世界</p>", status=TranslationStatus.TRANSLATED, tokens=5),
        ]
        result = merger.merge(chunks)
        assert result == "<p>你好</p><p>世界</p>"


class TestMerger:
    """
    测试 Merger 类的所有功能。
    """

    def test_merge_combines_translated_chunks_and_updates_language(self):
        """测试 merge 方法能否正确合并 chunks 的 translated 内容并更新语言属性。"""
        merger = Merger()
        chunks = [
            Chunk(
                name="chunk1",
                original="Hello",
                translated='<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>Hello</body></html>',
                status=TranslationStatus.COMPLETED,
                tokens=10,
            ),
            Chunk(
                name="chunk2",
                original="World",
                translated='<p lang="en" xml:lang="en">World</p>',
                status=TranslationStatus.COMPLETED,
                tokens=5,
            ),
            Chunk(
                name="chunk3",
                original="Test",
                translated='<div lang="en-UK" xml:lang="en-UK">Test</div>',
                status=TranslationStatus.COMPLETED,
                tokens=8,
            ),
            Chunk(
                name="chunk4",
                original="Long",
                translated='<span lang="en-AUSTRALIA" xml:lang="en-AUSTRALIA">Long</span>',
                status=TranslationStatus.COMPLETED,
                tokens=12,
            ),
        ]
        result = merger.merge(chunks, language="zh")
        assert (
            '<html lang="zh" xml:lang="zh" xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>Hello</body></html>'
            in result
        )
        assert '<p lang="zh" xml:lang="zh">World</p>' in result
        assert '<div lang="zh" xml:lang="zh">Test</div>' in result
        assert '<span lang="zh" xml:lang="zh">Long</span>' in result
        assert 'lang="en' not in result
        assert 'xml:lang="en' not in result

    def test_merge_reconstructs_html_wrapper_from_fragments(self):
        """测试当 chunks 是 HTML 片段（缺少 <html> 包裹）时，merge 能重建完整结构。"""
        merger = Merger()
        # 模拟 toc.xhtml 被分割成多个片段：head、nav 等
        chunks = [
            Chunk(
                name="/div/html[1]/head[1]",
                original='<head>\n<title>Contents</title>\n</head>',
                translated='<head>\n<title>目录</title>\n</head>',
                status=TranslationStatus.TRANSLATED,
                tokens=11,
            ),
            Chunk(
                name="/div/html[1]/body[1]/nav[1]",
                original='<nav id="toc"><h2>Contents</h2><ol><li><a href="ch01.xhtml">Chapter 1</a></li></ol></nav>',
                translated='<nav id="toc"><h2>目录</h2><ol><li><a href="ch01.xhtml">第一章</a></li></ol></nav>',
                status=TranslationStatus.TRANSLATED,
                tokens=65,
            ),
        ]
        # 模拟真实场景：传入完整的 original_content（replacer 会传 item.content）
        original_content = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en-US" xml:lang="en-US">
<head>
<title>Contents</title>
</head>
<body>
<nav id="toc"><h2>Contents</h2><ol><li><a href="ch01.xhtml">Chapter 1</a></li></ol></nav>
</body>
</html>'''
        result = merger.merge(chunks, original_content=original_content)

        # 验证重建了完整的 <html> 包裹（可能以 <?xml 开头）
        assert "<html " in result
        assert "</html>" in result
        # 验证翻译内容
        assert "目录" in result
        assert "第一章" in result

    def test_merge_reconstructs_html_with_namespace_attributes(self):
        """测试重建 HTML 时保留 xmlns 和 epub 命名空间属性。"""
        merger = Merger()
        chunks = [
            Chunk(
                name="/div/html[1]/head[1]",
                original='<head><title>Test</title></head>',
                translated='<head><title>测试</title></head>',
                status=TranslationStatus.TRANSLATED,
                tokens=8,
            ),
            Chunk(
                name="/div/html[1]/body[1]/p[1]",
                original='<p>Hello</p>',
                translated='<p>你好</p>',
                status=TranslationStatus.TRANSLATED,
                tokens=5,
            ),
        ]
        # 传入完整的 original_content 以便提取 xmlns 属性
        original_content = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Test</title></head><body><p>Hello</p></body></html>'
        result = merger.merge(chunks, original_content=original_content)

        # 验证 xmlns 属性被保留
        assert 'xmlns="http://www.w3.org/1999/xhtml"' in result
        # 验证翻译
        assert "测试" in result
        assert "你好" in result

    def test_merge_handles_empty_chunks(self):
        """测试 merge 方法处理空 chunks 列表时返回空字符串。"""
        merger = Merger()
        result = merger.merge([])
        assert result == ""

    def test_merge_does_not_add_head_when_original_has_none(self):
        """测试当原文没有 <head> 时，merge 不会错误添加。"""
        merger = Merger()
        # 模拟一个没有 head 的 HTML 片段
        chunks = [
            Chunk(
                name="c1",
                original="<html xmlns=\"http://www.w3.org/1999/xhtml\"><body><p>Hello</p></body></html>",
                translated="<html xmlns=\"http://www.w3.org/1999/xhtml\"><body><p>你好</p></body></html>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
            ),
        ]
        result = merger.merge(chunks)
        # 原文没有 <head>，翻译后也不应该有
        assert "<p>你好</p>" in result
        # 不应该有 <head></head> 被添加
        assert "<head></head>" not in result

    def test_merge_handles_no_language_attributes(self):
        """测试当合并内容中没有 lang 或 xml:lang 属性时，merge 方法仍能正常返回。"""
        merger = Merger()
        chunks = [
            Chunk(
                name="chunk1",
                original="Hello",
                translated="<html><body>Hello</body></html>",
                status=TranslationStatus.COMPLETED,
                tokens=10,
            )
        ]
        result = merger.merge(chunks, language="zh")
        assert result == "<html><body>Hello</body></html>"

    def test_merge_with_custom_language(self):
        """测试 merge 方法使用自定义语言代码时能否正确更新属性。"""
        merger = Merger()
        chunks = [
            Chunk(
                name="chunk1",
                original="Hello",
                translated='<html lang="en-US" xml:lang="en-US" xmlns="http://www.w3.org/1999/xhtml"><body>Hello</body></html>',
                status=TranslationStatus.COMPLETED,
                tokens=10,
            )
        ]
        result = merger.merge(chunks, language="fr")
        assert '<html lang="fr" xml:lang="fr" xmlns="http://www.w3.org/1999/xhtml"><body>Hello</body></html>' in result
        assert 'lang="en' not in result
        assert 'xml:lang="en' not in result

    def test_merge_handles_partial_language_attributes(self):
        """测试 merge 方法处理只有 lang 或 xml:lang 属性的情况。"""
        merger = Merger()
        chunks = [
            Chunk(
                name="chunk1",
                original="Hello",
                translated='<html lang="en-US"><body>Hello</body></html>',
                status=TranslationStatus.COMPLETED,
                tokens=10,
            ),
            Chunk(
                name="chunk2",
                original="World",
                translated='<html xml:lang="en-UK"><body>World</body></html>',
                status=TranslationStatus.COMPLETED,
                tokens=8,
            ),
        ]
        result = merger.merge(chunks, language="zh")
        assert '<html lang="zh"><body>Hello</body></html>' in result
        assert '<html xml:lang="zh"><body>World</body></html>' in result
        assert 'lang="en' not in result
        assert 'xml:lang="en' not in result

    def test_merge_preserves_doctype_when_lost_in_translation(self):
        """测试当 LLM 翻译丢失 DOCTYPE 时，merge 能从原文恢复"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original='<!DOCTYPE html><html lang="en"><body><p>Hello</p></body></html>',
                translated='<html lang="zh"><body><p>你好</p></body></html>',  # LLM 丢失了 DOCTYPE
                status=TranslationStatus.TRANSLATED,
                tokens=10,
            ),
        ]
        result = merger.merge(chunks, original_content='<!DOCTYPE html><html lang="en"><body><p>Hello</p></body></html>')
        assert "<!DOCTYPE html>" in result, f"DOCTYPE should be preserved. Got: {result[:100]}"

    def test_merge_preserves_doctype_and_xml_declaration(self):
        """测试当原文有 DOCTYPE 和 XML 声明时，翻译后都能保留"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original='<html lang="en"><body><p>Hello</p></body></html>',
                translated='<html lang="zh"><body><p>你好</p></body></html>',  # LLM 丢失了 XML 声明和 DOCTYPE
                status=TranslationStatus.TRANSLATED,
                tokens=10,
            ),
        ]
        original_content = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE html>
<html lang="en"><body><p>Hello</p></body></html>'''
        result = merger.merge(chunks, original_content=original_content)
        assert "<!DOCTYPE html>" in result, f"DOCTYPE should be preserved. Got: {result[:100]}"
        assert '<?xml version="1.0"' in result, f"XML declaration should be preserved. Got: {result[:100]}"

    def test_merge_ncx_with_unclosed_meta_tags_fixed(self):
        """测试 NCX 文件中未闭合的 meta 标签被自动修复（来自真实数据）"""
        merger = Merger()
        # 模拟 toc.ncx 的 chunks，translated 中 meta 标签没有闭合
        chunks = [
            Chunk(
                name="/div/ncx[1]/head[1]",
                original='<head>\n\t<meta name="dtb:uid" content="urn:uuid:90938ff6-9eb4-4854-80de-07e95790fe9e" />\n\t<meta name="dtb:depth" content="0" />\n</head>',
                translated='<head>\n\t<meta name="dtb:uid" content="urn:uuid:90938ff6-9eb4-4854-80de-07e95790fe9e">\n\t<meta name="dtb:depth" content="0">\n</head>',
                status=TranslationStatus.TRANSLATED,
                tokens=50,
                local_tag_map={},
            ),
            Chunk(
                name="/div/ncx[1]/docTitle[1]",
                original='<docTitle>\n\t<text>Ship an MCP Server in Python</text>\n</docTitle>',
                translated='<doctitle>\n\t<text>用 Python 快速构建 MCP 服务器</text>\n</doctitle>',
                status=TranslationStatus.TRANSLATED,
                tokens=20,
                local_tag_map={},
            ),
        ]
        # 真实的 original_content 来自 toc.ncx
        original_content = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
\t<head>
\t\t<meta name="dtb:uid" content="urn:uuid:90938ff6-9eb4-4854-80de-07e95790fe9e" />
\t\t<meta name="dtb:depth" content="0" />
\t</head>
\t<docTitle>
\t\t<text>Ship an MCP Server in Python</text>
\t</docTitle>
</ncx>'''
        result = merger.merge(chunks, original_content=original_content)
        # 验证 XML 声明保留
        assert '<?xml version="1.0"' in result, f"XML declaration should be preserved. Got: {result[:100]}"
        # 验证 meta 标签被修复为自闭合
        assert '<meta name="dtb:uid" content="urn:uuid:90938ff6-9eb4-4854-80de-07e95790fe9e"/>' in result, f"meta should be self-closed. Got: {result}"
        # 验证翻译内容保留
        assert "用 Python 快速构建 MCP 服务器" in result, f"Translation should be preserved. Got: {result}"

    def test_merge_void_elements_all_fixed(self):
        """测试所有 void 元素未闭合时都能被修复"""
        merger = Merger()
        # 测试所有 void 元素
        void_test_cases = [
            ("img", '<img src="image/cover.png" alt="Cover">', '<img src="image/cover.png" alt="Cover"/>'),
            ("link", '<link href="css/style.css" rel="stylesheet">', '<link href="css/style.css" rel="stylesheet"/>'),
            ("meta", '<meta name="keywords" content="python,mcp">', '<meta name="keywords" content="python,mcp"/>'),
            ("br", '<br>', '<br />'),  # 修复后有空格
            ("hr", '<hr>', '<hr />'),  # 修复后有空格
            ("input", '<input type="text" name="username">', '<input type="text" name="username"/>'),
            ("area", '<area shape="rect" coords="0,0,100,100" href="page.html">', '<area shape="rect" coords="0,0,100,100" href="page.html"/>'),
            ("base", '<base href="https://example.com">', '<base href="https://example.com"/>'),
            ("col", '<col>', '<col />'),  # 修复后有空格
            ("embed", '<embed src="video.mp4" type="video/mp4">', '<embed src="video.mp4" type="video/mp4"/>'),
            ("param", '<param name="autoplay" value="true">', '<param name="autoplay" value="true"/>'),
            ("source", '<source src="audio.mp3" type="audio/mpeg">', '<source src="audio.mp3" type="audio/mpeg"/>'),
            ("track", '<track kind="subtitles" src="subs.vtt">', '<track kind="subtitles" src="subs.vtt"/>'),
            ("wbr", '<wbr>', '<wbr />'),  # 修复后有空格
        ]

        for tag, unclosed, expected in void_test_cases:
            chunks = [
                Chunk(
                    name="c1",
                    original=f'<p>before</p>',
                    translated=f'<p>before</p>{unclosed}<p>after</p>',
                    status=TranslationStatus.TRANSLATED,
                    tokens=10,
                    local_tag_map={},
                ),
            ]
            result = merger.merge(chunks, original_content='<html><body></body></html>')
            assert expected in result, f"{tag}: expected {expected} in result, got: {result[:200]}"

    def test_merge_pagebreak_div_self_closing(self):
        """测试 pagebreak div 被修复为自闭合形式（Apple Books 要求）"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original='<div id="page1" role="doc-pagebreak" aria-label="1" epub:type="pagebreak" />',
                translated='<div aria-label="1" epub:type="pagebreak" id="page1" role="doc-pagebreak"></div><p>Page 1 content</p>',
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="c2",
                original='<div id="page2" role="doc-pagebreak" aria-label="2" epub:type="pagebreak" />',
                translated='<div aria-label="2" epub:type="pagebreak" id="page2" role="doc-pagebreak"></div><p>Page 2 content</p>',
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
        ]
        original_content = '<html><body></body></html>'
        result = merger.merge(chunks, original_content=original_content)

        # 验证 pagebreak div 被修复为自闭合形式
        assert '<div aria-label="1" epub:type="pagebreak" id="page1" role="doc-pagebreak"/>' in result
        assert '<div aria-label="2" epub:type="pagebreak" id="page2" role="doc-pagebreak"/>' in result
        # 确保不是非自闭合形式
        assert 'aria-label="1" epub:type="pagebreak" id="page1" role="doc-pagebreak"></div>' not in result
        assert 'aria-label="2" epub:type="pagebreak" id="page2" role="doc-pagebreak"></div>' not in result


        """测试 HTML 文件保留 DOCTYPE 和 XML 声明（来自真实数据 cover.xhtml）"""
        merger = Merger()
        chunks = [
            Chunk(
                name="/div/html[1]/head[1]",
                original='<head>\n\t<title>Cover</title>\n</head>',
                translated='<head>\n<title>封面</title>\n</head>',
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},
            ),
            Chunk(
                name="/div/html[1]/body[1]/figure[1]",
                original='<figure style="text-align:center;" epub:type="cover">\n\t\t<img src="image/1.png" alt="Cover" />\n\t</figure>',
                translated='<figure epub:type="cover" style="text-align:center;">\n<img alt="封面图片" role="doc-cover" src="image/1.png" style="max-width:100%;">\n</figure>',
                status=TranslationStatus.TRANSLATED,
                tokens=30,
                local_tag_map={},
            ),
        ]
        # 真实的 original_content 来自 cover.xhtml
        original_content = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en-US" xml:lang="en-US" xmlns:epub="http://www.idpf.org/2007/ops">
\t<head>
\t\t<title>Cover</title>
\t</head>
\t<body>
\t\t<figure style="text-align:center;" epub:type="cover">
\t\t\t<img src="image/1.png" alt="Cover" />
\t\t</figure>
\t</body>
</html>'''
        result = merger.merge(chunks, original_content=original_content)
        # 验证 XML 声明保留
        assert '<?xml version="1.0"' in result, f"XML declaration should be preserved. Got: {result[:150]}"
        # 验证 DOCTYPE 保留
        assert "<!DOCTYPE html>" in result, f"DOCTYPE should be preserved. Got: {result[:150]}"
        # 验证 html 标签保留（带 lang 属性已翻译）
        assert "<html" in result, f"html tag should be present. Got: {result[:150]}"
        # 验证翻译内容保留
        assert "封面" in result, f"Translation should be preserved. Got: {result}"
