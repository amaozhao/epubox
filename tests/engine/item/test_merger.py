from engine.item.merger import Merger
from engine.schemas import Chunk, TranslationStatus


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

    def test_merge_handles_no_language_attributes(self, caplog):
        """测试当合并内容中没有 lang 或 xml:lang 属性时，merge 方法是否记录警告日志。"""
        caplog.set_level("WARNING")
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
        assert "合并后的 XHTML 内容中未找到 lang 或 xml:lang 属性匹配 'en*'" in caplog.text

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
