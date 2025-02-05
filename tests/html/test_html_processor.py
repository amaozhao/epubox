"""Test cases for HTML content processor."""

import pytest
from bs4 import BeautifulSoup

from app.core.config import settings
from app.db.models import LimitType, TranslationProvider
from app.html.processor import SKIP_TAGS, HTMLProcessor
from app.translation.factory import ProviderFactory


class MockTranslator:
    def __init__(self):
        self.limit_value = 3000  # 与原始实现保持一致
        self.limit_type = LimitType.TOKENS
        self.retry_count = 3
        self.retry_delay = 60

    async def translate(
        self, text: str, source_lang: str = "en", target_lang: str = "zh"
    ) -> str:
        # 模拟翻译，保持标记不变
        text = text.replace("Hello", "你好")
        text = text.replace("world", "世界")
        text = text.replace("This is a", "这是一个")
        text = text.replace("test", "测试")
        text = text.replace("Item 1", "项目 1")
        text = text.replace("Item 2", "项目 2")
        text = text.replace("Item 3", "项目 3")
        text = text.replace("Header 1", "标题 1")
        text = text.replace("Header 2", "标题 2")
        text = text.replace("Cell 1", "单元格 1")
        text = text.replace("Cell 2", "单元格 2")
        return text

    def _count_tokens(self, text: str) -> int:
        # 简单实现：每个字符算一个 token
        return len(text)


class TestHTMLProcessor:
    """Test cases for TestHTMLProcessor class."""

    @pytest.fixture
    def translator(self):
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
            limit_value=3000,
            model="mistral-large-latest",
        )
        factory = ProviderFactory()
        return factory.create_provider(provider_model)

    @pytest.fixture
    def processor(self, translator):
        """Create a processor instance for testing."""
        return HTMLProcessor(
            translator=translator,
            source_lang="en",
            target_lang="zh",
            max_chunk_size=4500,
        )

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
        soup = BeautifulSoup(html, "lxml")
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
        soup = BeautifulSoup(html, "lxml")
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

    async def test_process_ncx(self, processor):
        """Test NCX content processing."""
        ncx = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">
  <head>
    <meta name="dtb:uid" content="urn:uuid:12345678-1234-1234-1234-123456789012"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle>
    <text>Test Book</text>
  </docTitle>
  <navMap>
    <navPoint id="navPoint-1" playOrder="1">
      <navLabel>
        <text>Chapter 1</text>
      </navLabel>
      <content src="chapter1.html"/>
    </navPoint>
  </navMap>
</ncx>"""
        result = await processor.process(ncx)

        # 验证 meta 标签被保留
        assert 'name="dtb:uid"' in result
        assert 'content="urn:uuid:12345678-1234-1234-1234-123456789012"' in result

        # 验证文本被翻译
        assert "测试书" in result or "Test Book" in result
        assert "第一章" in result or "Chapter 1" in result

    @pytest.mark.asyncio
    async def test_preserve_html_structure(self):
        """测试保留HTML结构"""
        html = """
        <div>
            <p>Hello <b>world</b>!</p>
            <p>This is a <i>test</i>.</p>
            <ul>
                <li>Item 1</li>
                <li>Item 2</li>
                <li>Item 3</li>
            </ul>
            <table>
                <tr>
                    <th>Header 1</th>
                    <th>Header 2</th>
                </tr>
                <tr>
                    <td>Cell 1</td>
                    <td>Cell 2</td>
                </tr>
            </table>
        </div>
        """
        processor = HTMLProcessor(
            translator=MockTranslator(), source_lang="en", target_lang="zh"
        )
        result = await processor.process(html)

        # 验证翻译结果
        soup = BeautifulSoup(result, "lxml")
        # 验证第一个段落
        p1 = soup.find("p")
        assert p1 is not None, "找不到第一个 p 标签"
        assert p1.b is not None, "找不到 b 标签"
        assert "world" not in p1.get_text(), "英文未被翻译"
        assert "世界" in p1.b.get_text(), "b 标签内容未正确翻译"

        # 验证第二个段落
        p2 = p1.find_next("p")
        assert p2 is not None, "找不到第二个 p 标签"
        assert p2.i is not None, "找不到 i 标签"
        assert "test" not in p2.get_text(), "英文未被翻译"
        assert "测试" in p2.i.get_text(), "i 标签内容未正确翻译"

        # 验证列表
        ul = soup.find("ul")
        assert ul is not None, "找不到 ul 标签"
        assert len(ul.find_all("li")) == 3, "列表项数量不正确"
        for i, li in enumerate(ul.find_all("li"), start=1):
            assert f"Item {i}" not in li.get_text(), f"英文未被翻译 ({i})"
            assert f"项目 {i}" in li.get_text(), f"列表项 {i} 内容未正确翻译"

        # 验证表格
        table = soup.find("table")
        assert table is not None, "找不到 table 标签"
        assert len(table.find_all("tr")) == 2, "表格行数量不正确"
        th1, th2 = table.find_all("th")
        assert th1 is not None, "找不到第一个 th 标签"
        assert th2 is not None, "找不到第二个 th 标签"
        assert "Header 1" not in th1.get_text(), "英文未被翻译 (Header 1)"
        assert "Header 2" not in th2.get_text(), "英文未被翻译 (Header 2)"
        assert "标题 1" in th1.get_text(), "第一个 th 标签内容未正确翻译"
        assert "标题 2" in th2.get_text(), "第二个 th 标签内容未正确翻译"
        td1, td2 = table.find_all("td")
        assert td1 is not None, "找不到第一个 td 标签"
        assert td2 is not None, "找不到第二个 td 标签"
        assert "Cell 1" not in td1.get_text(), "英文未被翻译 (Cell 1)"
        assert "Cell 2" not in td2.get_text(), "英文未被翻译 (Cell 2)"
        assert "单元格 1" in td1.get_text(), "第一个 td 标签内容未正确翻译"
        assert "单元格 2" in td2.get_text(), "第二个 td 标签内容未正确翻译"

        # 验证整体结构
        assert len(soup.find_all("p")) == 2, "p 标签数量不正确"
        assert len(soup.find_all("b")) == 1, "b 标签数量不正确"
        assert len(soup.find_all("i")) == 1, "i 标签数量不正确"
        assert len(soup.find_all("ul")) == 1, "ul 标签数量不正确"
        assert len(soup.find_all("li")) == 3, "li 标签数量不正确"
        assert len(soup.find_all("table")) == 1, "table 标签数量不正确"
        assert len(soup.find_all("tr")) == 2, "tr 标签数量不正确"
        assert len(soup.find_all("th")) == 2, "th 标签数量不正确"
        assert len(soup.find_all("td")) == 2, "td 标签数量不正确"
