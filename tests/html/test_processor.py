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
