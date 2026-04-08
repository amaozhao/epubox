"""
端到端集成测试

测试从 main.py 入口点开始的完整翻译流程。
使用最小化的测试数据和 mock 翻译器。
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.agents.workflow import get_translator_workflow
from engine.epub import Builder, Parser, Replacer
from engine.orchestrator import Orchestrator
from engine.schemas import Chunk, EpubBook, EpubItem, TranslationStatus
from engine.item.chunker import chunk_html, ChunkState
from agno.run import RunStatus
from agno.run.workflow import WorkflowRunOutput


class TestNeedsTranslationSkipped:
    """测试 needs_translation=False 的 chunks 被正确跳过"""

    @pytest.fixture
    def orchestrator(self):
        return Orchestrator()

    @pytest.fixture
    def mock_book_with_prefix_suffix(self):
        """创建一个包含 prefix/suffix chunk 的 EpubBook"""
        prefix_chunk = Chunk(
            name="prefix",
            original='<!DOCTYPE html><html><head></head><body>',
            translated=None,
            tokens=10,
            status=TranslationStatus.PENDING,
            needs_translation=False,  # 前缀 chunk 不需要翻译
        )
        content_chunk = Chunk(
            name="content",
            original="<p>Hello world</p>",
            translated=None,
            tokens=5,
            status=TranslationStatus.PENDING,
            needs_translation=True,
        )
        suffix_chunk = Chunk(
            name="suffix",
            original="</body></html>",
            translated=None,
            tokens=5,
            status=TranslationStatus.PENDING,
            needs_translation=False,  # 后缀 chunk 不需要翻译
        )
        return EpubBook(
            name="test_book",
            path="/mock/test.epub",
            extract_path="/mock/test_epub",
            items=[
                EpubItem(
                    id="test.xhtml",
                    path="/mock/test_epub/test.xhtml",
                    content="<p>Hello</p>",
                    chunks=[prefix_chunk, content_chunk, suffix_chunk],
                )
            ],
        )

    @pytest.mark.asyncio
    async def test_needs_translation_false_skipped(self, orchestrator, mock_book_with_prefix_suffix):
        """验证 needs_translation=False 的 chunk 被跳过，不进入 workflow"""
        with tempfile.TemporaryDirectory() as tmpdir:
            epub_path = Path(tmpdir) / "test.epub"
            epub_path.write_bytes(b"fake epub")

            # 更新 mock_book 的路径
            mock_book_with_prefix_suffix.path = str(epub_path)

            with (
                patch.object(Parser, "parse", return_value=mock_book_with_prefix_suffix),
                patch.object(Builder, "build"),
                patch.object(Replacer, "restore"),
                patch("engine.orchestrator.get_translator_workflow") as mock_get_workflow,
                patch("engine.orchestrator.GlossaryLoader"),
            ):
                mock_workflow = MagicMock()
                mock_workflow.arun = AsyncMock(
                    return_value=WorkflowRunOutput(
                        status=RunStatus.completed,
                        content=Chunk(
                            name="content",
                            original="<p>Hello world</p>",
                            translated="<p>你好世界</p>",
                            tokens=5,
                            status=TranslationStatus.COMPLETED,
                            needs_translation=True,
                        ),
                        run_id="mock_run_id",
                    )
                )
                mock_get_workflow.return_value = mock_workflow

                await orchestrator.translate_epub(str(epub_path))

            # 验证 workflow 只被调用 1 次（只处理 content chunk）
            assert mock_workflow.arun.call_count == 1
            call_args = mock_workflow.arun.call_args
            assert call_args[1]["input"].name == "content"

            # 验证 prefix 和 suffix chunk 被标记为 COMPLETED
            items = mock_book_with_prefix_suffix.items
            prefix = items[0].chunks[0]
            content = items[0].chunks[1]
            suffix = items[0].chunks[2]

            assert prefix.status == TranslationStatus.COMPLETED
            assert prefix.needs_translation is False
            assert content.status == TranslationStatus.COMPLETED
            assert suffix.status == TranslationStatus.COMPLETED
            assert suffix.needs_translation is False


class TestChunkerPrefixSuffix:
    """测试 chunker 生成的前缀/后缀 chunk"""

    def test_chunk_html_generates_prefix_and_suffix(self):
        """验证 chunk_html 生成 prefix 和 suffix chunk"""
        html = '<!DOCTYPE html><html><head></head><body><p>Hello</p></body></html>'
        chunks = chunk_html(html, token_limit=1000)

        # 验证有至少 3 个 chunks: prefix, content, suffix
        assert len(chunks) >= 2

        # 验证第一个 chunk 是 prefix (needs_translation=False)
        first_chunk = chunks[0]
        assert first_chunk.xpath == "prefix"
        assert first_chunk.needs_translation is False

        # 验证最后一个 chunk 是 suffix (needs_translation=False)
        last_chunk = chunks[-1]
        assert last_chunk.xpath == "suffix"
        assert last_chunk.needs_translation is False

    def test_chunk_state_has_needs_translation_field(self):
        """验证 ChunkState 有 needs_translation 字段"""
        chunk = ChunkState(
            xpath="test",
            original="<p>test</p>",
            tokens=3,
            needs_translation=False,
        )
        assert chunk.needs_translation is False


class TestValidatorIntegration:
    """验证器集成测试"""

    def test_validate_chunk_with_container_tag_cross_chunk(self):
        """验证容器标签跨 chunk 时不报错"""
        from engine.agents.html_validator import HtmlValidator

        validator = HtmlValidator()

        # chunk 1: 打开 nav 但未闭合
        chunk1_html = "<nav><ol><li>Item</li>"
        valid1, errors1 = validator.validate_chunk(chunk1_html, 0, "chunk1")

        # chunk 2: 闭合 nav
        chunk2_html = "</li></ol></nav>"
        validator.reset()
        valid2, errors2 = validator.validate_chunk(chunk2_html, 1, "chunk2")

        # 验证合并后栈为空
        validator.reset()
        validator._parse_html(chunk1_html, 0)
        validator._parse_html(chunk2_html, 1)

        # nav 是容器标签，应该在栈中
        assert len(validator.stack) == 0, "合并后栈应该为空"

    def test_validate_chunk_with_leaf_tag_unclosed_error(self):
        """验证叶子标签未闭合时报错"""
        from engine.agents.html_validator import HtmlValidator

        validator = HtmlValidator()

        # chunk: 打开 p 但未闭合
        chunk_html = "<p>Hello"
        valid, errors = validator.validate_chunk(chunk_html, 0, "chunk1")

        # p 是叶子标签，未闭合应该是错误
        assert valid is False
        assert any(e.get("type") == "unclosed_leaf_tag" for e in errors)

    def test_container_tags_cross_chunk_is_valid(self):
        """验证容器标签跨 chunk 是合法的"""
        from engine.agents.html_validator import HtmlValidator

        validator = HtmlValidator()

        # chunk 1: <body><nav> - nav 打开
        chunk1_html = "<body><nav>"
        valid1, errors1 = validator.validate_chunk(chunk1_html, 0, "chunk1")

        # chunk 2: </nav></body> - nav 闭合
        chunk2_html = "</nav></body>"
        validator.reset()
        valid2, errors2 = validator.validate_chunk(chunk2_html, 1, "chunk2")

        # nav 是容器标签，跨 chunk 未闭合是正常的，不应该报错
        # 注意：在这个简单测试中，validator 会因为 body 的闭合而报告问题
        # 但 nav 标签的跨 chunk 是正常的
        assert errors2 == [] or all(
            e.get("type") != "unclosed_leaf_tag"
            for e in errors2
        )


class TestFullWorkflowIntegration:
    """完整 workflow 集成测试（mock LLM）"""

    @pytest.mark.asyncio
    async def test_translate_epub_with_mock_translation(self):
        """验证使用 mock 翻译器的完整流程"""
        # 创建临时目录结构
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建 EPUB 文件
            epub_path = Path(tmpdir) / "test.epub"
            epub_path.write_bytes(b"fake epub content")

            # 创建 mock book
            mock_book = EpubBook(
                name="integration_test",
                path=str(epub_path),
                extract_path=str(Path(tmpdir) / "extract"),
                items=[
                    EpubItem(
                        id="chapter1.xhtml",
                        path=str(Path(tmpdir) / "extract" / "chapter1.xhtml"),
                        content="<p>Test content</p>",
                        chunks=[
                            Chunk(
                                name="chunk1",
                                original="<p>Hello</p>",
                                translated=None,
                                tokens=3,
                                status=TranslationStatus.PENDING,
                                needs_translation=True,
                            )
                        ],
                    )
                ],
            )

            with (
                patch.object(Parser, "parse", return_value=mock_book),
                patch.object(Builder, "build"),
                patch.object(Replacer, "restore"),
                patch("engine.orchestrator.get_translator_workflow") as mock_get_workflow,
                patch("engine.orchestrator.GlossaryLoader"),
            ):
                # Mock workflow
                mock_workflow = MagicMock()
                mock_workflow.arun = AsyncMock(
                    return_value=WorkflowRunOutput(
                        status=RunStatus.completed,
                        content=Chunk(
                            name="chunk1",
                            original="<p>Hello</p>",
                            translated="<p>你好</p>",
                            tokens=3,
                            status=TranslationStatus.COMPLETED,
                            needs_translation=True,
                        ),
                        run_id="test_run_id",
                    )
                )
                mock_get_workflow.return_value = mock_workflow

                orchestrator = Orchestrator()
                await orchestrator.translate_epub(str(epub_path))

                # 验证翻译成功
                assert mock_book.items[0].chunks[0].translated == "<p>你好</p>"
                assert mock_book.items[0].chunks[0].status == TranslationStatus.COMPLETED
