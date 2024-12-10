"""Test cases for HTML content processor."""

import pytest
from bs4 import BeautifulSoup

from app.core.config import settings
from app.html.processor import SKIP_TAGS, HTMLProcessor
from app.translation.factory import ProviderFactory
from app.translation.models import LimitType, TranslationProvider


@pytest.fixture
def translator():
    """创建翻译器实例."""
    provider_model = TranslationProvider(
        name="mistral",
        provider_type="mistral",
        config={"api_key": settings.MISTRAL_API_KEY},
        enabled=True,
        is_default=True,
        rate_limit=2,
        retry_count=3,
        retry_delay=60,
        limit_type=LimitType.TOKENS,
        limit_value=6000,
    )
    factory = ProviderFactory()
    return factory.create_provider(provider_model)


@pytest.fixture
def processor(translator):
    """Create a processor instance for testing."""
    return HTMLProcessor(
        translator=translator, source_lang="en", target_lang="zh", max_chunk_size=4500
    )


class TestHTMLProcessor:
    """Test cases for TestHTMLProcessor class."""

    async def test_create_placeholder(self, processor):
        """Test placeholder creation."""
        content = "<script>test</script>"
        placeholder = processor.create_placeholder(content)

        assert placeholder == "†0†"
        assert processor.placeholders[placeholder] == content
        assert processor.placeholder_counter == 1

    async def test_replace_skip_tags_recursive(self, processor):
        """Test recursive replacement of skip tags."""
        html = "<div><script>test</script><p>Hello</p><style>css</style></div>"
        soup = BeautifulSoup(html, "html.parser")
        processor.replace_skip_tags_recursive(soup)

        # 验证skip标签被替换为占位符
        assert len(processor.placeholders) == 2
        content = str(soup)
        assert "<script>" not in content
        assert "<style>" not in content
        assert "†0†" in content
        assert "†1†" in content
        assert "<p>Hello</p>" in content

    async def test_process_node(self, processor):
        """Test node processing."""
        html = "<p>Hello World</p>"
        soup = BeautifulSoup(html, "html.parser")
        await processor.process_node(soup.p)

        # 验证节点被处理
        processed = str(soup)
        assert len(processed) > 0

    async def test_process_complete(self, processor):
        """Test complete HTML processing."""
        html = (
            "<div><script>test</script><p>Hello</p><style>css</style><p>World</p></div>"
        )

        result = await processor.process(html)

        # 验证skip标签被替换为占位符并还原
        assert "<script>" in result
        assert "<style>" in result
        assert "test" in result
        assert "css" in result
        assert len(result) > 0

    async def test_restore_content(self, processor):
        """Test restoring content from placeholders."""
        original = "<script>test</script>"
        placeholder = processor.create_placeholder(original)
        translated = f"Some text {placeholder} more text"
        restored = await processor.restore_content(translated)

        assert original in restored
        assert placeholder not in restored

    async def test_nested_skip_tags(self, processor):
        """Test handling of nested skip tags."""
        html = "<pre>Some text<code>nested code</code></pre>"
        result = await processor.process(html)

        # 验证嵌套标签被正确处理
        assert "<pre>" in result
        assert "<code>" in result
        assert len(result) > 0

    async def test_mixed_content(self, processor):
        """Test handling of mixed content with skip tags and normal content."""
        html = "<div>Start<pre>skip this<code>and this</code></pre>Middle<script>skip</script>End</div>"
        result = await processor.process(html)

        # 验证skip标签内容被保留
        assert "<pre>" in result
        assert "<code>" in result
        assert "<script>" in result
        assert len(result) > 0

    async def test_empty_and_invalid_html(self, processor):
        """Test handling of empty and invalid HTML content."""
        # 测试空内容
        result = await processor.process("")
        assert result == ""

        # 测试空白内容
        result = await processor.process("   \n   ")
        assert result.strip() == ""

        # 测试无效HTML
        result = await processor.process("<div>test</p>")  # 标签不匹配
        assert "<div>" in result
        assert len(result) > 0

    async def test_text_only_content(self, processor):
        """Test handling of text-only content without tags."""
        # 纯文本内容
        text = "This is a plain text without any tags"
        result = await processor.process(text)
        assert len(result) > 0

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
        result = await processor.process(html)
        # 验证保留了完整的嵌套结构
        assert all(tag in result for tag in ["div", "section", "article", "p"])
        assert len(result) > 0

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
        result = await processor.process(html)
        # 验证注释被保留
        assert "<!-- This is a comment -->" in result
        assert "<!-- Another comment -->" in result
        assert len(result) > 0
