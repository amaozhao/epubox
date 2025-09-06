from unittest.mock import AsyncMock

import pytest
from agno.workflow import RunResponse

from engine.agents.proofer import ProofreadingResult
from engine.agents.translator import TranslationResponse
from engine.agents.workflow import TranslatorWorkflow

# 假设您的源代码位于 'engine' 包中
from engine.schemas import Chunk, TranslationStatus


@pytest.fixture
def workflow() -> TranslatorWorkflow:
    """为每个测试提供一个全新的 TranslatorWorkflow 实例。"""
    # 这里我们实例化工作流，它的 translator 和 proofer 属性
    # 在您的源代码中已经被 mock 掉了 (通过 get_translator 和 get_proofer)。
    return TranslatorWorkflow(session_id="test_session")


@pytest.fixture
def mock_chunk_factory():
    """
    一个用于创建 Chunk 对象的 fixture 工厂，可以按需定制。
    """

    def _factory(name, original, translated=None, tokens=0, status=TranslationStatus.PENDING):
        return Chunk(name=name, original=original, translated=translated, tokens=tokens, status=status)

    return _factory


@pytest.fixture
def mock_agents(mocker):
    """模拟 Translator 和 Proofer 代理。"""
    mock_translator = AsyncMock()
    mock_proofer = AsyncMock()

    # 模拟成功的翻译结果
    mock_translator.arun.return_value = AsyncMock(
        content=TranslationResponse(translation="你好 ##abcde1##，你的代码是 ##abcde2##。")
    )

    # 模拟成功的校对结果
    mock_proofer.arun.return_value = AsyncMock(content=ProofreadingResult(corrections={"你好": "您好", "代码": "代码"}))

    mocker.patch("engine.agents.workflow.get_translator", return_value=mock_translator)
    mocker.patch("engine.agents.workflow.get_proofer", return_value=mock_proofer)

    return mock_translator, mock_proofer


class TestTranslatorWorkflow:
    """
    TranslatorWorkflow 的测试用例集合。
    """

    @pytest.mark.asyncio
    async def test_arun_successful_path_with_corrections(self, workflow: TranslatorWorkflow, mock_chunk_factory):
        """
        测试理想情境：翻译成功，验证通过，且校对员提供了修正建议。
        """
        # Arrange (安排)
        original_text = "Hello ##abc1##, your code is ##abe2##."
        chunk = mock_chunk_factory(name="1", original=original_text)

        # 模拟 translator 的响应
        translated_text = "你好 ##abc1##，你的代码是 ##abe2##。"
        mock_trans_response = RunResponse(content=TranslationResponse(translation=translated_text))
        workflow.translator.arun = AsyncMock(return_value=mock_trans_response)

        # 模拟 proofer 的响应 (有修正)
        corrections = {"您好": "你好", "代码": "代码"}
        mock_proof_response = RunResponse(content=ProofreadingResult(corrections=corrections))
        workflow.proofer.arun = AsyncMock(return_value=mock_proof_response)

        # Act (执行)
        result = await workflow.arun(chunk=chunk)

        # Assert (断言)
        final_text = "你好 ##abc1##，你的代码是 ##abe2##。"
        assert result.content == final_text
        assert chunk.status == TranslationStatus.COMPLETED
        assert chunk.translated == final_text
        workflow.translator.arun.assert_awaited_once()
        workflow.proofer.arun.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_arun_successful_path_no_corrections(self, workflow: TranslatorWorkflow, mock_chunk_factory):
        """
        测试情境：翻译成功，但校对员未发现任何错误。
        """
        # Arrange
        original_text = "Text is perfect ##abc3##."
        chunk = mock_chunk_factory(name="test2", original=original_text)

        translated_text = "文本是完美的 ##abc3##。"
        mock_trans_response = RunResponse(content=TranslationResponse(translation=translated_text))
        workflow.translator.arun = AsyncMock(return_value=mock_trans_response)

        # 模拟 proofer 响应 (无修正)
        mock_proof_response = RunResponse(content=ProofreadingResult(corrections={}))
        workflow.proofer.arun = AsyncMock(return_value=mock_proof_response)

        # Act
        result = await workflow.arun(chunk=chunk)

        # Assert
        assert result.content == translated_text
        # 状态应为 TRANSLATED，因为没有应用修正，所以未进入 COMPLETED 状态
        assert chunk.status == TranslationStatus.COMPLETED
        assert chunk.translated == translated_text

    @pytest.mark.asyncio
    async def test_arun_fails_on_translator_invalid_response_type(
        self, workflow: TranslatorWorkflow, mock_chunk_factory
    ):
        """
        测试情境：当 translator 返回了非预期的格式时，工作流应失败。
        """
        # Arrange
        chunk = mock_chunk_factory(name="test3", original="Some text ##abe4##.")

        # 模拟一个无效的响应 (内容是字符串，而非 Pydantic 模型)
        mock_trans_response = RunResponse(content="This is just a string")
        workflow.translator.arun = AsyncMock(return_value=mock_trans_response)
        workflow.proofer.arun = AsyncMock()  # 为 proofer 设置 mock 以便检查它是否被调用

        # Act
        result = await workflow.arun(chunk=chunk)

        # Assert
        assert result.content == "Translation step failed: The agent returned an unexpected response type."
        assert chunk.status == TranslationStatus.PENDING  # 状态不应改变
        workflow.proofer.arun.assert_not_awaited()  # Proofer 不应该被调用

    @pytest.mark.asyncio
    async def test_arun_fails_on_placeholder_mismatch(self, workflow: TranslatorWorkflow, mock_chunk_factory):
        """
        测试情境：当翻译后的文本与原文的占位符不匹配时，工作流应失败。
        """
        # Arrange
        original_text = "Keep ##abc5## and ##abc6##."
        chunk = mock_chunk_factory(name="test4", original=original_text)

        # 模拟一个缺少占位符的翻译结果
        translated_text_with_mismatch = "保留 ##abc5##。"
        mock_trans_response = RunResponse(content=TranslationResponse(translation=translated_text_with_mismatch))
        workflow.translator.arun = AsyncMock(return_value=mock_trans_response)
        workflow.proofer.arun = AsyncMock()

        # Act
        result = await workflow.arun(chunk=chunk)

        # Assert
        assert result.content == "Translation step failed: Placeholder mismatch detected."
        assert chunk.status == TranslationStatus.PENDING
        assert chunk.translated is None
        workflow.proofer.arun.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arun_fallback_on_proofer_invalid_response_type(
        self, workflow: TranslatorWorkflow, mock_chunk_factory
    ):
        """
        测试情境：当 proofer 返回无效格式时，应触发降级机制，返回未经校对的翻译文本。
        """
        # Arrange
        chunk = mock_chunk_factory(name="test5", original="Text with ##abe7##.")

        translated_text = "带 ##abe7## 的文本。"
        mock_trans_response = RunResponse(content=TranslationResponse(translation=translated_text))
        workflow.translator.arun = AsyncMock(return_value=mock_trans_response)

        # 模拟一个来自 proofer 的无效响应
        mock_proof_response = RunResponse(content={"invalid": "dict"})
        workflow.proofer.arun = AsyncMock(return_value=mock_proof_response)

        # Act
        result = await workflow.arun(chunk=chunk)

        # Assert
        assert result.content == translated_text  # 应返回原始的翻译结果
        assert chunk.status == TranslationStatus.TRANSLATED  # 在 proofer 失败前，状态已更新
        assert chunk.translated == translated_text

    @pytest.mark.parametrize(
        "original, translated, expected",
        [
            ("Hello ##abe1##", "你好 ##abe1##", True),
            ("Hello ##ade1##", "你好", False),
            ("<p>##ade2##</p>", "<p>##ade3##</p>", False),
            ("Check ##ade4## and ##ade5##", "检查 ##ade5## 和 ##ade4##", True),  # 顺序不同但集合相同
            ("No placeholders here.", "这里没有占位符。", True),
        ],
    )
    def test_internal_validate_method(self, workflow: TranslatorWorkflow, original, translated, expected):
        """
        直接对内部辅助方法 `_validate` 进行单元测试，确保其逻辑的健壮性。
        """
        assert workflow._validate(original, translated) == expected
