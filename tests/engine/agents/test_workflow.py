from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.workflow import Workflow

from engine.agents.schemas import ProofreadingResult, TranslationResponse
from engine.agents.workflow import (
    _get_placeholders,
    _validate_placeholders,
    apply_corrections_step,
    get_translator_workflow,
    proofread_step,
    translate_step,
)
from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus


# Mock TranslationResponse and ProofreadingResult for testing
class MockTranslationResponse(TranslationResponse):
    def __init__(self, translation: str):
        super().__init__(translation=translation)


class MockProofreadingResult(ProofreadingResult):
    def __init__(self, corrections: dict):
        super().__init__(corrections=corrections)


@pytest.mark.asyncio
class TestWorkflow:
    @pytest.fixture(autouse=True)
    def mock_chunk_factory(self):
        """
        一个用于创建 Chunk 对象的 fixture 工厂，可以按需定制。
        """

        def _factory(name, original, translated=None, tokens=0, status=TranslationStatus.PENDING):
            return Chunk(name=name, original=original, translated=translated, tokens=tokens, status=status)

        return _factory

    async def test_get_placeholders(self, mock_chunk_factory):
        text = "Hello ##abcd##, your code is ##1234##."
        placeholders = _get_placeholders(text)
        expected = ["##abcd##", "##1234##"]
        assert placeholders == expected, f"Expected placeholders {expected}, got {placeholders}"

    async def test_validate_placeholders_success(self, mock_chunk_factory):
        original = "Hello ##abcd##, your code is ##1234##."
        translated = "你好 ##abcd##，你的代码是 ##1234##。"
        assert _validate_placeholders(original, translated) is True

    async def test_validate_placeholders_failure(self, mock_chunk_factory):
        original = "Hello ##abcd##, your code is ##1234##."
        translated = "你好 ##abcd##，你的代码是 ##wxyz##。"
        with patch.object(logger, "error") as mock_error:
            assert _validate_placeholders(original, translated) is False
            assert mock_error.call_count == 3  # Logs mismatch and lists

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_success(self, mock_get_translator, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=10)
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(return_value=MagicMock(content=MockTranslationResponse("你好 ##abcd##。")))
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk)
        output = await translate_step(step_input)
        assert output.success is True
        assert isinstance(output.content, Chunk)
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "你好 ##abcd##。"
        assert output.content.name == "test_chunk"
        assert output.content.tokens == 10

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_already_translated(self, mock_get_translator, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="Hello ##abcd##.",
            translated="你好 ##abcd##。",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        step_input = MagicMock(input=chunk)
        output = await translate_step(step_input)
        assert output.success is True
        assert isinstance(output.content, Chunk)
        assert output.content.translated == "你好 ##abcd##。"
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.name == "test_chunk"
        assert output.content.tokens == 10
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_placeholder_mismatch(self, mock_get_translator, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=5)
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(return_value=MagicMock(content=MockTranslationResponse("你好 ##wxyz##。")))
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk)
        with patch.object(logger, "error") as mock_error:
            output = await translate_step(step_input)
            assert output.success is False
            assert output.error == "翻译步骤失败：检测到占位符不匹配。"
            assert isinstance(output.content, Chunk)
            assert output.content.name == "test_chunk"
            assert output.content.tokens == 5
            assert mock_error.call_count == 4  # Error + lists + final error

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_unexpected_response(self, mock_get_translator, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=10)
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(return_value=MagicMock(content="invalid"))  # Not TranslationResponse
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk)
        with patch.object(logger, "error") as mock_error:
            output = await translate_step(step_input)
            assert output.success is False
            assert output.error == "翻译步骤失败：代理返回了意外的响应类型。"
            assert isinstance(output.content, Chunk)
            assert output.content.name == "test_chunk"
            assert output.content.tokens == 10
            assert mock_error.call_count == 1

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_success(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="Hello ##abcd##.",
            translated="你好 ##abcd##。",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"})))
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)
        assert output.success is True
        assert isinstance(output.content, dict)
        assert isinstance(output.content["chunk"], Chunk)
        assert isinstance(output.content["proofreading_result"], ProofreadingResult)
        assert output.content["proofreading_result"].corrections == {"你好": "您好"}
        assert output.content["chunk"].name == "test_chunk"
        assert output.content["chunk"].tokens == 10

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_no_translated_text(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=10)
        step_input = MagicMock(previous_step_content=chunk)
        with patch.object(logger, "error") as mock_error:
            output = await proofread_step(step_input)
            assert output.success is False
            assert output.error == "校对步骤失败：没有从上一步收到有效的翻译文本。"
            assert isinstance(output.content, dict)
            assert isinstance(output.content["chunk"], Chunk)
            assert output.content["chunk"].name == "test_chunk"
            assert output.content["chunk"].tokens == 10
            assert mock_error.call_count == 1

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_unexpected_response(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="Hello ##abcd##.",
            translated="你好 ##abcd##。",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(return_value=MagicMock(content="invalid"))  # Not ProofreadingResult
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content=chunk)
        with patch.object(logger, "error") as mock_error:
            output = await proofread_step(step_input)
            assert output.success is True  # Falls back to empty ProofreadingResult
            assert isinstance(output.content, dict)
            assert isinstance(output.content["chunk"], Chunk)
            assert output.content["proofreading_result"].corrections == {}
            assert output.content["chunk"].name == "test_chunk"
            assert output.content["chunk"].tokens == 10
            assert mock_error.call_count == 1

    async def test_apply_corrections_step_success(self, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="Hello ##abcd##.",
            translated="你好 ##abcd##。您好。",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"你好": "您好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)
        with patch.object(logger, "info") as mock_info:
            output = apply_corrections_step(step_input)
            assert output.success is True
            assert isinstance(output.content, Chunk)
            assert output.content.translated == "你好 ##abcd##。你好。"
            assert output.content.status == TranslationStatus.COMPLETED
            assert output.content.name == "test_chunk"
            assert output.content.tokens == 10
            assert mock_info.call_count == 2  # Discovered corrections + applied

    async def test_apply_corrections_step_no_translated_text(self, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=10)
        proofreading_result = MockProofreadingResult({})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)
        with patch.object(logger, "error") as mock_error:
            output = apply_corrections_step(step_input)
            assert output.success is False
            assert output.error == "应用校对建议步骤失败：缺少翻译文本。"
            assert isinstance(output.content, Chunk)
            assert output.content.name == "test_chunk"
            assert output.content.tokens == 10
            assert mock_error.call_count == 1

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_success(self, mock_get_proofer, mock_get_translator, mock_chunk_factory):
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(content=MockTranslationResponse("你好 ##abcd##。您好。"))
        )
        mock_get_translator.return_value = mock_translator

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"})))
        mock_get_proofer.return_value = mock_proofer

        workflow: Workflow = get_translator_workflow()
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=10)

        response = await workflow.arun(input=chunk)
        assert response.status == "COMPLETED"
        assert isinstance(response.content, Chunk)
        assert response.content.status == TranslationStatus.COMPLETED
        assert response.content.translated == "你好 ##abcd##。你好。"
        assert response.content.name == "test_chunk"
        assert response.content.tokens == 10

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_with_placeholder_mismatch(
        self, mock_get_proofer, mock_get_translator, mock_chunk_factory
    ):
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(return_value=MagicMock(content=MockTranslationResponse("你好 ##wxyz##。")))
        mock_get_translator.return_value = mock_translator

        mock_proofer = MagicMock()  # Not called due to early failure
        mock_get_proofer.return_value = mock_proofer

        workflow: Workflow = get_translator_workflow()
        chunk = mock_chunk_factory(name="test_chunk", original="Hello ##abcd##.", tokens=5)

        with patch.object(logger, "error") as mock_error:
            response = await workflow.arun(input=chunk)
            # assert response.status == "failed"  # Commented out as per your code
            assert "占位符不匹配" in str(mock_error.call_args_list)
            assert isinstance(response.content, Chunk)
            assert response.content.name == "test_chunk"
            assert response.content.tokens == 5

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_already_translated(self, mock_get_proofer, mock_get_translator, mock_chunk_factory):
        workflow: Workflow = get_translator_workflow()
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="Hello ##abcd##.",
            translated="你好 ##abcd##。您好。",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"})))
        mock_get_proofer.return_value = mock_proofer

        response = await workflow.arun(input=chunk)
        assert response.status == "COMPLETED"
        assert isinstance(response.content, Chunk)
        assert response.content.status == TranslationStatus.COMPLETED
        assert response.content.translated == "你好 ##abcd##。你好。"
        assert response.content.name == "test_chunk"
        assert response.content.tokens == 10
        mock_get_translator.assert_not_called()
