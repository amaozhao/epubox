"""Tests for the translation workflow"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from engine.agents.schemas import ProofreadingResult
from engine.agents.workflow import (
    apply_corrections_step,
    translate_step,
    validation_step,
    proofread_step,
    _translate_with_fallback,
    _call_translator,
)
from engine.schemas import Chunk, TranslationStatus
from agno.workflow import StepInput


class TestApplyCorrectionsStep:
    """测试 apply_corrections_step 的状态转换逻辑"""

    def test_translated_status_should_become_completed(self):
        """当 chunk.status == TRANSLATED 时，应该设置为 COMPLETED"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
            tokens=10,
        )

        step_input = StepInput(
            previous_step_content={
                "chunk": chunk,
                "validation_error": None,
                "proofreading_result": ProofreadingResult(corrections={}),
            }
        )

        result = apply_corrections_step(step_input)
        result_chunk = result.content

        # 关键断言：TRANSLATED 状态应该变成 COMPLETED
        assert result_chunk.status == TranslationStatus.COMPLETED, (
            f"Expected status to be COMPLETED, got {result_chunk.status}"
        )

    def test_untranslated_status_stays_untranslated(self):
        """当 chunk.status == UNTRANSLATED 时，应该保持不变"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated="",
            status=TranslationStatus.UNTRANSLATED,
            tokens=10,
        )

        step_input = StepInput(
            previous_step_content={
                "chunk": chunk,
                "validation_error": "some error",
                "proofreading_result": ProofreadingResult(corrections={}),
            }
        )

        result = apply_corrections_step(step_input)
        result_chunk = result.content

        assert result_chunk.status == TranslationStatus.UNTRANSLATED

    def test_completed_status_stays_completed(self):
        """当 chunk.status == COMPLETED 时，应该保持不变"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.COMPLETED,
            tokens=10,
        )

        step_input = StepInput(
            previous_step_content={
                "chunk": chunk,
                "validation_error": None,
                "proofreading_result": ProofreadingResult(corrections={}),
            }
        )

        result = apply_corrections_step(step_input)
        result_chunk = result.content

        assert result_chunk.status == TranslationStatus.COMPLETED


class TestWorkflowIntegration:
    """测试工作流步骤之间的状态转换"""

    def test_validation_step_with_translated_chunk(self):
        """validation_step 对 TRANSLATED 状态的 chunk 应该返回有效"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
            tokens=10,
        )

        # translate_step returns StepOutput(content=chunk)
        # so validation_step receives the chunk directly
        step_input = StepInput(
            previous_step_content=chunk
        )

        result = validation_step(step_input)
        result_data = result.content

        # validation_step returns {"chunk": chunk, "validation_error": None}
        assert result_data["validation_error"] is None
        assert result_data["chunk"].status == TranslationStatus.TRANSLATED


class TestTranslateWithRetry:
    """测试翻译重试机制"""

    @pytest.mark.asyncio
    async def test_successful_translation_after_retry(self):
        """测试第一次失败，第二次成功的重试"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated=None,
            status=TranslationStatus.PENDING,
            tokens=10,
        )

        # 第一次返回有问题的翻译，第二次返回正确的
        call_count = 0

        from agno.run import RunStatus
        from engine.agents.schemas import TranslationResponse

        class MockResponse:
            def __init__(self, translation):
                self.content = TranslationResponse(translation=translation)
                self.status = RunStatus.completed

        async def mock_arun(input_str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一次：返回缺少闭合标签的翻译
                return MockResponse("<p>你好")
            else:
                # 第二次：返回正确的翻译
                return MockResponse("<p>你好</p>")

        with patch("engine.agents.workflow.get_translator") as mock_get:
            mock_agent = MagicMock()
            mock_agent.arun = mock_arun
            mock_get.return_value = mock_agent

            result = await _translate_with_fallback(chunk, {})

        # 第二次重试后成功
        assert result.status == TranslationStatus.TRANSLATED
        assert result.translated == "<p>你好</p>"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_fail(self):
        """测试所有重试都失败"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated=None,
            status=TranslationStatus.PENDING,
            tokens=10,
        )

        call_count = 0

        from agno.run import RunStatus
        from engine.agents.schemas import TranslationResponse

        class MockResponse:
            def __init__(self, translation):
                self.content = TranslationResponse(translation=translation)
                self.status = RunStatus.completed

        async def mock_arun(input_str):
            nonlocal call_count
            call_count += 1
            # 每次都返回有问题的翻译
            return MockResponse("<p>你好")  # 缺少闭合标签

        with patch("engine.agents.workflow.get_translator") as mock_get:
            mock_agent = MagicMock()
            mock_agent.arun = mock_arun
            mock_get.return_value = mock_agent

            result = await _translate_with_fallback(chunk, {})

        # 3 次重试后仍然失败
        assert result.status == TranslationStatus.UNTRANSLATED
        assert call_count == 3


class TestCallTranslatorUnicodeEscape:
    """测试 _call_translator 处理 unicode_escape 解码错误"""

    @pytest.mark.asyncio
    async def test_backslash_path_in_translation(self):
        """测试翻译内容包含反斜杠路径时不会崩溃"""
        from agno.run import RunStatus
        from engine.agents.schemas import TranslationResponse

        chunk = Chunk(
            name="test",
            original='<code>C:\\Users\\test\\file</code>',
            translated=None,
            status=TranslationStatus.PENDING,
            tokens=10,
        )

        class MockResponse:
            def __init__(self, translation):
                self.content = TranslationResponse(translation=translation)
                self.status = RunStatus.completed

        async def mock_arun(input_str):
            # 返回包含反斜杠的翻译
            return MockResponse('<code>C:\\Users\\test\\file</code>')

        with patch("engine.agents.workflow.get_translator") as mock_get:
            mock_agent = MagicMock()
            mock_agent.arun = mock_arun
            mock_get.return_value = mock_agent

            # 不应该抛出异常
            result = await _call_translator(chunk.original, {})
            assert result is not None
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_unicode_escape_decode_failure(self):
        """测试翻译响应包含无法解码的 unicode_escape 时使用原始内容"""
        from agno.run import RunStatus
        from engine.agents.schemas import TranslationResponse

        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated=None,
            status=TranslationStatus.PENDING,
            tokens=10,
        )

        class MockResponse:
            def __init__(self, translation):
                self.content = TranslationResponse(translation=translation)
                self.status = RunStatus.completed

        async def mock_arun(input_str):
            # 返回包含无效 unicode_escape 的翻译（如反斜杠结尾）
            return MockResponse('"translation": "C:\\\\Users\\\\"')  # 末尾的反斜杠无法解码

        with patch("engine.agents.workflow.get_translator") as mock_get:
            mock_agent = MagicMock()
            mock_agent.arun = mock_arun
            mock_get.return_value = mock_agent

            # 应该能处理，不抛出异常
            result = await _call_translator(chunk.original, {})
            # result 可能是部分解析的内容或原始内容
            assert result is not None
            assert isinstance(result, str)


class TestWorkflowStatusLogic:
    """测试 workflow 各步骤基于状态的跳过逻辑"""

    @pytest.mark.asyncio
    async def test_translate_step_skips_non_pending(self):
        """translate_step 对非 PENDING 状态应该跳过"""
        for status in [TranslationStatus.TRANSLATED, TranslationStatus.COMPLETED, TranslationStatus.UNTRANSLATED]:
            chunk = Chunk(
                name="test",
                original="<p>Hello</p>",
                translated="<p>你好</p>",
                status=status,
                tokens=5,
            )
            step_input = StepInput(input=chunk, additional_data={})
            result = await translate_step(step_input)
            # 应该跳过，返回原 chunk
            assert result.content["chunk"].status == status

    def test_validation_step_skips_non_translated(self):
        """validation_step 对非 TRANSLATED 状态应该跳过"""
        for status in [TranslationStatus.PENDING, TranslationStatus.COMPLETED, TranslationStatus.UNTRANSLATED]:
            chunk = Chunk(
                name="test",
                original="<p>Hello</p>",
                translated="<p>你好</p>",
                status=status,
                tokens=5,
            )
            step_input = StepInput(previous_step_content={"chunk": chunk, "validation_error": None})
            result = validation_step(step_input)
            # 应该跳过
            assert result.content["validation_error"] is None

    def test_validation_step_fails_and_sets_untranslated(self):
        """validation_step 验证失败时应该设置 status 为 UNTRANSLATED"""
        chunk = Chunk(
            name="test",
            original="<p>Hello</p>",
            translated="<p>你好",  # 缺少闭合标签
            status=TranslationStatus.TRANSLATED,
            tokens=5,
        )
        step_input = StepInput(previous_step_content={"chunk": chunk, "validation_error": None})
        result = validation_step(step_input)
        # 验证失败，status 应该变成 UNTRANSLATED
        assert result.content["chunk"].status == TranslationStatus.UNTRANSLATED
        assert result.content["validation_error"] is not None

    @pytest.mark.asyncio
    async def test_proofread_step_skips_non_translated(self):
        """proofread_step 对非 TRANSLATED 状态应该跳过"""
        for status in [TranslationStatus.PENDING, TranslationStatus.COMPLETED, TranslationStatus.UNTRANSLATED]:
            chunk = Chunk(
                name="test",
                original="<p>Hello</p>",
                translated="<p>你好</p>",
                status=status,
                tokens=5,
            )
            step_input = StepInput(previous_step_content={
                "chunk": chunk,
                "validation_error": None,
                "proofreading_result": ProofreadingResult(corrections={})
            })
            result = await proofread_step(step_input)
            # 应该跳过
            assert result.content["proofreading_result"].corrections == {}

    def test_apply_corrections_step_skips_non_translated(self):
        """apply_corrections_step 对非 TRANSLATED 状态应该跳过"""
        for status in [TranslationStatus.PENDING, TranslationStatus.COMPLETED, TranslationStatus.UNTRANSLATED]:
            chunk = Chunk(
                name="test",
                original="<p>Hello</p>",
                translated="<p>你好</p>",
                status=status,
                tokens=5,
            )
            step_input = StepInput(previous_step_content={
                "chunk": chunk,
                "proofreading_result": ProofreadingResult(corrections={})
            })
            result = apply_corrections_step(step_input)
            # 应该跳过，status 保持不变
            assert result.content.status == status
