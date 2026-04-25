import json
import os
from unittest.mock import MagicMock, call, mock_open

import pytest

from engine.epub.parser import Parser
from engine.item.chunker import DomChunker
from engine.schemas import Chunk, EpubBook, EpubItem, TranslationStatus
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


def require_chunks(item: EpubItem) -> list[Chunk]:
    assert item.chunks is not None
    return item.chunks


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

    @pytest.mark.parametrize(
        ("relative_path", "html", "expected"),
        [
            (
                "OEBPS/toc01.xml",
                "<ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>",
                True,
            ),
            (
                "OEBPS/content.opf",
                "<package><metadata><dc:title>Book</dc:title></metadata></package>",
                False,
            ),
            (
                "OEBPS/Text/ch1.xhtml",
                '<html><body><nav epub:type="toc"><ol><li><a href="#c1">Chapter 1</a></li></ol></nav></body></html>',
                True,
            ),
        ],
    )
    def test_is_nav_document(self, relative_path, html, expected):
        assert Parser._is_nav_document(relative_path, html) is expected

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ('<html><body><nav class="toc"><a href="#c1">Chapter 1</a></nav></body></html>', True),
            ('<html><body><nav epub:type="toc"><a href="#c1">Chapter 1</a></nav></body></html>', True),
            ('<html><body><nav><a href="#c1">Chapter 1</a></nav></body></html>', False),
        ],
    )
    def test_has_embedded_toc_nav(self, html, expected):
        assert Parser._has_embedded_toc_nav(html) is expected

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
        assert item.source_html_valid is True
        assert item.source_html_errors == []

        assert "container.xml" not in [i.id for i in book.items]

    def test_parse_navigation_xhtml_produces_nav_chunks(self, mocker, parser_instance):
        """测试 navigation.xhtml 会被真正解析为 nav_text chunks，而不是空 chunks。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch("engine.epub.parser.DomChunker", DomChunker)
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ["navigation.xhtml"])])

        original_html = """
        <html><body>
          <section aria-label="chapter opening">
            <nav class="tocList" epub:type="toc" id="toc" role="doc-toc">
              <h1>Table of Contents</h1>
              <ol>
                <li><a href="c01.xhtml">1 Linear Modeling for Two-Dimensional Data</a></li>
                <li><a href="c02.xhtml">2 Multidimensional Data Analysis</a></li>
                <li><a href="c03.xhtml">3 Introduction to Automatic Classification</a></li>
                <li><a href="c04.xhtml">4 Linear Programming</a></li>
                <li><a href="c05.xhtml">5 Elements of Graph Theory</a></li>
                <li><a href="c06.xhtml">6 Path Optimization</a></li>
              </ol>
            </nav>
          </section>
        </body></html>
        """
        mocker.patch("builtins.open", mock_open(read_data=original_html))

        book = parser_instance.parse()

        item = book.items[0]
        assert item.id == "navigation.xhtml"
        assert item.chunks
        assert item.chunks[0].chunk_mode == "nav_text"
        assert item.chunks[0].nav_targets

    def test_parse_navigation_xhtml_preserves_inline_code_placeholders(self, mocker, parser_instance):
        """测试导航文档中的 code 文本会被占位符保护，避免目录标题里的命令被翻译。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch("engine.epub.parser.DomChunker", DomChunker)
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ["navigation.xhtml"])])

        original_html = """
        <html><body>
          <nav class="tocList" epub:type="toc" id="toc" role="doc-toc">
            <ol>
              <li><a href="c01.xhtml"><code>pip install epubox</code> quick start</a></li>
            </ol>
          </nav>
        </body></html>
        """
        mocker.patch("builtins.open", mock_open(read_data=original_html))

        book = parser_instance.parse()

        item = book.items[0]
        assert item.preserved_code == ["<code>pip install epubox</code>"]
        assert item.chunks
        assert "[CODE:0]" in item.chunks[0].original

    def test_parse_prose_chapter_does_not_collapse_to_title_only_chunk(self, mocker, parser_instance):
        """测试带公式图片的正文页不会被 PRE 提取误伤到只剩 title chunk。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch("engine.epub.parser.DomChunker", DomChunker)
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ["c04.xhtml"])])

        original_html = """
        <html><body>
          <section aria-labelledby="c04" epub:type="chapter" role="doc-chapter">
            <header><h1 id="c04">4 Linear Programming</h1></header>
            <aside>
              <section class="feature2">
                <h2>CONCEPTS COVERED IN THIS CHAPTER.–</h2>
                <p>Linear programming is a fundamental tool for optimizing functions subject to constraints.</p>
                <p>This chapter provides a detailed exploration of this technique, beginning with an introductory example.</p>
                <p>References: [BRO 82], [DES 76], [FAU 74].</p>
              </section>
            </aside>
            <section aria-labelledby="sec4-1">
              <h2 id="sec4-1">4.1. An introductory example</h2>
              <p>The mathematical formulation of the problem is described as follows:</p>
              <ol class="decimal">
                <li>Objective function:
                  <div class="informalEquation"><img alt="image" src="images/eqpg111-1.png"/></div>
                </li>
                <li>Constraints:
                  <ul class="hyphen">
                    <li>Machine A: takes <img alt="image" src="images/i111-1.png"/> hours.</li>
                    <li>Machine B: requires <img alt="image" src="images/i111-2.png"/> hours.</li>
                  </ul>
                </li>
              </ol>
              <p>The problem can be solved graphically because both the objective function and the constraints are linear.</p>
            </section>
          </section>
        </body></html>
        """
        mocker.patch("builtins.open", mock_open(read_data=original_html))

        book = parser_instance.parse()

        item = book.items[0]
        assert item.id == "c04.xhtml"
        assert item.chunks
        assert any("/html/body/section" in xpath for chunk in item.chunks for xpath in (chunk.xpaths or []))
        assert not all((chunk.xpaths or []) == ["/html/head/title"] for chunk in item.chunks)

    def test_parse_complex_chapter_uses_conservative_chunk_limit(self, mocker, parser_instance):
        """复杂章节应使用更保守的 chunk limit，降低多 section/figure 混合块的尺寸。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        dom_chunker_cls = mocker.patch("engine.epub.parser.DomChunker")
        dom_chunker_cls.return_value.chunk.return_value = []
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ["c07.xhtml"])])

        original_html = """
        <html><body>
          <section><header><h1>Title</h1></header><aside><section><p>Alpha</p></section></aside></section>
          <section><figure><img alt="fig" src="a.jpg"/><figcaption><p>Cap</p></figcaption></figure><p>Body text.</p></section>
          <section><span aria-label="220" epub:type="pagebreak" id="Page_220" role="doc-pagebreak"></span><p>More text.</p></section>
        </body></html>
        """
        mocker.patch("builtins.open", mock_open(read_data=original_html))

        parser_instance.parse()

        dom_chunker_cls.assert_called_with(token_limit=900, secondary_placeholder_limit=12)

    def test_parse_persists_source_html_integrity_errors(self, mocker, parser_instance):
        """测试原始 HTML 结构错误会被记录到 EpubItem 中，而不只是打日志。"""
        mocker.patch.object(parser_instance, "extract")
        mocker.patch.object(parser_instance, "load_json", return_value=None)
        mocker.patch.object(parser_instance, "save_json")
        mocker.patch("os.walk", return_value=[(parser_instance.output_dir, (), ["broken.xhtml"])])

        broken_html = "<html><body><p>Alpha</body></html>"
        mocker.patch("builtins.open", mock_open(read_data=broken_html))

        book = parser_instance.parse()

        item = book.items[0]
        assert item.source_html_valid is False
        assert item.source_html_errors
        assert any("未闭合" in err or "标签交错" in err for err in item.source_html_errors)

    def test_parse_passes_secondary_placeholder_limit_to_chunker(self, mocker, tmp_path):
        """测试 Parser 会把 secondary_placeholder_limit 传递给 DomChunker。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path), limit=123, secondary_placeholder_limit=7)

        mocker.patch.object(parser, "extract")
        mocker.patch.object(parser, "load_json", return_value=None)
        mocker.patch.object(parser, "save_json")
        mocker.patch("os.walk", return_value=[(parser.output_dir, (), ["chapter1.xhtml"])])
        mocker.patch("builtins.open", mock_open(read_data="<html><body><p>Hello</p></body></html>"))
        dom_chunker_cls = mocker.patch("engine.epub.parser.DomChunker")
        dom_chunker_cls.return_value.chunk.return_value = []

        parser.parse()

        dom_chunker_cls.assert_called_with(token_limit=123, secondary_placeholder_limit=7)

    def test_parse_treats_ncx_content_with_nonstandard_filename_as_nav_file(self, mocker, tmp_path):
        """测试即使文件名不是 toc.ncx，只要内容是 NCX/navMap 也走 nav_text 模式。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))

        mocker.patch.object(parser, "extract")
        mocker.patch.object(parser, "load_json", return_value=None)
        mocker.patch.object(parser, "save_json")
        mocker.patch("os.walk", return_value=[(parser.output_dir, (), ["toc01.xml"])])
        mocker.patch(
            "builtins.open",
            mock_open(
                read_data="<ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>"
            ),
        )
        dom_chunker_cls = mocker.patch("engine.epub.parser.DomChunker")
        dom_chunker_cls.return_value.chunk.return_value = []

        parser.parse()

        dom_chunker_cls.return_value.chunk.assert_called_once()
        assert dom_chunker_cls.return_value.chunk.call_args.kwargs["is_nav_file"] is True

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
            side_effect=lambda item, *, is_nav_file: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="nav-text",
                        original="[NAVTXT:0] Chapter 1",
                        translated=None,
                        status=TranslationStatus.PENDING,
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
        assert require_chunks(book.items[0])[0].chunk_mode == "nav_text"
        assert require_chunks(book.items[0])[0].nav_targets
        save_json.assert_called_once()

    def test_rebuild_nav_item_chunks_passes_secondary_placeholder_limit(self, tmp_path, mocker):
        """测试导航 chunk 重建时也会传递 secondary_placeholder_limit。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path), secondary_placeholder_limit=9)
        item = EpubBook.model_validate(
            {
                "name": "my_book",
                "path": str(epub_path),
                "extract_path": str(tmp_path / "temp" / "my_book"),
                "items": [
                    {
                        "id": "OEBPS/toc.ncx",
                        "path": str(tmp_path / "temp" / "my_book" / "OEBPS" / "toc.ncx"),
                        "content": "<ncx><navMap><navPoint><navLabel><text>Chapter 1</text></navLabel></navPoint></navMap></ncx>",
                    }
                ],
            }
        ).items[0]
        dom_chunker_cls = mocker.patch("engine.epub.parser.DomChunker")
        dom_chunker_cls.return_value.chunk.return_value = []

        parser._rebuild_nav_item_chunks(item, is_nav_file=True)

        dom_chunker_cls.assert_called_with(token_limit=1500, secondary_placeholder_limit=9)

    def test_rebuild_nav_item_chunks_does_not_pre_extract_navigation_xhtml(self, tmp_path, mocker):
        """测试 navigation.xhtml 这类导航文档重建时不会先被 PRE 提取吃空。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        html = """
        <html><body>
          <section aria-label="chapter opening">
            <nav class="tocList" epub:type="toc" id="toc" role="doc-toc">
              <h1>Table of Contents</h1>
              <ol>
                <li><a href="c01.xhtml">1 Linear Programming</a></li>
                <li><a href="c02.xhtml">2 Graph Theory</a></li>
              </ol>
            </nav>
          </section>
        </body></html>
        """
        item = EpubBook.model_validate(
            {
                "name": "my_book",
                "path": str(epub_path),
                "extract_path": str(tmp_path / "temp" / "my_book"),
                "items": [
                    {
                        "id": "OPS/navigation.xhtml",
                        "path": str(tmp_path / "temp" / "my_book" / "OPS" / "navigation.xhtml"),
                        "content": html,
                        "chunks": [
                            {
                                "name": "legacy-nav",
                                "original": "<title>Table of Contents</title>",
                                "translated": None,
                                "status": "pending",
                                "tokens": 20,
                                "xpaths": ["/html/head/title"],
                            }
                        ],
                    }
                ],
            }
        ).items[0]
        mocker.patch("engine.epub.parser.DomChunker", DomChunker)

        parser._rebuild_nav_item_chunks(item, is_nav_file=True)

        assert item.chunks
        assert item.chunks[0].chunk_mode == "nav_text"
        assert item.chunks[0].nav_targets

    def test_load_json_upgrades_embedded_toc_chunks(self, tmp_path, mocker):
        """测试普通文件中的内嵌目录块 checkpoint 也会被重建为 nav_text 模式。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        mocker.patch.object(
            parser,
            "_rebuild_nav_item_chunks",
            side_effect=lambda item, *, is_nav_file: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="embedded-nav-text",
                        original="[NAVTXT:0] Chapter 1",
                        translated=None,
                        status=TranslationStatus.PENDING,
                        tokens=10,
                        chunk_mode="nav_text",
                        xpaths=[],
                        nav_targets=[
                            NavTextTarget(
                                marker="[NAVTXT:0]",
                                xpath="/html/body/nav/div/a/span",
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
                            "id": "OEBPS/Text/Title_Pages.xhtml",
                            "path": str(tmp_path / "temp" / "my_book" / "OEBPS" / "Text" / "Title_Pages.xhtml"),
                            "content": '<html><body><nav class="toc"><div><a href="#c1"><span class="label">Chapter 1</span></a></div></nav></body></html>',
                            "chunks": [
                                {
                                    "name": "legacy-nav",
                                    "original": '<nav class="toc"><div><a href="#c1"><span class="label">Chapter 1</span></a></div></nav>',
                                    "translated": None,
                                    "status": "pending",
                                    "tokens": 20,
                                    "xpaths": ["/html/body/nav"],
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
        assert require_chunks(book.items[0])[0].chunk_mode == "nav_text"
        assert require_chunks(book.items[0])[0].nav_targets
        save_json.assert_called_once()

    def test_load_json_upgrades_nonstandard_ncx_filename_chunks(self, tmp_path, mocker):
        """测试文件名不标准但内容是 NCX/navMap 的 checkpoint 也会被重建为 nav_text 模式。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        mocker.patch.object(
            parser,
            "_rebuild_nav_item_chunks",
            side_effect=lambda item, *, is_nav_file: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="nav-text",
                        original="[NAVTXT:0] Chapter 1",
                        translated=None,
                        status=TranslationStatus.PENDING,
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
                            "id": "OEBPS/toc01.xml",
                            "path": str(tmp_path / "temp" / "my_book" / "OEBPS" / "toc01.xml"),
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
        assert require_chunks(book.items[0])[0].chunk_mode == "nav_text"
        assert require_chunks(book.items[0])[0].nav_targets
        save_json.assert_called_once()

    def test_load_json_rebuilds_oversized_nav_text_chunks(self, tmp_path, mocker):
        """测试已是 nav_text 但单块过大的 checkpoint 也会按新策略重建。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        rebuild = mocker.patch.object(
            parser,
            "_rebuild_nav_item_chunks",
            side_effect=lambda item, *, is_nav_file: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="nav-a",
                        original="[NAVTXT:0] Chapter 1",
                        translated=None,
                        status=TranslationStatus.PENDING,
                        tokens=10,
                        chunk_mode="nav_text",
                        xpaths=[],
                        nav_targets=[
                            NavTextTarget(
                                marker="[NAVTXT:0]",
                                xpath="/ncx/navMap/navPoint[1]/navLabel/text",
                                text_index=0,
                                original_text="Chapter 1",
                            )
                        ],
                    ),
                    Chunk(
                        name="nav-b",
                        original="[NAVTXT:1] Chapter 2",
                        translated=None,
                        status=TranslationStatus.PENDING,
                        tokens=10,
                        chunk_mode="nav_text",
                        xpaths=[],
                        nav_targets=[
                            NavTextTarget(
                                marker="[NAVTXT:1]",
                                xpath="/ncx/navMap/navPoint[2]/navLabel/text",
                                text_index=0,
                                original_text="Chapter 2",
                            )
                        ],
                    ),
                ],
            ),
        )
        oversized_targets = [
            {
                "marker": f"[NAVTXT:{i}]",
                "xpath": f"/ncx/navMap/navPoint[{i + 1}]/navLabel/text",
                "text_index": 0,
                "original_text": f"Chapter {i}",
            }
            for i in range(DomChunker.DEFAULT_NAV_UNIT_LIMIT + 1)
        ]
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
                                    "name": "oversized-nav",
                                    "original": "\n".join(
                                        f"[NAVTXT:{i}] Chapter {i}"
                                        for i in range(DomChunker.DEFAULT_NAV_UNIT_LIMIT + 1)
                                    ),
                                    "translated": None,
                                    "status": "pending",
                                    "tokens": 200,
                                    "chunk_mode": "nav_text",
                                    "xpaths": [],
                                    "nav_targets": oversized_targets,
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
        assert len(require_chunks(book.items[0])) == 2
        rebuild.assert_called_once()
        save_json.assert_called_once()

    def test_load_json_upgrades_title_only_broken_chunks(self, tmp_path, mocker):
        """测试正文页只剩 title chunk 的坏 checkpoint 会被自动重建。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        rebuild = mocker.patch.object(
            parser,
            "_rebuild_item_chunks",
            side_effect=lambda item, *, is_nav_file, strip_title=False: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="body-text",
                        original="<title>4 Linear Programming</title>\n<section><p>Hello</p></section>",
                        translated=None,
                        status=TranslationStatus.PENDING,
                        tokens=20,
                        xpaths=["/html/head/title", "/html/body/section"],
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
                            "id": "OPS/c04.xhtml",
                            "path": str(tmp_path / "temp" / "my_book" / "OPS" / "c04.xhtml"),
                            "content": (
                                "<html><head><title>4 Linear Programming</title></head><body><section>"
                                "<p>Hello world in body with enough prose to require rechunking for this checkpoint upgrade test.</p>"
                                "</section></body></html>"
                            ),
                            "chunks": [
                                {
                                    "name": "title-only",
                                    "original": "<title>4 Linear Programming</title>",
                                    "translated": None,
                                    "status": "pending",
                                    "tokens": 8,
                                    "xpaths": ["/html/head/title"],
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
        assert len(require_chunks(book.items[0])) == 1
        assert "/html/body/section" in require_chunks(book.items[0])[0].xpaths
        rebuild.assert_called_once_with(book.items[0], is_nav_file=False, strip_title=False)
        save_json.assert_called_once()

    def test_load_json_upgrades_title_only_broken_chunks_keeps_completed_title_translation(self, tmp_path, mocker):
        """测试 title-only checkpoint 升级时会保留已完成的标题译文。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        rebuild = mocker.patch.object(
            parser,
            "_rebuild_item_chunks",
            side_effect=lambda item, *, is_nav_file, strip_title=False: setattr(
                item,
                "chunks",
                [
                    Chunk(
                        name="body-text",
                        original="<section><p>Hello</p></section>",
                        translated=None,
                        status=TranslationStatus.PENDING,
                        tokens=12,
                        xpaths=["/html/body/section"],
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
                            "id": "OPS/c04.xhtml",
                            "path": str(tmp_path / "temp" / "my_book" / "OPS" / "c04.xhtml"),
                            "content": (
                                "<html><head><title>4 Linear Programming</title></head><body><section>"
                                "<p>Hello world in body with enough prose to require rechunking for this checkpoint upgrade test.</p>"
                                "</section></body></html>"
                            ),
                            "chunks": [
                                {
                                    "name": "title-only",
                                    "original": "<title>4 Linear Programming</title>",
                                    "translated": "<title>4 线性规划</title>",
                                    "status": "completed",
                                    "tokens": 8,
                                    "xpaths": ["/html/head/title"],
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
        assert len(require_chunks(book.items[0])) == 2
        assert require_chunks(book.items[0])[0].translated == "<title>4 线性规划</title>"
        assert require_chunks(book.items[0])[0].status == "completed"
        assert require_chunks(book.items[0])[0].xpaths == ["/html/head/title"]
        assert require_chunks(book.items[0])[1].xpaths == ["/html/body/section"]
        rebuild.assert_called_once_with(book.items[0], is_nav_file=False, strip_title=True)
        save_json.assert_called_once()

    def test_load_json_ignores_checkpoint_when_placeholder_inventory_mismatches(self, tmp_path, mocker):
        """旧 checkpoint 的占位符映射与当前提取结果不一致时，应放弃加载并走全量重建。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
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
                            "id": "OPS/c06.xhtml",
                            "path": str(tmp_path / "temp" / "my_book" / "OPS" / "c06.xhtml"),
                            "content": "<html><body><h3><code>Algorithm</code></h3><p>Body</p></body></html>",
                            "chunks": [],
                            "preserved_pre": [],
                            "preserved_code": [],
                            "preserved_style": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        save_json = mocker.patch.object(parser, "save_json")

        book = parser.load_json()

        assert book is None
        save_json.assert_not_called()

    def test_load_json_keeps_completed_nav_text_chunks_at_current_unit_limit(self, tmp_path, mocker):
        """测试已经按当前 24 单元策略切好的 nav_text checkpoint 不会在每次加载时被误重建。"""
        epub_path = tmp_path / "my_book.epub"
        parser = Parser(path=str(epub_path))
        rebuild = mocker.patch.object(parser, "_rebuild_nav_item_chunks")
        checkpoint_path = tmp_path / "my_book.json"
        nav_targets = [
            {
                "marker": f"[NAVTXT:{i}]",
                "xpath": f"/ncx/navMap/navPoint[{i + 1}]/navLabel/text",
                "text_index": 0,
                "original_text": f"Chapter {i}",
            }
            for i in range(DomChunker.DEFAULT_NAV_UNIT_LIMIT)
        ]
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
                                    "name": "nav-0",
                                    "original": "\n".join(
                                        f"[NAVTXT:{i}] Chapter {i}" for i in range(DomChunker.DEFAULT_NAV_UNIT_LIMIT)
                                    ),
                                    "translated": "\n".join(
                                        f"[NAVTXT:{i}] 第{i}章" for i in range(DomChunker.DEFAULT_NAV_UNIT_LIMIT)
                                    ),
                                    "status": "completed",
                                    "tokens": 200,
                                    "chunk_mode": "nav_text",
                                    "xpaths": [],
                                    "nav_targets": nav_targets,
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
        assert require_chunks(book.items[0])[0].status == "completed"
        rebuild.assert_not_called()
        save_json.assert_not_called()
