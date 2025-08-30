import os
from unittest.mock import MagicMock, call, mock_open

import pytest

from engine.epub.parser import Parser
from engine.schemas import EpubBook


@pytest.fixture(autouse=True)
def setup_mocks(mocker):
    """
    为所有测试用例设置通用的 mock，以确保测试的独立性。
    """
    # 模拟 Chunker 类
    mocked_chunker = MagicMock()
    mocked_chunker.chunk.return_value = []  # 返回一个空列表，避免后续的 chunk 处理

    # 在所有测试用例中，当 Chunker 类被实例化时，都返回我们模拟的 Chunker 实例
    mocker.patch("engine.epub.parser.Chunker", return_value=mocked_chunker)

    # # 模拟 Replacer 类的实例
    mocked_replacer = MagicMock()

    # # 模拟 Replacer 实例的 placeholder 属性，并给它赋值
    mocked_replacer.placeholder = MagicMock()
    mocked_replacer.placeholder.placer_map = {"key": "value"}

    # # 模拟 Replacer 实例的 replace 方法，并设置其返回值
    mocked_replacer.replace.return_value = "processed content"

    # # 在所有测试用例中，当 Replacer 类被实例化时，都返回我们模拟的 Replacer 实例
    mocker.patch("engine.epub.parser.Replacer", return_value=mocked_replacer)


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

    def test_get_output_dir(self, parser_instance):
        """测试 _get_output_dir 方法是否正确生成解压路径。"""
        # Get the directory of the mocked epub_path
        epub_dir = os.path.dirname(parser_instance.path)
        # Construct the expected path using the same logic as your code
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

        mocker.patch("builtins.open", mock_open(read_data="<html><body>Test content.</body></html>"))

        book = parser_instance.parse()

        parser_instance.extract.assert_called_once()

        assert isinstance(book, EpubBook)
        assert book.name == "my_book"
        assert book.extract_path == parser_instance.output_dir
        assert len(book.items) == 1

        item = book.items[0]
        assert item.id == "chapter1.xhtml"
        assert item.content == "processed content"

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
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ("image.jpg", "style.css", "font.otf"))])

        book = parser_instance.parse()

        assert len(book.items) == 0
