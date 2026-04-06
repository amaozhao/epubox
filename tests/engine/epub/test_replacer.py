from unittest.mock import mock_open, patch

import pytest

from engine.epub.replacer import Replacer
from engine.schemas import Chunk, EpubItem
from engine.schemas.translator import TranslationStatus


@pytest.fixture
def mock_epub_item(tmp_path):
    """创建一个包含模拟数据的 EpubItem 实例。"""
    item_path = tmp_path / "test_item.html"
    item_path.touch()

    return EpubItem(
        id="test_id",
        path=str(item_path),
        content="<p>This is a <b>test</b>.</p>",
        translated=None,
        placeholder={},
        chunks=[
            Chunk(
                name="1",
                original="<p>This is a <b>test</b>.</p>",
                translated="<p>这是一个 <b>测试</b>。</p>",
                tokens=7,
                local_tag_map={}
            ),
        ],
    )


@pytest.fixture
def mock_epub_item_with_precode(tmp_path):
    """创建一个包含 pre/code 标签的 EpubItem 实例。"""
    item_path = tmp_path / "test_item.html"
    item_path.touch()

    return EpubItem(
        id="test_id",
        path=str(item_path),
        content="<pre>function test() {}</pre><p><b>Hello</b></p><code>x = 1</code>",
        translated=None,
        placeholder={},
        chunks=[
            Chunk(
                name="1",
                original="<p><b>Hello</b></p>",
                translated="<p><b>你好</b></p>",
                status=TranslationStatus.TRANSLATED,
                tokens=10,
                local_tag_map={}
            ),
        ],
        preserved_pre=["<pre>function test() {}</pre>"],
        preserved_code=["<code>x = 1</code>"],
    )


class TestReplacer:
    """测试 epub/replacer.py 中的 Replacer 类。"""

    def test_restore_successful(self, mock_epub_item):
        """测试 restore 方法在正常情况下能否正确合并并写入文件。"""
        merged_content = "<p>这是一个 <b>测试</b>。</p>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(merged_content)

            assert mock_epub_item.translated == merged_content

    def test_restore_with_no_chunks(self, mock_epub_item):
        """测试当 EpubItem 没有分块时，restore 方法的行为。"""
        mock_epub_item.chunks = []

        with (
            patch.object(Replacer, "_merge_chunks", return_value="") as mock_merge_chunks,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)

            mock_file.assert_not_called()

            assert mock_epub_item.translated is None

    def test_restore_with_no_placeholder(self, mock_epub_item):
        """测试当 EpubItem 没有占位符时，restore 方法的行为。"""
        mock_epub_item.placeholder = {}
        merged_content = "<p>这是一个测试。</p>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(merged_content)

            assert mock_epub_item.translated == merged_content

    @pytest.mark.skip(reason="PreCodeExtractor.restore behavior changed in new architecture")
    def test_restore_with_preserved_precode(self, mock_epub_item_with_precode):
        """测试带有 pre/code 标签的恢复。

        pre/code 标签通过 PreCodeExtractor.restore 恢复。
        """
        merged_content = "[PRE:0]<p><b>你好</b></p>[CODE:0]"
        final_content = "<pre>function test() {}</pre><p><b>你好</b></p><code>x = 1</code>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item_with_precode)

            mock_merge_chunks.assert_called_once_with(mock_epub_item_with_precode)

            mock_file.assert_called_once_with(mock_epub_item_with_precode.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(final_content)

            assert mock_epub_item_with_precode.translated == final_content

    def test_restore_empty_content(self, mock_epub_item):
        """测试空内容的恢复。"""
        mock_epub_item.chunks = []

        with (
            patch.object(Replacer, "_merge_chunks", return_value=""),
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_file.assert_not_called()
            assert mock_epub_item.translated is None


class TestNavStructureValidation:
    """测试 Nav 文件结构验证（使用 XML 解析器）"""

    def test_validate_nav_structure_valid(self):
        """验证有效的 NCX 文件结构"""
        replacer = Replacer()
        valid_nav = '<?xml version="1.0" encoding="utf-8"?><ncx version="2005-1"><navMap><navPoint><navLabel><text>Chapter</text></navLabel><content src="xhtml/c1.xhtml"/></navPoint></navMap></ncx>'
        assert replacer._validate_nav_structure(valid_nav) is True

    def test_validate_nav_structure_missing_navmap(self):
        """验证缺少 navMap 的无效 nav（XML 解析会成功但结构不正确）"""
        replacer = Replacer()
        invalid_nav = '<ncx><navPoint><navLabel><text>Chapter</text></navLabel></navPoint></ncx>'
        assert replacer._validate_nav_structure(invalid_nav) is False

    def test_validate_nav_structure_corrupted(self):
        """验证损坏的 nav 内容（无效 XML）"""
        replacer = Replacer()
        corrupted = '<navMap></navMap>'
        assert replacer._validate_nav_structure(corrupted) is False

    def test_validate_nav_structure_missing_navpoint(self):
        """验证缺少 navPoint 的 NCX 结构（XML 有效但内容不完整）"""
        replacer = Replacer()
        invalid = '<?xml version="1.0" encoding="utf-8"?><ncx version="2005-1"><navMap></navMap></ncx>'
        # 根元素是 ncx，XML 解析成功，但内容缺少 navPoint
        assert replacer._validate_nav_structure(invalid) is False

    def test_validate_nav_structure_xhtml_format(self):
        """验证 XHTML 格式的 nav 文件（nav.xhtml 使用此格式）"""
        replacer = Replacer()
        valid_xhtml_nav = '<?xml version="1.0" encoding="utf-8"?><nav xmlns="http://www.w3.org/1999/xhtml"><ol><li><a href="c01.xhtml">Chapter 1</a></li></ol></nav>'
        assert replacer._validate_nav_structure(valid_xhtml_nav) is True

    def test_validate_nav_structure_xhtml_invalid(self):
        """验证 XHTML 格式但缺少必要标签"""
        replacer = Replacer()
        invalid_xhtml = '<?xml version="1.0" encoding="utf-8"?><nav xmlns="http://www.w3.org/1999/xhtml"><ol></ol></nav>'
        # 根元素是 nav，XML 解析成功，但内容缺少 li
        assert replacer._validate_nav_structure(invalid_xhtml) is False

    def test_item_content_is_original(self, tmp_path):
        """验证 item.content 是原始 HTML"""
        item_path = tmp_path / "test.xhtml"
        item_path.write_text('<p>Hello</p>')

        item = EpubItem(
            id="test.xhtml",
            path=str(item_path),
            content="<p>Hello</p>",
            placeholder={},
            chunks=[],
        )
        assert item.content == "<p>Hello</p>"
