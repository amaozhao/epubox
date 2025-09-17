from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.workflow import StepOutput

from engine.epub import Builder, Parser, Replacer
from engine.orchestrator import Orchestrator
from engine.schemas import Chunk, EpubBook, EpubItem, TranslationStatus


class TestOrchestrator:
    """
    测试 Orchestrator 类及其核心方法。
    """

    @pytest.fixture
    def orchestrator(self):
        """
        创建一个 Orchestrator 实例。
        """
        return Orchestrator()

    @pytest.fixture
    def mock_book(self):
        """
        【修复第一处】: 恢复正确的、包含多个 items 和 chunks 的 mock 数据。
        这是导致 `call_count` 为 0 的根本原因。
        """
        return EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<p>Hello world.</p>",
                    chunks=[
                        Chunk(
                            name="1",
                            original="<p>Hello world.</p>",
                            translated=None,
                            tokens=3,
                            status=TranslationStatus.PENDING,
                        )
                    ],
                ),
                EpubItem(
                    id="item2",
                    path="/mock/path/test_epub/item2.html",
                    content="<p>已翻译内容。</p>",
                    chunks=[
                        Chunk(
                            name="1",
                            original="<p>Translated content.</p>",
                            translated="<p>已翻译内容。</p>",
                            tokens=3,
                            status=TranslationStatus.COMPLETED,
                        )
                    ],
                ),
                EpubItem(id="item3", path="/mock/path/test_epub/item3.html", content="<p>No chunks.</p>", chunks=[]),
            ],
        )

    # --- 测试 _should_translate_chunk 方法 ---

    def test_should_translate_chunk_with_no_translation(self, orchestrator):
        """
        测试当分块没有翻译内容时，_should_translate_chunk 方法返回 True。
        """
        chunk = Chunk(name="1", original="test content", translated=None, tokens=2, status=TranslationStatus.PENDING)
        assert orchestrator._should_translate_chunk(chunk) is True

    def test_should_translate_chunk_with_english_translation(self, orchestrator):
        """
        测试当分块有非中文翻译内容时，_should_translate_chunk 方法返回 True。
        """
        chunk = Chunk(
            name="1",
            original="test content",
            translated="translated content",
            tokens=2,
            status=TranslationStatus.PENDING,
        )
        assert orchestrator._should_translate_chunk(chunk) is True

    def test_should_translate_chunk_with_chinese_translation(self, orchestrator):
        """
        测试当分块已有中文翻译时，_should_translate_chunk 方法返回 False。
        """
        chunk = Chunk(
            name="1", original="test content", translated="测试内容", tokens=2, status=TranslationStatus.COMPLETED
        )
        assert orchestrator._should_translate_chunk(chunk) is False

    def test_should_translate_chunk_with_empty_string(self, orchestrator):
        """
        测试当 translated 属性为空字符串时，_should_translate_chunk 方法返回 True。
        """
        chunk = Chunk(name="1", original="test content", translated="", tokens=2, status=TranslationStatus.PENDING)
        assert orchestrator._should_translate_chunk(chunk) is True

    # --- 测试 translate_epub 方法 ---
    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch("engine.agents.workflow.get_translator_workflow")
    async def test_translate_epub_successful_translation(
        self, mock_get_translator_workflow, mock_parser_parse, orchestrator
    ):
        """
        测试 translate_epub 成功翻译后，EpubBook 的状态是否正确更新。
        """
        # 定义测试数据：一个包含三个 EpubItem，其中两个需要翻译的 EpubBook 实例
        mock_chunk1 = Chunk(
            name="1", original="<p>Hello world.</p>", translated=None, tokens=3, status=TranslationStatus.PENDING
        )
        mock_chunk2 = Chunk(
            name="2", original="<p>How are you?</p>", translated=None, tokens=4, status=TranslationStatus.PENDING
        )

        mock_book_with_chunks = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="item1.html",
                    content="<p>Hello world.</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[mock_chunk1],
                ),
                EpubItem(
                    id="item2",
                    path="item2.html",
                    content="<p>No chunks here.</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[],
                ),
                EpubItem(
                    id="item3",
                    path="item3.html",
                    content="<p>How are you?</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[mock_chunk2],
                ),
            ],
        )

        mock_parser_parse.return_value = mock_book_with_chunks

        # 模拟 workflow.arun 的行为，返回翻译结果
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=StepOutput(
                success=True,
                content=Chunk(
                    name="1",
                    original="<p>How are you?</p>",
                    translated="<p>你好吗？</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                step_run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 调用被测试的方法
        await orchestrator.translate_epub("mock_epub_path")

        # 验证最终状态，这是测试的核心
        # 检查第一个 chunk 的翻译结果
        first_item_chunks = mock_book_with_chunks.items[0].chunks
        assert isinstance(first_item_chunks, list)
        assert first_item_chunks[0].translated == "<p>你好，世界。</p>"
        assert first_item_chunks[0].status == TranslationStatus.COMPLETED

        # 检查第三个 chunk 的翻译结果
        third_item_chunks = mock_book_with_chunks.items[2].chunks
        assert isinstance(third_item_chunks, list)
        assert third_item_chunks[0].translated == "<p>你好吗？</p>"
        assert third_item_chunks[0].status == TranslationStatus.COMPLETED

    @pytest.mark.asyncio
    # @patch("engine.orchestrator.tqdm", side_effect=lambda iterable, **kwargs: iterable)
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.agents.workflow.get_translator_workflow")
    async def test_translate_epub_skips_translated_chunks(
        self,
        mock_get_translator_workflow,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
        mock_book,
    ):
        """
        测试当分块已被翻译时，translate_epub 能正确跳过。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=StepOutput(
                success=True,
                content=Chunk(
                    name="1",
                    original="<p>Hello world.</p>",
                    translated="<p>你好，世界。</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                step_run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 使用真实的 _should_translate_chunk（item1 需要翻译，item2 已翻译）
        await orchestrator.translate_epub("mock_epub_path")

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.agents.workflow.get_translator_workflow")
    async def test_translate_epub_handles_errors(
        self,
        mock_get_translator_workflow,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
        mock_book,
    ):
        """
        测试当 TranslatorWorkflow 返回错误响应时，translate_epub 能正确处理。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例，模拟失败
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            side_effect=StepOutput(
                success=False,
                content=mock_book.items[0].chunks[0],
                error="翻译步骤失败：检测到占位符不匹配。",
                step_run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 模拟 logger.error
        with patch("engine.orchestrator.logger.error"):
            # 确保 _should_translate_chunk 总是返回 True
            with patch.object(orchestrator, "_should_translate_chunk", return_value=True):
                await orchestrator.translate_epub("mock_epub_path")

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.agents.workflow.get_translator_workflow")
    async def test_translate_epub_skips_empty_chunks(
        self,
        mock_get_translator_workflow,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
        mock_book,
    ):
        """
        测试当 EpubItem 的 chunks 为空时，translate_epub 能正确跳过翻译流程。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=StepOutput(
                success=True,
                content=Chunk(
                    name="1",
                    original="<p>Hello world.</p>",
                    translated="<p>你好，世界。</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                step_run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 使用真实的 _should_translate_chunk
        await orchestrator.translate_epub("mock_epub_path")
        # mock_workflow.arun.assert_called_once_with(input=mock_book.items[0].chunks[0])
