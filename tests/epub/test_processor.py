"""
Test cases for the EPUB processor module.
"""

from pathlib import Path

import ebooklib
import ebooklib.epub as epub
import pytest

from app.epub.processor import EpubProcessor


@pytest.fixture
def processor(create_test_epub, temp_work_dir):
    """创建EpubProcessor实例."""
    epub_path = create_test_epub("<p>Original content</p>")
    return EpubProcessor(str(epub_path), str(temp_work_dir))


class TestEpubProcessor:
    """Test cases for EpubProcessor class."""

    async def test_prepare_success(self, processor):
        """测试prepare方法 - 成功场景."""
        assert await processor.prepare()
        assert processor.work_file.exists()
        assert processor.book is not None

    async def test_prepare_file_not_found(self, temp_work_dir):
        """测试prepare方法 - 文件不存在场景."""
        processor = EpubProcessor("/nonexistent/file.epub", str(temp_work_dir))
        assert not await processor.prepare()
        assert not processor.work_file.exists()

    async def test_extract_content(self, processor):
        """测试extract_content方法."""
        await processor.prepare()
        contents = await processor.extract_content()
        assert isinstance(contents, dict)
        assert len(contents) > 0

        # 验证内容文件
        content_file = next(
            (name for name in contents if name.endswith("chap_01.xhtml")), None
        )
        assert content_file is not None
        assert "Original content" in contents[content_file]

        # 验证导航文件
        nav_file = next((name for name in contents if name.endswith("nav.xhtml")), None)
        assert nav_file is not None
        nav_content = contents[nav_file]
        assert "Test Book" in nav_content  # 验证书名在导航中
        assert 'nav epub:type="toc"' in nav_content  # 验证导航结构

    async def test_extract_content_no_prepare(self, processor):
        """测试extract_content方法 - 未prepare场景."""
        with pytest.raises(RuntimeError):
            await processor.extract_content()

    async def test_update_content(self, processor):
        """测试update_content方法."""
        await processor.prepare()
        contents = await processor.extract_content()

        # 修改内容
        translated_contents = {}
        for name, content in contents.items():
            if name.endswith("chap_01.xhtml"):
                # 只翻译内容文件
                translated_contents[name] = content.replace("Original", "Translated")
            else:
                # 保持导航文件不变
                translated_contents[name] = content

        assert await processor.update_content(translated_contents)

        # 验证更新后的内容
        updated_contents = await processor.extract_content()
        for name, content in updated_contents.items():
            if name.endswith("chap_01.xhtml"):
                assert "Translated content" in content
                assert "Original content" not in content
            else:
                assert "Test Book" in content  # 导航文件应保持不变

    async def test_update_content_no_prepare(self, processor):
        """测试update_content方法 - 未prepare场景."""
        with pytest.raises(RuntimeError):
            await processor.update_content({})

    async def test_cleanup(self, processor):
        """测试cleanup方法."""
        await processor.prepare()
        work_file = processor.work_file
        await processor.cleanup()
        assert not work_file.exists()
        assert processor.book is None
        assert len(processor.html_contents) == 0

    def test_get_work_file_path(self, processor):
        """测试get_work_file_path方法."""
        path = processor.get_work_file_path()
        assert isinstance(path, str)
        assert path.endswith(".epub")

    async def test_internal_methods(self, processor):
        """测试内部方法."""
        await processor.prepare()

        # 测试_load_epub
        assert processor._load_epub() is True
        assert processor.book is not None

        # 测试_extract_html
        contents = processor._extract_html()
        assert isinstance(contents, dict)
        assert len(contents) > 0
        for name, content in contents.items():
            item = next(
                item for item in processor.book.get_items() if item.get_name() == name
            )
            if isinstance(item, ebooklib.epub.EpubNav):
                assert 'nav epub:type="toc"' in content
                assert "Test Book" in content
            else:
                assert "Original content" in content

        # 测试_update_html
        translated_contents = {
            name: content.replace("Original", "Translated")
            for name, content in contents.items()
        }
        assert processor._update_html(translated_contents) is True

        # 测试_save_epub
        assert processor._save_epub() is True

        # 验证保存的内容
        processor.book = None  # 清除当前加载的内容
        assert processor._load_epub() is True  # 重新加载
        contents = processor._extract_html()
        for name, content in contents.items():
            item = next(
                item for item in processor.book.get_items() if item.get_name() == name
            )
            if isinstance(item, ebooklib.epub.EpubNav):
                assert 'nav epub:type="toc"' in content
                assert "Test Book" in content
            else:
                assert "Translated content" in content

    async def test_update_html_error_cases(self, processor):
        """测试_update_html方法的错误场景."""
        await processor.prepare()

        # 测试空内容字典
        assert (
            processor._update_html({}) is True
        )  # 空字典应该返回成功，因为没有要更新的内容

        # 测试不存在的文件名
        non_existent = {"non_existent.xhtml": "<p>Test content</p>"}
        assert processor._update_html(non_existent) is True  # 不存在的文件应该被跳过

        # 验证更新后的内容没有变化
        original_contents = await processor.extract_content()
        content_file = next(
            name for name in original_contents if name.endswith("chap_01.xhtml")
        )
        assert (
            "Original content" in original_contents[content_file]
        )  # 确保原始内容未被修改

    async def test_save_epub_error_cases(self, processor, monkeypatch):
        """测试_save_epub方法的错误场景."""
        await processor.prepare()

        # 测试工作目录不可写
        def mock_write_epub(*args):
            raise PermissionError("Permission denied")

        monkeypatch.setattr(epub, "write_epub", mock_write_epub)
        assert not processor._save_epub()

        # 测试EPUB结构不完整
        processor.book.spine = None  # 破坏EPUB结构
        assert not processor._save_epub()

    async def test_cleanup_error_cases(self, processor, tmp_path):
        """测试cleanup方法的错误场景."""
        await processor.prepare()

        # 创建额外的文件在工作目录中
        extra_file = processor.work_dir / "extra.txt"
        extra_file.write_text("Some content")

        # 执行清理
        await processor.cleanup()

        # 验证：
        # 1. work_file 被删除
        assert not processor.work_file.exists()
        # 2. 内存资源被清理
        assert processor.book is None
        assert len(processor.html_contents) == 0
        # 3. 工作目录保留（因为还有其他文件）
        assert processor.work_dir.exists()
        assert extra_file.exists()

        # 测试文件删除失败的情况
        # 重新准备环境
        await processor.prepare()
        # 删除文件的权限
        processor.work_file.chmod(0o444)  # 设置为只读

        # 执行清理
        await processor.cleanup()

        # 验证内存资源仍然被清理
        assert processor.book is None
        assert len(processor.html_contents) == 0
