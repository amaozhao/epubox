"""Test cases for HTML content processor."""

import pytest
from bs4 import BeautifulSoup

from app.html.processor import SKIP_TAGS, HTMLContentProcessor


@pytest.fixture
def processor():
    """Create a processor instance for testing."""
    return HTMLContentProcessor()


class TestHTMLContentProcessor:
    """Test cases for HTMLContentProcessor class."""

    async def test_create_placeholder(self, processor):
        """Test placeholder creation."""
        content = "<script>test</script>"
        placeholder = processor._create_placeholder(content)

        assert placeholder == "†0†"
        assert processor.placeholders[placeholder] == content
        assert processor.placeholder_counter == 1

    async def test_process_content(self, processor):
        """Test processing content after skip tags are replaced."""
        # 测试短文本：应该作为一个整体任务
        html = "<div><p>Hello</p><p>World</p></div>"
        soup = BeautifulSoup(html, "html.parser")
        tasks = []

        processor._process_content(soup, tasks)

        assert len(tasks) == 1
        assert tasks[0]["content"] == "<div><p>Hello</p><p>World</p></div>"

        # 测试长文本：应该拆分成多个任务
        processor.max_chunk_size = 30  # 设置一个很小的限制，考虑标签长度
        long_html = "<div><p>This is a long paragraph</p><p>Another long text</p></div>"
        soup = BeautifulSoup(long_html, "html.parser")
        tasks = []

        processor._process_content(soup, tasks)

        assert len(tasks) == 2
        assert tasks[0]["content"] == "<p>This is a long paragraph</p>"
        assert tasks[1]["content"] == "<p>Another long text</p>"

    async def test_process_html(self, processor):
        """Test complete HTML processing."""
        html = (
            "<div><script>test</script><p>Hello</p><style>css</style><p>World</p></div>"
        )

        tasks = await processor.process_html(html)

        # 验证生成了正确的任务，应该包含占位符
        assert len(tasks) == 1
        # 检查占位符映射
        assert len(processor.placeholders) == 2
        script_placeholder = list(processor.placeholders.keys())[0]
        style_placeholder = list(processor.placeholders.keys())[1]
        # 验证内容包含占位符
        expected_content = f"<div>{script_placeholder}<p>Hello</p>{style_placeholder}<p>World</p></div>"
        assert tasks[0]["content"] == expected_content
        # 验证原始内容被正确保存
        assert "<script>test</script>" in processor.placeholders.values()
        assert "<style>css</style>" in processor.placeholders.values()

    async def test_restore_content(self, processor):
        """Test restoring content from placeholders."""
        # 设置初始状态
        original = "<script>test</script>"
        placeholder = processor._create_placeholder(original)

        # 测试还原
        translated = f"Some text {placeholder} more text"
        restored = await processor.restore_content(translated)

        assert original in restored
        assert placeholder not in restored

    async def test_skip_tags_comprehensive(self, processor):
        """Test handling of all skip tags."""
        # 创建包含所有skip tags的HTML
        skip_tags_html = "".join(
            f"<{tag}>test</{tag}>" for tag in SKIP_TAGS if ":" not in tag
        )

        tasks = await processor.process_html(skip_tags_html)

        # 验证所有skip tags都被替换为占位符，并作为一个任务
        assert len(tasks) == 1
        # 验证所有skip标签都被替换为占位符
        assert processor.placeholder_counter == len(
            [tag for tag in SKIP_TAGS if ":" not in tag]
        )
        # 验证任务内容只包含占位符
        content = tasks[0]["content"]
        assert all(placeholder in content for placeholder in processor.placeholders)

    async def test_nested_skip_tags(self, processor):
        """Test handling of nested skip tags."""
        html = "<pre>Some text<code>nested code</code></pre>"

        tasks = await processor.process_html(html)

        # 验证生成了一个包含占位符的任务
        assert len(tasks) == 1
        assert processor.placeholder_counter == 1
        placeholder = list(processor.placeholders.keys())[0]
        assert tasks[0]["content"] == placeholder
        # 验证原始内容被正确保存
        assert "<pre>" in processor.placeholders[placeholder]
        assert "<code>" in processor.placeholders[placeholder]

    async def test_mixed_content(self, processor):
        """Test handling of mixed content with skip tags and normal content."""
        html = "<div>Start<pre>skip this<code>and this</code></pre>Middle<script>skip</script>End</div>"

        tasks = await processor.process_html(html)

        # 验证生成了一个包含占位符的任务
        assert len(tasks) == 1
        # 验证占位符被正确创建
        assert len(processor.placeholders) == 2  # pre和script标签
        pre_placeholder = list(processor.placeholders.keys())[0]
        script_placeholder = list(processor.placeholders.keys())[1]
        # 验证内容格式正确
        expected = f"<div>Start{pre_placeholder}Middle{script_placeholder}End</div>"
        assert tasks[0]["content"] == expected

    async def test_content_length_limit(self, processor):
        """Test content length limit handling."""
        # 创建一个长文本，注意包含标签的总长度
        text = "test " * 100  # 500 字符
        html = f"<div><p>{text}</p></div>"  # 加上标签后超过限制
        processor.max_chunk_size = 100  # 设置一个较小的限制

        tasks = await processor.process_html(html)

        # 由于超过了限制，应该递归到p标签
        assert len(tasks) == 1
        assert tasks[0]["content"] == f"<p>{text}</p>"

    async def test_empty_and_invalid_html(self, processor):
        """Test handling of empty and invalid HTML content."""
        # 测试空内容
        tasks = await processor.process_html("")
        assert len(tasks) == 0

        # 测试空白内容
        tasks = await processor.process_html("   \n   ")
        assert len(tasks) == 0

        # 测试无效HTML
        tasks = await processor.process_html("<div>test</p>")  # 标签不匹配
        assert len(tasks) == 1
        assert tasks[0]["content"] == "<div>test</div>"

    async def test_text_only_content(self, processor):
        """Test handling of text-only content without tags."""
        # 纯文本内容
        text = "This is a plain text without any tags"
        tasks = await processor.process_html(text)
        assert len(tasks) == 1
        assert tasks[0]["content"] == text

        # 带换行的纯文本
        text = "Line 1\nLine 2\nLine 3"
        tasks = await processor.process_html(text)
        assert len(tasks) == 1
        assert tasks[0]["content"] == text

    async def test_deep_nested_content(self, processor):
        """Test handling of deeply nested content that are not skip tags."""
        html = """
        <div>
            <section>
                <article>
                    <div>
                        <p>Deeply nested content</p>
                    </div>
                </article>
            </section>
        </div>
        """
        tasks = await processor.process_html(html)
        assert len(tasks) == 1
        # 验证保留了完整的嵌套结构
        assert all(
            tag in tasks[0]["content"] for tag in ["div", "section", "article", "p"]
        )
        assert "Deeply nested content" in tasks[0]["content"]

    async def test_html_with_comments(self, processor):
        """Test handling of HTML content with comments."""
        html = """
        <div>
            <!-- This is a comment -->
            <p>Text before comment</p>
            <!-- Another comment -->
            <p>Text after comment</p>
        </div>
        """
        tasks = await processor.process_html(html)
        assert len(tasks) == 1
        # 验证注释被保留
        assert "<!-- This is a comment -->" in tasks[0]["content"]
        assert "<!-- Another comment -->" in tasks[0]["content"]
        assert "<p>Text before comment</p>" in tasks[0]["content"]
        assert "<p>Text after comment</p>" in tasks[0]["content"]

    async def test_process_long_html(self, processor):
        """Test processing of long HTML content."""
        with open("tests/html/test.html", "r", encoding="utf-8") as f:
            html = f.read()

        tasks = await processor.process_html(html)

        # 基本验证
        assert isinstance(tasks, list)
        assert len(tasks) > 0

        # 合并所有任务内容以便搜索
        all_content = " ".join(task["content"] for task in tasks)

        # 1. 验证标题和内容被正确提取
        assert "1000+" in all_content
        assert "AI/ML Product/Project" in all_content
        assert "This is a list of 1000+ AI product ideas" in all_content
        assert "21 Industries" in all_content
        assert "1. Energy" in all_content
        assert "2. Agriculture" in all_content

        # 2. 验证 img 和 link 标签被替换为占位符
        img_tag = '<img src="image_rsrc44.jpg" alt="" class="class_sP"/>'
        link_tag = '<link rel="stylesheet" type="text/css" href="stylesheet.css"/>'

        # 检查占位符是否存在且格式正确
        assert len(processor.placeholders) >= 2
        assert all(
            placeholder.startswith("†") and placeholder.endswith("†")
            for placeholder in processor.placeholders.keys()
        )

        # 检查原始标签是否被正确保存在占位符中
        placeholders_content = processor.placeholders.values()
        found_img_match = False
        found_link_match = False

        for content in placeholders_content:
            if self._compare_html_tags(img_tag, content):
                found_img_match = True
            if self._compare_html_tags(link_tag, content):
                found_link_match = True

        assert (
            found_img_match
        ), f"No matching img tag found in placeholders. Expected: {img_tag}"
        assert (
            found_link_match
        ), f"No matching link tag found in placeholders. Expected: {link_tag}"

        # 检查原始内容中不应该包含这些标签
        assert img_tag not in all_content
        assert link_tag not in all_content

        # 3. 验证内容恢复
        for task in tasks:
            restored = await processor.restore_content(task["content"])
            if "†" in task["content"]:
                # 有占位符的内容应该被替换
                assert restored != task["content"]
                # 确保占位符被替换回了有效的HTML
                assert "<" in restored and ">" in restored

                # 验证还原后的img标签
                if self._compare_html_tags(img_tag, task["content"]):
                    assert self._compare_html_tags(img_tag, restored)

                # 验证还原后的link标签
                if self._compare_html_tags(link_tag, task["content"]):
                    assert self._compare_html_tags(link_tag, restored)

                # 验证所有占位符都被替换
                assert "†" not in restored
            else:
                # 没有占位符的内容应该保持不变
                assert restored == task["content"]

        # 4. 验证完整的还原流程
        # 选择一个包含占位符的任务
        placeholder_task = next(task for task in tasks if "†" in task["content"])
        restored_content = await processor.restore_content(placeholder_task["content"])

        # 解析原始内容和还原后的内容
        original_soup = BeautifulSoup(html, "html.parser")
        restored_soup = BeautifulSoup(restored_content, "html.parser")

        # 验证还原后的img标签
        original_img = original_soup.find("img")
        restored_img = restored_soup.find("img")
        if original_img and restored_img:
            assert original_img.attrs == restored_img.attrs

        # 验证还原后的link标签
        original_link = original_soup.find("link")
        restored_link = restored_soup.find("link")
        if original_link and restored_link:
            assert original_link.attrs == restored_link.attrs

        # 5. 验证HTML结构和属性被保留
        assert 'class="heading_sF"' in all_content
        assert 'id="id__798_2_"' in all_content
        assert 'class="class_s8"' in all_content
        assert 'class="class_s3W"' in all_content

    def _compare_html_tags(self, tag1_str: str, tag2_str: str) -> bool:
        """比较两个HTML标签是否语义等价。

        Args:
            tag1_str: 第一个HTML标签字符串
            tag2_str: 第二个HTML标签字符串

        Returns:
            bool: 如果标签语义等价则返回True
        """
        # 解析HTML标签
        soup1 = BeautifulSoup(tag1_str, "html.parser")
        soup2 = BeautifulSoup(tag2_str, "html.parser")

        tag1 = soup1.find()
        tag2 = soup2.find()

        if tag1 is None or tag2 is None:
            return False

        # 比较标签名
        if tag1.name != tag2.name:
            return False

        # 比较属性
        return tag1.attrs == tag2.attrs
