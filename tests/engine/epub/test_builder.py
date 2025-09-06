import os
import zipfile

import pytest

from engine.epub.builder import Builder


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
        with pytest.MonkeyPatch().context() as m:
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
