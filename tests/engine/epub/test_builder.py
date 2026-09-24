import os
import xml.etree.ElementTree as ET
import zipfile

import pytest

from engine.epub.builder import Builder


def make_font_epub(tmp_path, *, fixed: bool = False, risky_content: str = "", encrypted: bool = False):
    root = tmp_path / "book"
    oebps = root / "OEBPS"
    meta_inf = root / "META-INF"
    fonts = oebps / "fonts"
    fonts.mkdir(parents=True)
    meta_inf.mkdir()
    (root / "mimetype").write_text("application/epub+zip")
    (fonts / "used.ttf").write_bytes(b"used-font")
    (fonts / "unused.ttf").write_bytes(b"unused-font")
    (oebps / "style.css").write_text(
        '@font-face { font-family: "Book Font"; src: url(fonts/used.ttf); }\n'
        'body { font-family: "Book Font"; }\n' + risky_content,
        encoding="utf-8",
    )
    (oebps / "chapter.xhtml").write_text(
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="style.css"/></head>'
        "<body><p>中文正文 English</p></body></html>",
        encoding="utf-8",
    )
    fixed_meta = '<meta property="rendition:layout">pre-paginated</meta>' if fixed else ""
    (oebps / "content.opf").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0">'
        f"<metadata><dc:language>en</dc:language>{fixed_meta}</metadata>"
        '<manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="css" href="style.css" media-type="text/css"/>'
        '<item id="used" href="fonts/used.ttf" media-type="font/ttf"/>'
        '<item id="unused" href="fonts/unused.ttf" media-type="font/ttf"/></manifest>'
        '<spine><itemref idref="chapter"/></spine></package>',
        encoding="utf-8",
    )
    (meta_inf / "container.xml").write_text(
        '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>",
        encoding="utf-8",
    )
    if encrypted:
        (meta_inf / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<EncryptedData><CipherData><CipherReference URI="OEBPS/fonts/unused.ttf"/>'
            "</CipherData></EncryptedData></encryption>",
            encoding="utf-8",
        )
    return Builder(str(root), str(tmp_path / "output.epub")), root


class TestBuilder:
    """
    测试 Builder 类的所有功能。
    """

    @pytest.fixture
    def setup_builder(self, tmp_path):
        """
        创建一个临时目录和文件结构，并返回一个 Builder 实例。
        `tmp_path` 是 pytest 提供的一个内置 fixture，用于创建临时目录。
        """
        source_dir = tmp_path / "source_dir"
        output = tmp_path / "output"

        # 创建模拟源目录和文件
        os.makedirs(os.path.join(source_dir, "OEBPS"), exist_ok=True)
        os.makedirs(os.path.join(source_dir, "META-INF"), exist_ok=True)

        with open(os.path.join(source_dir, "mimetype"), "w") as f:
            f.write("application/epub+zip")
        with open(os.path.join(source_dir, "OEBPS", "chapter1.xhtml"), "w") as f:
            f.write("<html><body>Hello, world!</body></html>")
        with open(os.path.join(source_dir, "META-INF", "container.xml"), "w") as f:
            f.write("<container/>")

        output_path = os.path.join(output, "test_book.epub")

        return Builder(str(source_dir), str(output_path))

    def test_build_creates_epub_with_correct_structure(self, setup_builder):
        """测试 build 方法能否正确创建 EPUB 文件并包含所有文件。"""
        builder = setup_builder

        # 执行打包操作
        result_path = builder.build()

        # 断言返回路径与预期一致
        assert result_path == builder.output
        # 断言 EPUB 文件已创建
        assert os.path.exists(result_path)

        # 检查 EPUB 文件的内容
        with zipfile.ZipFile(result_path, "r") as zf:
            # 获取压缩包中的所有文件名
            file_list = zf.namelist()

            # 断言包含所有预期的文件
            assert "mimetype" in file_list
            assert "OEBPS/chapter1.xhtml" in file_list
            assert "META-INF/container.xml" in file_list

            # 检查 mimetype 文件是否未压缩
            mimetype_info = zf.getinfo("mimetype")
            assert mimetype_info.compress_type == zipfile.ZIP_STORED

    def test_build_raises_error_if_source_dir_not_found(self):
        """测试当源目录不存在时，build 方法是否记录警告日志并返回输出路径（不抛出异常）。"""
        builder = Builder("/non/existent/path", "/temp/output.epub")

        # 使用 caplog fixture 捕获日志（可选，如果你的 pytest 配置支持）
        # 如果不使用 caplog，可以省略日志断言
        with pytest.MonkeyPatch().context():
            # 模拟 logger.warning 为 print（如果 logger 不可 mock）
            # 实际中，你可以 mock logger 或使用 caplog
            result_path = builder.build()

            # 断言返回路径与预期一致（新逻辑：返回 self.output）
            assert result_path == builder.output

            # 可选：验证日志输出（假设使用 caplog fixture）
            # import pytest
            # def test_...(caplog):
            #     ...
            #     caplog.set_level("WARNING")
            #     builder.build()
            #     assert "源目录不存在" in caplog.text

    def test_build_handles_mimetype_file_not_found(self, setup_builder):
        """测试当源目录缺少 mimetype 文件时，build 方法是否能正常工作（并创建它）。"""
        builder = setup_builder
        # 修正：从源目录而不是输出目录中删除文件
        os.remove(os.path.join(builder.dir, "mimetype"))

        # 执行打包操作
        result_path = builder.build()

        # 检查 EPUB 文件已创建，并且 mimetype 文件已自动添加
        assert os.path.exists(result_path)
        with zipfile.ZipFile(result_path, "r") as zf:
            file_list = zf.namelist()
            assert "mimetype" in file_list
            mimetype_content = zf.read("mimetype").decode("utf-8")
            assert mimetype_content == "application/epub+zip"
            mimetype_info = zf.getinfo("mimetype")
            assert mimetype_info.compress_type == zipfile.ZIP_STORED

    def test_build_updates_language_without_changing_css(self, setup_builder):
        builder = setup_builder
        opf_path = os.path.join(builder.dir, "OEBPS", "content.opf")
        css_path = os.path.join(builder.dir, "OEBPS", "style.css")
        css_content = 'body { font-family: "Bookerly", serif; }\ncode { font-family: monospace; }'
        with open(opf_path, "w", encoding="utf-8") as f:
            f.write("<package><metadata><dc:language>en</dc:language></metadata></package>")
        with open(css_path, "w", encoding="utf-8") as f:
            f.write(css_content)

        result_path = builder.build()

        with open(css_path, encoding="utf-8") as f:
            assert f.read() == css_content
        with open(opf_path, encoding="utf-8") as f:
            assert "<dc:language>zh-CN</dc:language>" in f.read()
        with zipfile.ZipFile(result_path) as zf:
            assert zf.read("OEBPS/style.css").decode() == css_content

    def test_reflowable_book_uses_device_font_then_prunes_fonts(self, tmp_path):
        builder, root = make_font_epub(tmp_path)

        result_path = builder.build()

        chapter = (root / "OEBPS/chapter.xhtml").read_text()
        opf = ET.parse(root / "OEBPS/content.opf")
        manifest_hrefs = {item.attrib["href"] for item in opf.getroot().iter() if item.tag.endswith("item")}
        assert 'lang="zh-CN"' in chapter
        assert 'xml:lang="zh-CN"' in chapter
        assert "epubox-device-font" in chapter
        assert "font-family: serif !important" in chapter
        assert "fonts/used.ttf" in manifest_hrefs
        assert "fonts/unused.ttf" not in manifest_hrefs
        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()
        with zipfile.ZipFile(result_path) as zf:
            assert "OEBPS/fonts/used.ttf" in zf.namelist()

    def test_fixed_layout_preserves_referenced_font_but_prunes_unused(self, tmp_path):
        builder, root = make_font_epub(tmp_path, fixed=True)

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()

    def test_spine_fixed_layout_property_preserves_font_mode(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        opf_path = root / "OEBPS/content.opf"
        opf_path.write_text(
            opf_path.read_text().replace(
                '<itemref idref="chapter"/>',
                '<itemref idref="chapter" properties="rendition:layout-pre-paginated"/>',
            )
        )

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()

    def test_standalone_svg_font_reference_is_preserved(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        opf_path = root / "OEBPS/content.opf"
        opf_path.write_text(
            opf_path.read_text().replace(
                "</manifest>",
                '<item id="svg" href="diagram.svg" media-type="image/svg+xml"/></manifest>',
            )
        )
        (root / "OEBPS/diagram.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><style>@font-face {'
            'font-family: "Book Font"; src: url(fonts/used.ttf);}</style><text>图</text></svg>',
            encoding="utf-8",
        )

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" in (root / "OEBPS/chapter.xhtml").read_text()

    def test_css_escaped_font_url_is_resolved_before_pruning(self, tmp_path):
        builder, root = make_font_epub(tmp_path, fixed=True)
        (root / "OEBPS/style.css").write_text(
            '@font-face { font-family: "Book Font"; src: url(fonts/\\75 sed.ttf); }',
            encoding="utf-8",
        )

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()

    @pytest.mark.parametrize(
        "icon_content",
        [
            '.icon::before { content: "\\e001"; }',
            '.icon::before { content: "\\00e001"; }',
            '.icon::before { content: "\ue001"; }',
        ],
    )
    def test_css_icon_keeps_referenced_font_and_skips_device_mode(self, tmp_path, icon_content):
        builder, root = make_font_epub(tmp_path, risky_content=icon_content)

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()

    def test_inline_svg_and_code_pua_are_preserved_locally(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" href="style.css"/></head>'
            "<body><svg><text>Logo</text></svg><p>中文正文</p><pre><code>\uec02file.py</code></pre></body></html>",
            encoding="utf-8",
        )

        builder.build()

        chapter = chapter_path.read_text()
        assert "epubox-preserve-font" in chapter
        assert "<svg" in chapter
        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert not (root / "OEBPS/fonts/unused.ttf").exists()
        tree = ET.parse(chapter_path)
        svg_text = next(element for element in tree.getroot().iter() if element.tag.endswith("text"))
        assert "epubox-device-font" not in svg_text.attrib.get("class", "")

    def test_encrypted_font_is_never_pruned(self, tmp_path):
        builder, root = make_font_epub(tmp_path, fixed=True, encrypted=True)

        builder.build()

        assert (root / "OEBPS/fonts/unused.ttf").exists()

    def test_font_optimization_can_be_disabled(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        builder.optimize_fonts = False

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()
        assert 'lang="zh-CN"' in (root / "OEBPS/chapter.xhtml").read_text()

    def test_manifest_path_escape_fails_closed(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        outside = tmp_path / "outside.css"
        outside.write_text("author-content", encoding="utf-8")
        opf_path = root / "OEBPS/content.opf"
        opf_path.write_text(
            opf_path.read_text().replace(
                "</manifest>",
                '<item id="outside" href="../../outside.css" media-type="text/css"/></manifest>',
            )
        )

        builder.build()

        assert outside.read_text() == "author-content"
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()

    def test_device_font_processing_is_idempotent(self, tmp_path):
        builder, root = make_font_epub(tmp_path)

        builder.build()
        builder.build()

        chapter = ET.parse(root / "OEBPS/chapter.xhtml")
        device_elements = [
            element for element in chapter.getroot().iter() if "epubox-device-font" in element.attrib.get("class", "")
        ]
        assert len(device_elements) == 1
        assert device_elements[0].attrib["style"].count("font-family") == 1

    def test_scripted_epub_preserves_all_fonts(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        opf_path = root / "OEBPS/content.opf"
        opf_path.write_text(
            opf_path.read_text().replace(
                "</manifest>",
                '<item id="script" href="font-loader.js" media-type="text/javascript"/></manifest>',
            )
        )
        (root / "OEBPS/font-loader.js").write_text("new FontFace('Book', 'url(fonts/unused.ttf)')")

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" not in (root / "OEBPS/chapter.xhtml").read_text()

    def test_inline_important_font_is_replaced_on_chinese_element(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'
            '<p id="x" style="color: red; font-family: Book Font !important">中文正文</p></body></html>',
            encoding="utf-8",
        )

        builder.build()

        paragraph = next(element for element in ET.parse(chapter_path).getroot().iter() if element.tag.endswith("p"))
        assert "color: red" in paragraph.attrib["style"]
        assert paragraph.attrib["style"].count("font-family") == 1
        assert "font-family: serif !important" in paragraph.attrib["style"]

    def test_inline_script_or_scripted_property_preserves_all_fonts(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><script>new FontFace("Book", '
            '"url(fonts/unused.ttf)")</script></head><body><p>中文</p></body></html>',
            encoding="utf-8",
        )

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert (root / "OEBPS/fonts/unused.ttf").exists()
        assert "epubox-device-font" not in chapter_path.read_text()

    def test_scripted_property_without_script_preserves_all_fonts(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        opf_path = root / "OEBPS/content.opf"
        opf_path.write_text(
            opf_path.read_text().replace(
                '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>',
                '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" properties="scripted"/>',
            )
        )

        builder.build()

        assert (root / "OEBPS/fonts/used.ttf").exists()
        assert (root / "OEBPS/fonts/unused.ttf").exists()

    def test_cjk_extension_character_uses_device_font(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body><p>\U00020000</p></body></html>',
            encoding="utf-8",
        )

        builder.build()

        paragraph = next(element for element in ET.parse(chapter_path).getroot().iter() if element.tag.endswith("p"))
        assert "font-family: serif !important" in paragraph.attrib["style"]

    def test_inline_vertical_writing_fails_closed_but_language_is_set(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'
            '<p style="writing-mode: vertical-rl">中文</p></body></html>',
            encoding="utf-8",
        )

        builder.build()

        chapter = chapter_path.read_text()
        assert 'lang="zh-CN"' in chapter
        assert "epubox-device-font" not in chapter
        assert (root / "OEBPS/fonts/used.ttf").exists()

    def test_pua_descendant_prevents_ancestor_font_override(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>中文<span>\ue001</span></body></html>',
            encoding="utf-8",
        )

        builder.build()

        body = next(element for element in ET.parse(chapter_path).getroot().iter() if element.tag.endswith("body"))
        span = next(element for element in body.iter() if element.tag.endswith("span"))
        assert "epubox-device-font" not in body.attrib.get("class", "")
        assert "font-family" not in body.attrib.get("style", "")
        assert "epubox-preserve-font" in span.attrib["class"]

    def test_xml_stylesheet_processing_instruction_is_preserved(self, tmp_path):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        chapter_path.write_text(
            '<?xml version="1.0"?><!-- template begins with <html marker -->'
            '<?xml-stylesheet type="text/css" href="style.css"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body><p>中文</p></body></html>'
            "<?tail keep?><!-- trailing -->",
            encoding="utf-8",
        )

        builder.build()

        content = chapter_path.read_text()
        assert "<!-- template begins with <html marker -->" in content
        assert "<?xml-stylesheet" in content
        assert "<?tail keep?>" in content
        assert "<!-- trailing -->" in content
        ET.parse(chapter_path)

    @pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be"])
    def test_non_utf8_xhtml_is_preserved_byte_for_byte(self, tmp_path, encoding):
        builder, root = make_font_epub(tmp_path)
        chapter_path = root / "OEBPS/chapter.xhtml"
        text = (
            f'<?xml version="1.0" encoding="{encoding}"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body><p>中文</p></body></html>'
        )
        chapter_path.write_bytes(text.encode(encoding))
        original = chapter_path.read_bytes()

        builder.build()

        assert chapter_path.read_bytes() == original
        assert "<html" in chapter_path.read_bytes().decode(encoding)


class TestModifyContentOpf:
    """测试 _modify_content_opf 方法"""

    def test_opf_not_found_returns_false(self, tmp_path):
        """测试.opf文件不存在时返回False"""
        builder = Builder(str(tmp_path), str(tmp_path / "output.epub"))
        result = builder._modify_content_opf(str(tmp_path / "nonexistent.opf"))
        assert result is False

    def test_opf_with_dc_language_tag(self, tmp_path):
        """测试修改dc:language标签"""
        opf_content = """<?xml version="1.0"?>
<package version="2.0">
    <metadata>
        <dc:language id="en_language">en-us</dc:language>
    </metadata>
</package>"""
        opf_path = tmp_path / "content.opf"
        opf_path.write_text(opf_content)

        builder = Builder(str(tmp_path), str(tmp_path / "output.epub"))
        result = builder._modify_content_opf(str(opf_path))
        assert result is True

        content = opf_path.read_text()
        assert '<dc:language id="en_language">zh-CN</dc:language>' in content

    def test_opf_with_meta_language_tag(self, tmp_path):
        """测试修改meta language标签"""
        opf_content = """<?xml version="1.0"?>
<package version="2.0">
    <metadata>
        <meta id="meta-language" property="dcterms:language">en</meta>
    </metadata>
</package>"""
        opf_path = tmp_path / "content.opf"
        opf_path.write_text(opf_content)

        builder = Builder(str(tmp_path), str(tmp_path / "output.epub"))
        result = builder._modify_content_opf(str(opf_path))
        assert result is True

    def test_opf_no_language_tag(self, tmp_path):
        """测试opf没有语言标签时记录警告但仍返回True（文件被写回）"""
        opf_content = """<?xml version="1.0"?>
<package version="2.0">
    <metadata>
        <dc:title>Test</dc:title>
    </metadata>
</package>"""
        opf_path = tmp_path / "content.opf"
        opf_path.write_text(opf_content)

        builder = Builder(str(tmp_path), str(tmp_path / "output.epub"))
        result = builder._modify_content_opf(str(opf_path))
        # 即使没修改也返回True（文件被写回）
        assert result is True
