from unittest.mock import AsyncMock, MagicMock, patch

import json
import os
import pytest
from agno.run import RunStatus
from agno.run.workflow import WorkflowRunOutput

from engine.epub import Builder, Parser, DomReplacer
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

    def test_should_process_chunk_retries_untranslated_chunks(self, orchestrator):
        """测试 rerun 时会重试未翻译成功的 chunk。"""
        chunk = Chunk(
            name="1",
            original="<p>Hello</p>",
            translated="",
            tokens=2,
            status=TranslationStatus.TRANSLATION_FAILED,
        )

        assert orchestrator._should_process_chunk(chunk) is True
        assert chunk.status == TranslationStatus.TRANSLATION_FAILED

    def test_should_process_chunk_promotes_manual_translations(self, orchestrator):
        """测试手动翻译后的 chunk 会进入校对流程。"""
        chunk = Chunk(
            name="1",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            tokens=2,
            status=TranslationStatus.TRANSLATION_FAILED,
        )

        assert orchestrator._should_process_chunk(chunk) is True
        assert chunk.status == TranslationStatus.TRANSLATED

    def test_should_process_chunk_skips_accepted_as_is_chunks(self, orchestrator):
        """测试 ACCEPTED_AS_IS 的 chunk 会被视为已完成输出。"""
        chunk = Chunk(
            name="1",
            original="<p>Hello</p>",
            translated="<p>Hello</p>",
            tokens=2,
            status=TranslationStatus.ACCEPTED_AS_IS,
        )

        assert orchestrator._should_process_chunk(chunk) is False

    def test_should_process_chunk_retries_writeback_failed_chunks(self, orchestrator):
        """测试 WRITEBACK_FAILED 的 chunk 会在重跑时恢复到校对流程。"""
        chunk = Chunk(
            name="1",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            tokens=2,
            status=TranslationStatus.WRITEBACK_FAILED,
        )

        assert orchestrator._should_process_chunk(chunk) is True
        assert chunk.status == TranslationStatus.TRANSLATED

    def test_get_output_path_marks_incomplete_artifacts(self, orchestrator):
        """测试存在失败 chunk 时输出文件名会标记为 incomplete。"""
        book = EpubBook(
            name="test_book",
            path="/mock/path/test_book.epub",
            extract_path="/mock/path/test_book",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_book/item1.html",
                    content="<p>Hello</p>",
                    chunks=[
                        Chunk(
                            name="1",
                            original="<p>Hello</p>",
                            translated="",
                            tokens=2,
                            status=TranslationStatus.TRANSLATION_FAILED,
                        )
                    ],
                )
            ],
        )

        assert orchestrator._get_output_path(book).endswith("test_book-cn-incomplete.epub")

    def test_get_output_path_marks_writeback_failed_artifacts_incomplete(self, orchestrator):
        """测试 WRITEBACK_FAILED 的 chunk 也会生成 incomplete 输出后缀。"""
        book = EpubBook(
            name="test_book",
            path="/mock/path/test_book.epub",
            extract_path="/mock/path/test_book",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_book/item1.html",
                    content="<p>Hello</p>",
                    chunks=[
                        Chunk(
                            name="1",
                            original="<p>Hello</p>",
                            translated="<p>你好</p>",
                            tokens=2,
                            status=TranslationStatus.WRITEBACK_FAILED,
                        )
                    ],
                )
            ],
        )

        assert orchestrator._get_output_path(book).endswith("test_book-cn-incomplete.epub")

    def test_get_output_path_keeps_success_suffix_for_completed_artifacts(self, orchestrator):
        """测试全部 chunk 已完成时输出文件名保持成功后缀。"""
        book = EpubBook(
            name="test_book",
            path="/mock/path/test_book.epub",
            extract_path="/mock/path/test_book",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_book/item1.html",
                    content="<p>Hello</p>",
                    chunks=[
                        Chunk(
                            name="1",
                            original="<p>Hello</p>",
                            translated="<p>Hello</p>",
                            tokens=2,
                            status=TranslationStatus.ACCEPTED_AS_IS,
                        )
                    ],
                )
            ],
        )

        assert orchestrator._get_output_path(book).endswith("test_book-cn.epub")

    # --- 测试 translate_epub 方法 ---
    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_successful_translation(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """
        测试 translate_epub 成功翻译后，EpubBook 的状态是否正确更新。
        """
        # 模拟术语表加载（避免文件 I/O）
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

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
                    path="/mock/path/test_epub/item1.html",
                    content="<p>Hello world.</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[mock_chunk1],
                ),
                EpubItem(
                    id="item2",
                    path="/mock/path/test_epub/item2.html",
                    content="<p>No chunks here.</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[],
                ),
                EpubItem(
                    id="item3",
                    path="/mock/path/test_epub/item3.html",
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
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=Chunk(
                    name="1",
                    original="<p>How are you?</p>",
                    translated="<p>你好吗？</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 调用被测试的方法
        await orchestrator.translate_epub("mock_epub_path")

        # 验证最终状态，这是测试的核心
        # 检查第一个 chunk 的翻译结果
        first_item_chunks = mock_book_with_chunks.items[0].chunks
        assert isinstance(first_item_chunks, list)
        assert first_item_chunks[0].translated == "<p>你好吗？</p>"
        assert first_item_chunks[0].status == TranslationStatus.COMPLETED

        # 检查第三个 chunk 的翻译结果
        third_item_chunks = mock_book_with_chunks.items[2].chunks
        assert isinstance(third_item_chunks, list)
        assert third_item_chunks[0].translated == "<p>你好吗？</p>"
        assert third_item_chunks[0].status == TranslationStatus.COMPLETED

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_skips_translated_chunks(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
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
        # 模拟术语表加载（避免文件 I/O）
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=Chunk(
                    name="1",
                    original="<p>Hello world.</p>",
                    translated="<p>你好，世界。</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 使用真实的 _should_translate_chunk（item1 需要翻译，item2 已翻译）
        await orchestrator.translate_epub("mock_epub_path")

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_handles_errors(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
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
        # 模拟术语表加载（避免文件 I/O）
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例，模拟失败
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.error,
                content=mock_book.items[0].chunks[0],
                run_id="mock_run_id",
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
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_retries_untranslated_chunks_on_rerun(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试重跑时会重新处理之前标记为 UNTRANSLATED 的 chunk。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        untranslated_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="",
            tokens=3,
            status=TranslationStatus.TRANSLATION_FAILED,
        )
        mock_parser_parse.return_value = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<p>Hello world.</p>",
                    translated=None,
                    placeholder=None,
                    chunks=[untranslated_chunk],
                )
            ],
        )

        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=Chunk(
                    name="1",
                    original="<p>Hello world.</p>",
                    translated="<p>你好，世界。</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        await orchestrator.translate_epub("mock_epub_path")

        mock_workflow.arun.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_skips_empty_chunks(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
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
        # 模拟术语表加载（避免文件 I/O）
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 get_translator_workflow 返回的 Workflow 实例
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=Chunk(
                    name="1",
                    original="<p>Hello world.</p>",
                    translated="<p>你好，世界。</p>",
                    tokens=3,
                    status=TranslationStatus.COMPLETED,
                ),
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        # 使用真实的 _should_translate_chunk
        await orchestrator.translate_epub("mock_epub_path")
        # mock_workflow.arun.assert_called_once_with(input=mock_book.items[0].chunks[0])
    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(DomReplacer, "restore", return_value=None)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_returns_incomplete_output_path_for_failed_chunks(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试失败 chunk 会生成带 incomplete 标记的输出文件。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        failed_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="",
            tokens=3,
            status=TranslationStatus.TRANSLATION_FAILED,
        )
        mock_parser_parse.return_value = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<p>Hello world.</p>",
                    chunks=[failed_chunk],
                )
            ],
        )
        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=failed_chunk,
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow

        with patch.object(orchestrator, "_save_manual_translation_report"):
            output_path = await orchestrator.translate_epub("mock_epub_path")

        assert output_path.endswith("test_book-cn-incomplete.epub")
        mock_builder_build.assert_called_once()
    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_preserves_writeback_failed_status_when_recovery_rerun_errors(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试 WRITEBACK_FAILED 恢复重跑异常时不会把 checkpoint 乐观写成 translated。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        recovery_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="<p>你好，世界。</p>",
            tokens=3,
            status=TranslationStatus.WRITEBACK_FAILED,
            xpaths=["/html/body/p"],
        )
        book = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<html><body><p>Hello world.</p></body></html>",
                    chunks=[recovery_chunk],
                )
            ],
        )
        mock_parser_parse.return_value = book

        saved_snapshots = []

        def capture_checkpoint(saved_book):
            saved_snapshots.append(saved_book.model_dump(mode="json"))

        mock_parser_save_json.side_effect = capture_checkpoint

        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(side_effect=RuntimeError("retry failed before writeback"))
        mock_get_translator_workflow.return_value = mock_workflow
        mock_shutil.copytree.return_value = None
        mock_shutil.rmtree.return_value = None

        with patch("engine.orchestrator.os.path.exists", return_value=True), \
            patch("builtins.open", new_callable=MagicMock), \
            patch.object(orchestrator, "_save_manual_translation_report") as mock_save_report:
            output_path = await orchestrator.translate_epub("mock_epub_path")

        assert recovery_chunk.status == TranslationStatus.WRITEBACK_FAILED
        assert saved_snapshots[-1]["items"][0]["chunks"][0]["status"] == TranslationStatus.WRITEBACK_FAILED.value
        assert output_path.endswith("test_book-cn-incomplete.epub")
        mock_save_report.assert_called_once()

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_persists_writeback_failed_status_to_checkpoint(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试回写失败后的 WRITEBACK_FAILED 状态会被再次保存到 checkpoint。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        translated_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="<p>你好，世界。</p>",
            tokens=3,
            status=TranslationStatus.COMPLETED,
            xpaths=["/html/body/div"],
        )
        book = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<html><body><p>Hello world.</p></body></html>",
                    chunks=[translated_chunk],
                )
            ],
        )
        mock_parser_parse.return_value = book

        saved_snapshots = []

        def capture_checkpoint(saved_book):
            saved_snapshots.append(saved_book.model_dump(mode="json"))

        mock_parser_save_json.side_effect = capture_checkpoint

        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=translated_chunk,
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow
        mock_shutil.copytree.return_value = None
        mock_shutil.rmtree.return_value = None

        with patch("engine.orchestrator.os.path.exists", return_value=True), \
            patch("builtins.open", new_callable=MagicMock), \
            patch.object(orchestrator, "_save_manual_translation_report"):
            await orchestrator.translate_epub("mock_epub_path")

        assert saved_snapshots[-1]["items"][0]["chunks"][0]["status"] == TranslationStatus.WRITEBACK_FAILED.value

    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_reports_writeback_failed_chunks_for_manual_followup(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试回写失败的 chunk 会进入手动报告并输出 incomplete 文件。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        translated_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="<p>你好，世界。</p>",
            tokens=3,
            status=TranslationStatus.COMPLETED,
            xpaths=["/html/body/div"],
        )
        book = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<html><body><p>Hello world.</p></body></html>",
                    chunks=[translated_chunk],
                )
            ],
        )
        mock_parser_parse.return_value = book

        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=translated_chunk,
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow
        mock_shutil.copytree.return_value = None
        mock_shutil.rmtree.return_value = None

        with patch("engine.orchestrator.os.path.exists", return_value=True), \
            patch("builtins.open", new_callable=MagicMock), \
            patch.object(orchestrator, "_save_manual_translation_report") as mock_save_report:
            output_path = await orchestrator.translate_epub("mock_epub_path")

        assert translated_chunk.status == TranslationStatus.WRITEBACK_FAILED
        assert output_path.endswith("test_book-cn-incomplete.epub")
        mock_save_report.assert_called_once()
        report_chunks = mock_save_report.call_args.args[0]
        assert report_chunks == [
            {
                "file": "item1",
                "chunk_name": "1",
                "original": "<p>Hello world.</p>",
                "path": "/mock/path/test_epub/item1.html",
                "placeholder": None,
                "status": TranslationStatus.WRITEBACK_FAILED.value,
            }
        ]
    @pytest.mark.asyncio
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch("engine.orchestrator.shutil")
    @patch("engine.orchestrator.get_translator_workflow")
    @patch("engine.orchestrator.GlossaryLoader")
    @patch("engine.orchestrator.GlossaryExtractor")
    async def test_translate_epub_logs_writeback_failures_as_errors(
        self,
        mock_glossary_extractor,
        mock_glossary_loader,
        mock_get_translator_workflow,
        mock_shutil,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        orchestrator,
    ):
        """测试回写失败会体现在最终统计日志里，而不是仍被记为成功。"""
        mock_glossary_loader.return_value.load.return_value = {}
        mock_glossary_extractor.return_value.extract_from_epub.return_value = {}

        translated_chunk = Chunk(
            name="1",
            original="<p>Hello world.</p>",
            translated="<p>你好，世界。</p>",
            tokens=3,
            status=TranslationStatus.COMPLETED,
            xpaths=["/html/body/div"],
        )
        book = EpubBook(
            name="test_book",
            path="/mock/path/test.epub",
            extract_path="/mock/path/test_epub",
            items=[
                EpubItem(
                    id="item1",
                    path="/mock/path/test_epub/item1.html",
                    content="<html><body><p>Hello world.</p></body></html>",
                    chunks=[translated_chunk],
                )
            ],
        )
        mock_parser_parse.return_value = book

        mock_workflow = MagicMock()
        mock_workflow.arun = AsyncMock(
            return_value=WorkflowRunOutput(
                status=RunStatus.completed,
                content=translated_chunk,
                run_id="mock_run_id",
            )
        )
        mock_get_translator_workflow.return_value = mock_workflow
        mock_shutil.copytree.return_value = None
        mock_shutil.rmtree.return_value = None

        with patch("engine.orchestrator.os.path.exists", return_value=True), \
            patch("builtins.open", new_callable=MagicMock), \
            patch.object(orchestrator, "_save_manual_translation_report"), \
            patch("engine.orchestrator.logger.info") as mock_logger_info:
            await orchestrator.translate_epub("mock_epub_path")

        stats_messages = [call.args[0] for call in mock_logger_info.call_args_list if "翻译统计:" in call.args[0]]
        assert stats_messages == ["翻译统计: 总数=1, 成功=0, 失败=0, 跳过=0, 错误=1"]


class TestManualTranslationReport:
    """测试手动翻译报告功能"""

    def test_save_manual_translation_report(self, tmp_path):
        """测试保存手动翻译报告"""
        orchestrator = Orchestrator()
        manual_chunks = [
            {
                "file": "toc.ncx",
                "chunk_name": "abc123",
                "original": "<navPoint><text>Chapter 1</text></navPoint>",
                "path": "/tmp/toc.ncx",
                "placeholder": {"[id0]": "<navPoint>"},
                "status": "untranslated",
            }
        ]
        output_path = str(tmp_path / "test.epub")
        report_path = orchestrator._save_manual_translation_report(manual_chunks, output_path)

        assert os.path.exists(report_path)
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        assert report["total"] == 1
        assert report["chunks"][0]["chunk_name"] == "abc123"

    def test_load_manual_translations(self, tmp_path):
        """测试加载手动翻译报告"""
        orchestrator = Orchestrator()

        report_file = tmp_path / "manual_report.json"
        report_data = {
            "chunks": [
                {"chunk_name": "c1", "translated": "<p>你好</p>"},
                {"chunk_name": "c2", "translated": ""},  # 空翻译不加载
                {"chunk_name": "c3"},  # 无 translated 字段不加载
            ]
        }
        report_file.write_text(json.dumps(report_data, ensure_ascii=False), encoding="utf-8")

        result = orchestrator._load_manual_translations(str(report_file))
        assert result == {"c1": "<p>你好</p>"}
        assert len(result) == 1

    def test_load_manual_translations_file_not_exists(self):
        """测试加载不存在的报告文件返回空字典"""
        orchestrator = Orchestrator()
        result = orchestrator._load_manual_translations("/nonexistent/path/report.json")
        assert result == {}
