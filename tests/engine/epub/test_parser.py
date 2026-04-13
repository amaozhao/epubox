import json
import os
from unittest.mock import MagicMock, call, mock_open

import pytest

from engine.epub.parser import Parser
from engine.schemas import Chunk, EpubBook
from engine.schemas.chunk import NavTextTarget
from engine.schemas.epub import CHECKPOINT_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def setup_mocks(mocker):
    """
    为所有测试用例设置通用的 mock，以确保测试的独立性。
    """
    # 模拟 DomChunker 类
    mocked_chunker = MagicMock()
    mocked_chunker.chunk.return_value = []

    mocker.patch("engine.epub.parser.DomChunker", return_value=mocked_chunker)


@pytest.fixture
def parser_instance():
    """创建一个 Parser 实例供测试使用。"""
    epub_path = "/path/to/my_book.epub"
    return Parser(path=epub_path)


class TestParser:
    """
    测试 Parser 类的所有功能。
    """

    def test_name_property(self, parser_instance):
        """测试 name 属性是否正确返回 EPUB 文件名。"""
        assert parser_instance.name == "my_book"

    @pytest.mark.parametrize(
        ("relative_path", "expected"),
        [
            ("OEBPS/toc.ncx", True),
            ("OEBPS/Text/nav.xhtml", True),
            ("OEBPS/Text/toc.xhtml", True),
            ("OEBPS/Text/chapter1.xhtml", False),
        ],
    )
    def test_is_nav_file(self, relative_path, expected):
        assert Parser._is_nav_file(relative_path) is expected

    def test_get_output_dir(self, parser_instance):
        """测试 _get_output_dir 方法是否正确生成解压路径。"""
        epub_dir = os.path.dirname(parser_instance.path)
        expected_path = os.path.join(epub_dir, "temp", "my_book")
        assert parser_instance._get_output_dir() == expected_path

    def test_extract_creates_dir_and_extracts_all_files(self, mocker, parser_instance):
        """测试当目录不存在时，extract 方法能创建目录并解压所有文件。"""
        mocker.patch("os.path.exists", return_value=False)
        mock_makedirs = mocker.patch("os.makedirs")
        mock_zipfile = mocker.patch("zipfile.ZipFile", autospec=True)
        zip_mock = mock_zipfile.return_value.__enter__.return_value

        zip_mock.infolist.return_value = [
            MagicMock(filename="OEBPS/", is_dir=lambda: True),
            MagicMock(filename="OEBPS/toc.ncx", is_dir=lambda: False),
            MagicMock(filename="OEBPS/chapter1.xhtml", is_dir=lambda: False),
        ]

        parser_instance.extract()

        expected_calls = [call(parser_instance.output_dir), call(os.path.join(parser_instance.output_dir, "OEBPS/"))]
        mock_makedirs.assert_has_calls(expected_calls, any_order=True)

        assert zip_mock.extract.call_count == 2
        zip_mock.extract.assert_any_call(zip_mock.infolist.return_value[1], parser_instance.output_dir)
        zip_mock.extract.assert_any_call(zip_mock.infolist.return_value[2], parser_instance.output_dir)

    def test_extract_skips_existing_files(self, mocker, parser_instance):
        """测试当文件已存在时，extract 方法能正确跳过。"""

        def mock_exists(path):
            return path.endswith("chapter1.xhtml") or path == parser_instance.output_dir

        mocker.patch("os.path.exists", side_effect=mock_exists)
        mocker.patch("os.makedirs")

        mock_zipfile = mocker.patch("zipfile.ZipFile", autospec=True)
        zip_mock = mock_zipfile.return_value.__enter__.return_value

        zip_mock.infolist.return_value = [
            MagicMock(filename="OEBPS/chapter1.xhtml", is_dir=lambda: False),
            MagicMock(filename="OEBPS/chapter2.xhtml", is_dir=lambda: False),
        ]

        parser_instance.extract()

        assert zip_mock.extract.call_count == 1
        zip_mock.extract.assert_called_once_with(zip_mock.infolist.return_value[1], parser_instance.output_dir)

    def test_parse_correctly_processes_files(self, mocker, parser_instance):
        """测试 parse 方法能正确解析文件、调用 replacer 并返回 EpubBook。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")

        mocker.patch(
            "os.walk",
            return_value=[
                (parser_instance.output_dir, (), ("chapter1.xhtml", "style.css", "container.xml")),
                (os.path.join(parser_instance.output_dir, "images"), (), ("cover.jpg",)),
            ],
        )

        original_html = "<html><body>Test content.</body></html>"
        mocker.patch("builtins.open", mock_open(read_data=original_html))

        book = parser_instance.parse()

        parser_instance.extract.assert_called_once()

        assert isinstance(book, EpubBook)
        assert book.name == "my_book"
        assert book.extract_path == parser_instance.output_dir
        assert len(book.items) == 1

        item = book.items[0]
        assert item.id == "chapter1.xhtml"
        # item.content 应该是原始 HTML，不应该是经过 TagPreserver 处理后的占位符版本
        assert item.content == original_html

        assert "container.xml" not in [i.id for i in book.items]

    @pytest.mark.parametrize("file_ext", [".xhtml", ".html", ".xml", ".ncx"])
    def test_parse_processes_translatable_file_types(self, mocker, parser_instance, file_ext):
        """测试 parse 方法能正确解析所有可翻译的文件类型。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), [f"chapter1{file_ext}"])])

        mocker.patch("builtins.open", mock_open(read_data="Test Content."))

        book = parser_instance.parse()

        assert len(book.items) == 1
        assert book.items[0].id == f"chapter1{file_ext}"

    def test_parse_skips_non_translatable_files(self, mocker, parser_instance):
        """测试 parse 方法能正确跳过非可翻译文件。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch(
            "os.walk", return_value=[(parser_instance.output_dir, (), ("image.jpg", "style.css", "font.otf"))]
        )

        book = parser_instance.parse()

        assert len(book.items) == 0

    def test_save_json_writes_checkpoint_schema_version(self, tmp_path):
        """测试保存 checkpoint 时会写入 schema version。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        book = EpubBook(name="my_book", path=str(epub_path), extract_path=str(tmp_path / "temp" / "my_book"))

        parser.save_json(book)

        payload = json.loads((tmp_path / "my_book.json").read_text(encoding="utf-8"))
        assert payload["checkpoint_schema_version"] == CHECKPOINT_SCHEMA_VERSION

    def test_load_json_rejects_incompatible_checkpoint_schema_version(self, tmp_path):
        """测试旧版 checkpoint 会快速失败，而不是被静默复用。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        checkpoint_path = tmp_path / "my_book.json"
        checkpoint_path.write_text(
            json.dumps(
                {
                    "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION - 1,
                    "name": "my_book",
                    "path": str(epub_path),
                    "extract_path": str(tmp_path / "temp" / "my_book"),
                    "items": [],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="checkpoint schema version"):
            parser.load_json()

    def test_load_json_upgrades_legacy_nav_chunks(self, tmp_path, mocker):
        """测试旧版导航 checkpoint 会被自动重建为 nav_text 模式。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        mocker.patch.object(
            parser,
            "_rebuild_nav_item_chunks",
            side_effect=lambda item: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="nav-text",
                        original="[NAVTXT:0] Chapter 1",
                        translated=None,
                        status="pending",
                        tokens=10,
                        chunk_mode="nav_text",
                        xpaths=[],
                        nav_targets=[
                            NavTextTarget(
                                marker="[NAVTXT:0]",
                                xpath="/ncx/navMap/navPoint/navLabel/text",
                                text_index=0,
                                original_text="Chapter 1",
                            )
                        ],
                    )
                ],
            ),
        )
        checkpoint_path = tmp_path / "my_book.json"
        checkpoint_path.write_text(
            json.dumps(
                {
                    "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "name": "my_book",
                    "path": str(epub_path),
                    "extract_path": str(tmp_path / "temp" / "my_book"),
                    "items": [
                        {
                            "id": "OEBPS/toc.ncx",
                            "path": str(tmp_path / "temp" / "my_book" / "OEBPS" / "toc.ncx"),
                            "content": "<ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>",
                            "chunks": [
                                {
                                    "name": "legacy-nav",
                                    "original": "<navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint>",
                                    "translated": None,
                                    "status": "pending",
                                    "tokens": 20,
                                    "xpaths": ["/ncx/navMap/navPoint"],
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        save_json = mocker.patch.object(parser, "save_json")

        book = parser.load_json()

        assert book is not None
        assert book.items[0].chunks[0].chunk_mode == "nav_text"
        assert book.items[0].chunks[0].nav_targets
        save_json.assert_called_once()
