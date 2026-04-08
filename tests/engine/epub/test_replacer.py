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


class TestItemContent:
    """测试 EpubItem 内容相关"""

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
