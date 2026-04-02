from unittest.mock import mock_open, patch

import pytest

from engine.epub.replacer import Replacer
from engine.schemas import Chunk, EpubItem


@pytest.fixture
def mock_epub_item(tmp_path):
    """创建一个包含模拟数据的 EpubItem 实例。"""
    item_path = tmp_path / "test_item.html"
    item_path.touch()

    return EpubItem(
        id="test_id",
        path=str(item_path),
        content="[id0]This is a [id1]test[id2].[id3]",
        translated=None,
        placeholder={"[id0]": "<p>", "[id1]": "<b>", "[id2]": "</b>", "[id3]": "</p>"},
        chunks=[
            Chunk(
                name="1",
                original="[id0]This is a [id1]test[id2].[id3]",
                translated="[id0]这是一个 [id1]测试[id2]。[id3]",
                tokens=7,
                global_indices=[0, 1, 2, 3],
                local_tag_map={"[id0]": "<p>", "[id1]": "<b>", "[id2]": "</b>", "[id3]": "</p>"}
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
        content="[PRE:0]<p>[id0]Hello[id1]</p>[CODE:0]",
        translated=None,
        placeholder={"[id0]": "<b>", "[id1]": "</b>"},
        chunks=[
            Chunk(
                name="1",
                original="[PRE:0]<p>[id0]Hello[id1]</p>[CODE:0]",
                translated="[PRE:0]<p>[id0]你好[id1]</p>[CODE:0]",
                tokens=10,
                global_indices=[0],
                local_tag_map={"[id0]": "<b>", "[id1]": "</b>"}
            ),
        ],
        preserved_pre=["<pre>function test() {}</pre>"],
        preserved_code=["<code>x = 1</code>"],
    )


class TestReplacer:
    """测试 epub/replacer.py 中的 Replacer 类。"""

    def test_restore_successful(self, mock_epub_item):
        """测试 restore 方法在正常情况下能否正确合并、还原并写入文件。"""
        merged_content = "[id0]这是一个 [id1]测试[id2]。[id3]"
        restored_content = "<p>这是一个 <b>测试</b>。</p>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch.object(Replacer, "_restore_tags", return_value=restored_content) as mock_restore_tags,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_tags.assert_called_once_with(mock_epub_item, merged_content)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(restored_content)

            assert mock_epub_item.translated == restored_content

    def test_restore_with_no_chunks(self, mock_epub_item):
        """测试当 EpubItem 没有分块时，restore 方法的行为。"""
        mock_epub_item.chunks = []

        with (
            patch.object(Replacer, "_merge_chunks", return_value="") as mock_merge_chunks,
            patch.object(Replacer, "_restore_tags", return_value="") as mock_restore_tags,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_tags.assert_called_once_with(mock_epub_item, "")

            mock_file.assert_not_called()

            assert mock_epub_item.translated is None

    def test_restore_with_no_placeholder(self, mock_epub_item):
        """测试当 EpubItem 没有占位符时，restore 方法的行为。"""
        mock_epub_item.placeholder = {}
        merged_content = "[id0]这是一个测试。[id1]"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch.object(Replacer, "_restore_tags", return_value=merged_content) as mock_restore_tags,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_tags.assert_called_once_with(mock_epub_item, merged_content)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(merged_content)

            assert mock_epub_item.translated == merged_content

    def test_restore_with_preserved_precode(self, mock_epub_item_with_precode):
        """测试带有 pre/code 标签的恢复。"""
        merged_content = "[PRE:0]<p>[id0]你好[id1]</p>[CODE:0]"
        # _restore_tags mock 返回已恢复 id 占位符的内容
        tags_restored_content = "[PRE:0]<p><b>你好</b></p>[CODE:0]"
        final_content = "<pre>function test() {}</pre><p><b>你好</b></p><code>x = 1</code>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch.object(Replacer, "_restore_tags", return_value=tags_restored_content) as mock_restore_tags,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item_with_precode)

            mock_merge_chunks.assert_called_once_with(mock_epub_item_with_precode)
            mock_restore_tags.assert_called_once_with(mock_epub_item_with_precode, merged_content)

            mock_file.assert_called_once_with(mock_epub_item_with_precode.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(final_content)

            assert mock_epub_item_with_precode.translated == final_content

    def test_restore_empty_content(self, mock_epub_item):
        """测试空内容的恢复。"""
        mock_epub_item.chunks = []

        with (
            patch.object(Replacer, "_merge_chunks", return_value="") as mock_merge_chunks,
            patch.object(Replacer, "_restore_tags", return_value="") as mock_restore_tags,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_file.assert_not_called()
            assert mock_epub_item.translated is None


class TestNavStructureValidation:
    """测试 Nav 文件结构验证"""

    def test_validate_nav_structure_valid(self):
        """验证有效的 nav 文件结构"""
        replacer = Replacer()
        valid_nav = '<navMap><navPoint><navLabel><text>Chapter</text></navLabel><content src="xhtml/c1.xhtml"/></navPoint></navMap>'
        assert replacer._validate_nav_structure(valid_nav) is True

    def test_validate_nav_structure_missing_navmap(self):
        """验证缺少 navMap 的无效 nav"""
        replacer = Replacer()
        invalid_nav = '<navPoint><navLabel><text>Chapter</text></navLabel></navPoint>'
        assert replacer._validate_nav_structure(invalid_nav) is False

    def test_validate_nav_structure_corrupted(self):
        """验证损坏的 nav 内容"""
        replacer = Replacer()
        corrupted = '<navMap></navMap>'
        assert replacer._validate_nav_structure(corrupted) is False

    def test_validate_nav_structure_missing_navpoint(self):
        """验证缺少 navPoint"""
        replacer = Replacer()
        invalid = '<navMap><navLabel><text>Chapter</text></navLabel></navMap>'
        assert replacer._validate_nav_structure(invalid) is False

    def test_validate_nav_structure_xhtml_format(self):
        """验证 XHTML 格式的 nav 文件（nav.xhtml 使用此格式）"""
        replacer = Replacer()
        valid_xhtml_nav = '<nav epub:type="toc"><ol><li><a href="c01.xhtml">Chapter 1</a></li></ol></nav>'
        assert replacer._validate_nav_structure(valid_xhtml_nav) is True

    def test_validate_nav_structure_xhtml_invalid(self):
        """验证 XHTML 格式但缺少必要标签"""
        replacer = Replacer()
        invalid_xhtml = '<nav epub:type="toc"><ol></ol></nav>'
        assert replacer._validate_nav_structure(invalid_xhtml) is False

    def test_item_content_is_original_not_placeholder(self, tmp_path):
        """验证 item.content 是原始 HTML 而不是占位符版本"""
        item_path = tmp_path / "test.xhtml"
        item_path.write_text('<p>Hello</p>')

        item = EpubItem(
            id="test.xhtml",
            path=str(item_path),
            content="<p>Hello</p>",
            placeholder={"[id0]": "<p>"},
            chunks=[],
        )
        # content 应该是原始 HTML，不是 "[id0]Hello" 这样的占位符版本
        assert item.content == "<p>Hello</p>"
        assert "[id" not in item.content
