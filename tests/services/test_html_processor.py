import html

import pytest
from bs4 import BeautifulSoup

from src.services.html_processor import (
    AttributeProcessor,
    ContentRestoreError,
    ErrorRecovery,
    HTMLElement,
    HTMLProcessingError,
    HTMLProcessor,
    RestoreStrategy,
    StructureError,
    TokenLimitError,
)


class MockTranslationService:
    def __init__(self, max_tokens=1000):
        self.max_tokens = max_tokens
        self.translate_calls = []

    async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        self.translate_calls.append(
            {"text": text, "source_lang": source_lang, "target_lang": target_lang}
        )
        translations = {
            "测试标题": "Test Title",
            "这是一段测试文本": "This is a test text",
            "这是另一段测试文本": "This is another test text",
            "文章标题": "Article Title",
            "第一段落": "First paragraph",
            "第二段落": "Second paragraph",
            "列表项1": "List item 1",
            "列表项2": "List item 2",
        }

        for zh, en in translations.items():
            if zh in text:
                text = text.replace(zh, en)
        return text

    def get_token_limit(self) -> int:
        return self.max_tokens


@pytest.fixture
def html_processor():
    return HTMLProcessor(MockTranslationService())


@pytest.mark.asyncio
class TestHTMLProcessor:
    """HTML处理器测试"""

    @pytest.fixture
    def processor(self):
        return HTMLProcessor(MockTranslationService())

    class TestPlaceholderGeneration:
        """占位符生成策略测试"""

        async def test_generate_unique_placeholders(self, processor):
            """测试生成唯一占位符"""
            p1 = processor.generate_placeholder("DIV")
            p2 = processor.generate_placeholder("DIV")
            p3 = processor.generate_placeholder("SPAN")

            assert p1 != p2  # 相同标签类型生成不同占位符
            assert p1 != p3  # 不同标签类型生成不同占位符
            assert all(p.startswith("[[") and p.endswith("]]") for p in [p1, p2, p3])

        async def test_non_translatable_tags(self, processor):
            """测试不可翻译标签处理"""
            html = """
            <div>
                <pre>def test(): pass</pre>
                <code>print("hello")</code>
                <script>alert('test')</script>
                <style>.test { color: red; }</style>
            </div>
            """
            processed_html, mapping = await processor.preprocess(html)

            # 验证所有不可翻译标签都被替换为占位符
            for tag in processor.NON_TRANSLATABLE_TAGS:
                assert f"[[{tag.upper()}_" in processed_html

            # 验证映射包含所有原始内容
            assert len(mapping) == 4  # 应该有4个占位符映射

    class TestAttributeProcessing:
        """属性处理测试"""

        async def test_boolean_attributes(self):
            """测试布尔属性处理"""
            attr_processor = AttributeProcessor()
            assert attr_processor.process_boolean_attr(True) is None
            assert attr_processor.process_boolean_attr("") is None
            assert attr_processor.process_boolean_attr("value") == "value"

        async def test_class_attributes(self):
            """测试class属性处理"""
            attr_processor = AttributeProcessor()
            assert attr_processor.process_class_attr("cls1 cls2") == ["cls1", "cls2"]
            assert attr_processor.process_class_attr("single") == ["single"]

        async def test_style_attributes(self):
            """测试style属性处理"""
            attr_processor = AttributeProcessor()
            assert attr_processor.process_style_attr(" color: red; ") == "color: red;"

        async def test_data_attributes(self):
            """测试data属性处理"""
            attr_processor = AttributeProcessor()
            assert (
                attr_processor.process_data_attr({"key": "value"}) == '{"key": "value"}'
            )
            assert attr_processor.process_data_attr(["item"]) == '["item"]'
            assert attr_processor.process_data_attr("string") == "string"

    class TestContentRestoration:
        """内容还原测试"""

        async def test_complete_content_restore(self):
            """测试完整内容还原"""
            mapping = {"content": "<div>test</div>"}
            restored = RestoreStrategy.from_complete_content(mapping)
            assert restored == "<div>test</div>"

        async def test_rebuild_from_parts(self):
            """测试从部分信息重建"""
            mapping = {
                "name": "div",
                "attributes": {"class": "test", "id": "main"},
                "structure": {"inner_html": "content"},
            }
            restored = RestoreStrategy.rebuild_from_parts(mapping)
            assert 'class="test"' in restored
            assert 'id="main"' in restored
            assert ">content<" in restored

    class TestErrorHandling:
        """错误处理测试"""

        async def test_validate_placeholder(self):
            """测试占位符验证"""
            assert ErrorRecovery.validate_placeholder("[[DIV_1]]") is True
            assert ErrorRecovery.validate_placeholder("invalid") is False

        async def test_validate_mapping(self):
            """测试映射数据验证"""
            valid_mapping = {"type": "div", "name": "div", "content": "<div>test</div>"}
            assert ErrorRecovery.validate_mapping(valid_mapping) is True

            invalid_mapping = {"type": "div", "content": "<div>test</div>"}
            assert ErrorRecovery.validate_mapping(invalid_mapping) is False

        async def test_token_limit_error(self, processor):
            """测试Token限制错误"""
            # 创建一个超长的文本
            long_text = "测试文本" * 1000
            html = f"<div>{long_text}</div>"

            # 设置一个较小的token限制
            processor.translation_service.max_tokens = 100

            with pytest.raises(TokenLimitError):
                await processor.process_content(html, "zh", "en")

        async def test_structure_error(self, processor):
            """测试结构错误"""
            # 测试未闭合标签的情况
            invalid_html = "<div>未闭合的标签"
            result = await processor.process_content(invalid_html, "zh", "en")
            assert result == invalid_html  # 应该返回原文

            # 测试空内容的情况
            with pytest.raises(StructureError):
                await processor.preprocess("")

            # 测试无效HTML结构的情况
            with pytest.raises(StructureError):
                await processor.preprocess("not html content")

        async def test_content_restore_error(self, processor):
            """测试内容还原错误"""
            invalid_mapping = {"[[INVALID]]": {"type": "unknown"}}
            with pytest.raises(ContentRestoreError):
                await processor.restore_content("[[INVALID]]", invalid_mapping)
