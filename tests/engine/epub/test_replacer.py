from unittest.mock import mock_open, patch

import pytest

# 确保这里的导入路径与你的项目结构一致
from engine.epub.replacer import Replacer
from engine.schemas import Chunk, EpubItem


@pytest.fixture
def mock_epub_item(tmp_path):
    """
    创建一个包含模拟数据的 EpubItem 实例。
    """
    item_path = tmp_path / "test_item.html"
    item_path.touch()

    return EpubItem(
        id="test_id",
        path=str(item_path),
        content="<p>This is a <b>test</b>.</p><h2>Another heading.</h2>",
        translated=None,
        placeholder={"##abcde1##": "<b>test</b>"},
        chunks=[
            Chunk(
                name="1", original="<p>This is a ##abcde1##.</p>", translated="<p>这是一个 ##abcde1##。</p>", tokens=7
            ),
            Chunk(name="2", original="<h2>Another heading.</h2>", translated="<h2>另一个标题。</h2>", tokens=3),
        ],
    )


class TestReplacer:
    """
    测试 epub/replacer.py 中的 Replacer 类。
    """

    def test_restore_successful(self, mock_epub_item):
        """
        测试 restore 方法在正常情况下能否正确合并、还原并写入文件。
        """
        merged_content = "<p>这是一个 ##abcde1##。</p><h2>另一个标题。</h2>"
        restored_content = "<p>这是一个 <b>test</b>。</p><h2>另一个标题。</h2>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch.object(Replacer, "_restore_replacer", return_value=restored_content) as mock_restore_replacer,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_replacer.assert_called_once_with(mock_epub_item, merged_content)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(restored_content)

            assert mock_epub_item.translated == restored_content

    def test_restore_with_no_chunks(self, mock_epub_item):
        """
        测试当 EpubItem 没有分块时，restore 方法的行为。
        """
        # 将 chunks 设为空列表
        mock_epub_item.chunks = []

        with (
            patch.object(Replacer, "_merge_chunks", return_value="") as mock_merge_chunks,
            patch.object(Replacer, "_restore_replacer", return_value="") as mock_restore_replacer,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_replacer.assert_called_once_with(mock_epub_item, "")

            mock_file.assert_not_called()

            assert mock_epub_item.translated is None

    def test_restore_with_no_placeholder(self, mock_epub_item):
        """
        测试当 EpubItem 没有占位符时，restore 方法的行为。
        """
        # 清空 placeholder 字典
        mock_epub_item.placeholder = {}
        merged_content = "<p>这是一个 ##abcde1##。</p><h2>另一个标题。</h2>"

        with (
            patch.object(Replacer, "_merge_chunks", return_value=merged_content) as mock_merge_chunks,
            patch.object(Replacer, "_restore_replacer", return_value=merged_content) as mock_restore_replacer,
            patch("builtins.open", new_callable=mock_open) as mock_file,
        ):
            replacer = Replacer()
            replacer.restore(mock_epub_item)

            mock_merge_chunks.assert_called_once_with(mock_epub_item)
            mock_restore_replacer.assert_called_once_with(mock_epub_item, merged_content)

            mock_file.assert_called_once_with(mock_epub_item.path, "w", encoding="utf-8")
            mock_file().write.assert_called_once_with(merged_content)

            assert mock_epub_item.translated == merged_content
