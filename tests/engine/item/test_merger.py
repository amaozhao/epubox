from engine.item.merger import Merger
from engine.schemas import Chunk, TranslationStatus


class TestMergerPlaceholderRestore:
    """测试 Merger 对占位符的恢复（集成测试）"""

    def test_translated_with_placeholders_restored(self):
        """translated 包含占位符，在 merge 时应该被恢复为实际标签"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p><em>Hello</em></p>",
                translated="<p><em>你好</em></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={},  # 没有占位符，因为 original 中没有 [idN]
            ),
        ]
        result = merger.merge(chunks)
        # 直接就是正确 HTML
        assert "<em>" in result
        assert "</em>" in result
        assert "你好" in result

    def test_translated_with_inline_placeholders(self):
        """translated 包含占位符，在 merge 时应该被恢复为实际标签"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p>[id0]Hello[id1]</p>",
                translated="<p>[id0]你好[id1]</p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                # [id0] = <em> 开头, [id1] = </em> 结尾
                local_tag_map={"[id0]": "<em>", "[id1]": "</em>"},
            ),
        ]
        result = merger.merge(chunks)
        # 占位符应该被恢复为实际标签
        assert "<em>" in result
        assert "</em>" in result
        assert "[id0]" not in result
        assert "[id1]" not in result
        assert "你好" in result

    def test_multiple_chunks_placeholders_restored(self):
        """多个 chunk 的占位符都应该被正确恢复"""
        merger = Merger()
        chunks = [
            Chunk(
                name="c1",
                original="<p>[id0]Hello[id1]</p>",
                translated="<p>[id0]你好[id1]</p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                # [id0] = <em> 开头, [id1] = </em> 结尾 - 成对出现
                local_tag_map={"[id0]": "<em>", "[id1]": "</em>"},
            ),
            Chunk(
                name="c2",
                original="<p>[id0]World[id1]</p>",
                translated="<p>[id0]世界[id1]</p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={"[id0]": "<strong>", "[id1]": "</strong>"},
            ),
        ]
        result = merger.merge(chunks)
        assert "[id0]" not in result
        assert "[id1]" not in result
        assert "<em>" in result
        assert "</em>" in result
        assert "<strong>" in result
        assert "</strong>" in result
        assert "你好" in result
        assert "世界" in result

    def test_untranslated_chunk_uses_original_with_restore(self):
        """UNTRANSLATED chunk 应该使用 original，并恢复其占位符"""
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
        # Chunk 1 使用 original
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

    def test_merge_handles_empty_chunks(self):
        """测试 merge 方法处理空 chunks 列表时返回空字符串。"""
        merger = Merger()
        result = merger.merge([])
        assert result == ""

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
