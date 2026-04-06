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
    # 模拟 TagPreserver 类
    mocked_preserver = MagicMock()
    mocked_preserver.preserve_tags.return_value = ("", MagicMock(tag_map={}, counter=0))

    mocker.patch("engine.epub.parser.TagPreserver", return_value=mocked_preserver)

    # 模拟 HtmlChunker 类
    mocked_chunker = MagicMock()
    mocked_chunker.chunk.return_value = []

    mocker.patch("engine.epub.parser.HtmlChunker", return_value=mocked_chunker)


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
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ("image.jpg", "style.css", "font.otf"))])

        book = parser_instance.parse()

        assert len(book.items) == 0


class TestParserChunkValidation:
    """测试 chunk 拆分后的 local_tag_map 验证逻辑（Step 5.1）"""

    def test_parse_valid_chunks_no_warning(self, mocker):
        """验证结构正确的 chunks 不会产生警告"""
        parser = Parser(path="/path/to/test.epub")
        mocker.patch.object(parser, "load_json", return_value=None)
        mocker.patch.object(parser, "save_json")
        mocker.patch.object(parser, "extract")

        # Mock os.walk 返回一个有效的 xhtml 文件
        mocker.patch(
            "os.walk",
            return_value=[(parser.output_dir, (), ("chapter1.xhtml",))],
        )

        # 有效的 HTML：<p>...</p> 配对正确
        valid_html = "<html><body><p>Hello</p></body></html>"
        mocker.patch("builtins.open", mock_open(read_data=valid_html))

        # Mock PreCodeExtractor
        mock_pre_extractor = MagicMock()
        mock_pre_extractor.extract.return_value = valid_html
        mock_pre_extractor.preserved_pre = []
        mock_pre_extractor.preserved_code = []
        mock_pre_extractor.preserved_style = []
        mocker.patch("engine.epub.parser.PreCodeExtractor", return_value=mock_pre_extractor)

        # Mock HtmlChunker 返回两个有效 chunk
        mock_chunker = MagicMock()
        mock_chunker.chunk_by_html_tags.return_value = [
            "<p>Hello</p>",
            "<p>World</p>",
        ]
        mocker.patch("engine.epub.parser.HtmlChunker", return_value=mock_chunker)

        # Mock count_tokens 避免真实调用
        mocker.patch("engine.epub.parser.count_tokens", return_value=10)

        # Mock TagPreserver 返回正确的 local_tag_map
        call_count = [0]
        def mock_preserve_tags(text):
            call_count[0] += 1
            # 模拟占位符映射
            if call_count[0] == 1:
                return ("[id0]Hello[id1]", MagicMock(tag_map={"[id0]": "<p>", "[id1]": "</p>"}))
            else:
                return ("[id0]World[id1]", MagicMock(tag_map={"[id0]": "<p>", "[id1]": "</p>"}))
        mock_preserver = MagicMock()
        mock_preserver.preserve_tags.side_effect = mock_preserve_tags
        mocker.patch("engine.epub.parser.TagPreserver", return_value=mock_preserver)

        # Spy on logger.warning
        mock_logger = mocker.patch("engine.epub.parser.logger")

        book = parser.parse()

        # 验证生成了 2 个 chunk
        assert len(book.items) == 1
        assert len(book.items[0].chunks) == 2
        # 验证没有警告（local_tag_map 正确且栈验证通过）
        warning_calls = [c for c in mock_logger.warning.call_args_list if "拆分后 HTML 结构异常" in str(c)]
        assert len(warning_calls) == 0

    def test_parse_invalid_chunks_logs_warning(self, mocker):
        """验证结构错误的 chunks 会产生警告"""
        parser = Parser(path="/path/to/test.epub")
        mocker.patch.object(parser, "load_json", return_value=None)
        mocker.patch.object(parser, "save_json")
        mocker.patch.object(parser, "extract")

        mocker.patch(
            "os.walk",
            return_value=[(parser.output_dir, (), ("chapter1.xhtml",))],
        )

        # HTML 本身是有效的，但 chunk 边界分割会破坏标签配对
        html_with_open_p = "<html><body><p>Hello"
        mocker.patch("builtins.open", mock_open(read_data=html_with_open_p))

        mock_pre_extractor = MagicMock()
        mock_pre_extractor.extract.return_value = html_with_open_p
        mock_pre_extractor.preserved_pre = []
        mock_pre_extractor.preserved_code = []
        mock_pre_extractor.preserved_style = []
        mocker.patch("engine.epub.parser.PreCodeExtractor", return_value=mock_pre_extractor)

        # 模拟 chunker 在 </p> 后分割，然后又遇到 <p>（标签顺序颠倒）
        # 例如：chunk 0 = "Hello</p>"，chunk 1 = "<p>World"
        mock_chunker = MagicMock()
        mock_chunker.chunk_by_html_tags.return_value = [
            "Hello</p>",
            "<p>World",
        ]
        mocker.patch("engine.epub.parser.HtmlChunker", return_value=mock_chunker)

        # Mock count_tokens 避免真实调用
        mocker.patch("engine.epub.parser.count_tokens", return_value=10)

        # chunk 0: [id0] = </p>, chunk 1: [id0] = <p>
        # restore 后: chunk0 = "Hello</p>", chunk1 = "<p>World"
        # 验证时：chunk0 遇到 </p> 但栈为空 → unexpected_close error
        call_count = [0]
        def mock_preserve_tags(text):
            call_count[0] += 1
            if call_count[0] == 1:
                return ("Hello[id0]", MagicMock(tag_map={"[id0]": "</p>"}))
            else:
                return ("[id0]World", MagicMock(tag_map={"[id0]": "<p>"}))
        mock_preserver = MagicMock()
        mock_preserver.preserve_tags.side_effect = mock_preserve_tags
        mocker.patch("engine.epub.parser.TagPreserver", return_value=mock_preserver)

        mock_logger = mocker.patch("engine.epub.parser.logger")

        book = parser.parse()

        # 应该有警告：chunk0 遇到 </p> 时栈为空（unexpected_close）
        warning_calls = [c for c in mock_logger.warning.call_args_list if "拆分后 HTML 结构异常" in str(c)]
        assert len(warning_calls) == 1

