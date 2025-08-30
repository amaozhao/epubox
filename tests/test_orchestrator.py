from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 确保这里的导入路径与你的项目结构一致
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
        创建一个包含模拟数据的 EpubBook 实例。
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
                    chunks=[Chunk(name="1", original="<p>Hello world.</p>", translated=None, tokens=3)],
                ),
                EpubItem(
                    id="item2",
                    path="/mock/path/test_epub/item2.html",
                    content="<p>已翻译内容。</p>",
                    chunks=[
                        Chunk(
                            name="1", original="<p>Translated content.</p>", translated="<p>已翻译内容。</p>", tokens=3
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
    @patch("engine.orchestrator.tqdm", side_effect=lambda iterable, **kwargs: iterable)
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.orchestrator.TranslatorWorkflow")
    async def test_translate_epub_successful_translation(
        self,
        mock_workflow_class,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        mock_tqdm,
        orchestrator,
        mock_book,
    ):
        """
        测试 translate_epub 在所有分块都需要翻译时的完整流程。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 TranslatorWorkflow 实例的 arun 方法
        mock_instance = mock_workflow_class.return_value
        mock_instance.arun = AsyncMock(return_value="Mocked Translation")

        # 确保 _should_translate_chunk 总是返回 True
        with patch.object(orchestrator, "_should_translate_chunk", return_value=True) as mock_should_translate_chunk:
            await orchestrator.translate_epub("mock_epub_path")

            # 验证主要依赖项的调用
            mock_parser_parse.assert_called_once()
            mock_tqdm.assert_called_once()
            mock_should_translate_chunk.assert_called()

            # mock_book.items[0] 包含 1 个分块，mock_book.items[1] 包含 1 个分块，mock_book.items[2] 包含 0 个分块
            # _should_translate_chunk 总是返回 True，所以总共调用了 2 次
            assert mock_instance.arun.call_count == 2

            # 验证 save_json 和 restore 在每次循环中都正确调用
            # 因为 item3 的 chunks 为空，这两个方法不会被调用
            assert mock_parser_save_json.call_count == 2
            assert mock_replacer_restore.call_count == 2

            # 验证 Builder 的调用
            mock_builder_build.assert_called_once()

    @pytest.mark.asyncio
    @patch("engine.orchestrator.tqdm", side_effect=lambda iterable, **kwargs: iterable)
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.orchestrator.TranslatorWorkflow")
    async def test_translate_epub_skips_translated_chunks(
        self,
        mock_workflow_class,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        mock_tqdm,
        orchestrator,
        mock_book,
    ):
        """
        测试当分块已被翻译时，translate_epub 能正确跳过。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 TranslatorWorkflow 实例的 arun 方法
        mock_instance = mock_workflow_class.return_value
        mock_instance.arun = AsyncMock(return_value="Mocked Translation")

        # 确保 _should_translate_chunk 返回正确的值 (item1 需要翻译, item2 不需要)
        with patch.object(
            orchestrator, "_should_translate_chunk", side_effect=[True, False]
        ) as mock_should_translate_chunk:
            await orchestrator.translate_epub("mock_epub_path")

            # 验证 _should_translate_chunk 被调用了两次（每次循环一次）
            assert mock_should_translate_chunk.call_count == 2

            # 验证 workflow 的调用次数，应该只有一次 (对于 item1)
            assert mock_instance.arun.call_count == 1

            # 验证 save_json 和 restore 仍然为每个 item 调用
            assert mock_parser_save_json.call_count == 2
            assert mock_replacer_restore.call_count == 2

            # 验证 Builder 的调用
            mock_builder_build.assert_called_once()

    @pytest.mark.asyncio
    @patch("engine.orchestrator.tqdm", side_effect=lambda iterable, **kwargs: iterable)
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.orchestrator.TranslatorWorkflow")
    async def test_translate_epub_handles_errors(
        self,
        mock_workflow_class,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        mock_tqdm,
        orchestrator,
        mock_book,
    ):
        """
        测试当 TranslatorWorkflow 返回错误响应时，translate_epub 能正确处理。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 TranslatorWorkflow 实例的 arun 方法
        mock_instance = mock_workflow_class.return_value
        mock_instance.arun = AsyncMock(return_value="Mocked Error")

        # 确保 _should_translate_chunk 总是返回 True
        with patch.object(orchestrator, "_should_translate_chunk", return_value=True) as mock_should_translate_chunk:
            await orchestrator.translate_epub("mock_epub_path")

            # 验证 workflow 的调用次数
            assert mock_instance.arun.call_count == 2

            # 验证 save_json 和 restore 仍然为每个 item 调用
            assert mock_parser_save_json.call_count == 2
            assert mock_replacer_restore.call_count == 2

            # 验证 Builder 的调用
            mock_builder_build.assert_called_once()

    @pytest.mark.asyncio
    @patch("engine.orchestrator.tqdm", side_effect=lambda iterable, **kwargs: iterable)
    @patch.object(Parser, "parse", new_callable=MagicMock)
    @patch.object(Parser, "save_json", new_callable=MagicMock)
    @patch.object(Builder, "build", new_callable=MagicMock)
    @patch.object(Replacer, "restore", new_callable=MagicMock)
    @patch("engine.orchestrator.TranslatorWorkflow")
    async def test_translate_epub_skips_empty_chunks(
        self,
        mock_workflow_class,
        mock_replacer_restore,
        mock_builder_build,
        mock_parser_save_json,
        mock_parser_parse,
        mock_tqdm,
        orchestrator,
        mock_book,
    ):
        """
        测试当 EpubItem 的 chunks 为空时，translate_epub 能正确跳过翻译流程。
        """
        # 模拟 Parser 的行为
        mock_parser_parse.return_value = mock_book

        # 模拟 TranslatorWorkflow 实例的 arun 方法
        mock_instance = mock_workflow_class.return_value
        mock_instance.arun = AsyncMock(return_value="Mocked Translation")

        # 我们需要确保所有有 chunk 的 item 都被处理，所以 mock 掉 _should_translate_chunk
        with patch.object(orchestrator, "_should_translate_chunk", return_value=True):
            await orchestrator.translate_epub("mock_epub_path")

        # 验证 workflow 应该只调用两次，因为 item3 的 chunks 为空
        assert mock_instance.arun.call_count == 2

        # 验证 save_json 和 restore 仍然为每个 item 调用
        assert mock_parser_save_json.call_count == 2
        assert mock_replacer_restore.call_count == 2

        # 验证 Builder 的调用
        mock_builder_build.assert_called_once()
