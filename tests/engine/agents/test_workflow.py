from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.workflow import Workflow

from engine.agents.schemas import ProofreadingResult, TranslationResponse
from engine.agents.validator import validate_placeholders
from engine.agents.workflow import (
    _get_placeholder_indices,
    _get_placeholders_from_indices,
    _has_translatable_content,
    apply_corrections_step,
    get_translator_workflow,
    proofread_step,
    translate_step,
)
from engine.core.logger import engine_logger as logger
from engine.item.placeholder import PlaceholderManager
from engine.schemas import Chunk, TranslationStatus


class MockTranslationResponse(TranslationResponse):
    def __init__(self, translation: str):
        super().__init__(translation=translation)


class MockProofreadingResult(ProofreadingResult):
    def __init__(self, corrections: dict):
        super().__init__(corrections=corrections)


@pytest.mark.asyncio
class TestWorkflow:
    @pytest.fixture(autouse=True)
    def mock_placeholder_mgr(self):
        """创建测试用 PlaceholderManager"""
        mgr = PlaceholderManager()
        mgr.tag_map = {
            "[id0]": "<p>",
            "[id1]": "</p>",
        }
        mgr.counter = 2
        return mgr

    @pytest.fixture(autouse=True)
    def mock_chunk_factory(self):
        def _factory(name, original, translated=None, tokens=0, status=TranslationStatus.PENDING, global_indices=None, local_tag_map=None):
            return Chunk(
                name=name,
                original=original,
                translated=translated,
                tokens=tokens,
                status=status,
                global_indices=global_indices or [],
                local_tag_map=local_tag_map or {}
            )
        return _factory

    async def test_get_placeholder_indices(self, mock_chunk_factory):
        text = "Hello [id0], your code is [id1]."
        indices = _get_placeholder_indices(text)
        assert indices == [0, 1]

    async def test_get_placeholders_from_indices(self, mock_chunk_factory):
        """验证索引列表转换为占位符字符串列表"""
        indices = [0, 1, 2]
        result = _get_placeholders_from_indices(indices)
        assert result == ["[id0]", "[id1]", "[id2]"]

    async def test_has_translatable_content(self, mock_chunk_factory):
        assert _has_translatable_content("Hello [id0] World") is True
        assert _has_translatable_content("[id0][id1]") is False
        assert _has_translatable_content("   ") is False

    async def test_validate_placeholders_success(self, mock_chunk_factory):
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id0] 世界 [id1]"
        is_valid, _ = validate_placeholders(translated, tag_map)
        assert is_valid is True

    async def test_validate_placeholders_failure(self, mock_chunk_factory):
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id0] 世界 [id2]"  # 缺少 [id1]
        is_valid, _ = validate_placeholders(translated, tag_map)
        assert is_valid is False

    async def test_validate_placeholders_duplicate_in_translated(self, mock_chunk_factory):
        """验证译文包含重复占位符时被检测为无效"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id0] 世界 [id0]"  # [id1] 缺失，[id0] 重复
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False
        assert "缺少" in error_msg

    async def test_validate_placeholders_order_mismatch(self, mock_chunk_factory):
        """验证占位符顺序不同时被检测为无效"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id1] 世界 [id0]"  # 顺序错误
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False
        assert "顺序错误" in error_msg

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_success(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(content=MockTranslationResponse("[id0]你好[id1]"))
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert isinstance(output.content, Chunk)
        assert output.content.status == TranslationStatus.TRANSLATED
        assert "[id0]" in output.content.translated

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_placeholder_count_in_input(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        """验证 translator_input 包含 placeholder_count 字段"""
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]World[id2]",
            tokens=10,
            global_indices=[0, 1, 2],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<p>"}
        )
        captured_input = {}

        async def capture_arun(json_input):
            captured_input["json_str"] = json_input
            return MagicMock(content=MockTranslationResponse("[id0]你好[id1]世界[id2]"))

        mock_translator = MagicMock()
        mock_translator.arun = capture_arun
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        assert output.content.status == TranslationStatus.TRANSLATED
        import json
        parsed = json.loads(captured_input["json_str"])
        assert "placeholder_count" in parsed
        assert parsed["placeholder_count"] == 3
        assert "untranslatable_placeholders" in parsed
        assert "[id0]" in parsed["untranslatable_placeholders"]
        assert "[id1]" in parsed["untranslatable_placeholders"]
        assert "[id2]" in parsed["untranslatable_placeholders"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_content_safety_fallback(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        """验证翻译遇到内容安全错误时自动切换到备用模型"""
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )
        call_count = [0]

        async def safety_error_then_success(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第一次：模拟内容安全审核错误
                return MagicMock(status="error", content="相关法律法规不予显示")
            return MagicMock(content=MockTranslationResponse("[id0]你好[id1]"))

        mock_translator = MagicMock()
        mock_translator.arun = safety_error_then_success
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        # 备用模型翻译成功
        assert output.content.status == TranslationStatus.TRANSLATED
        assert call_count[0] == 2

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_content_safety_fallback(self, mock_get_proofer, mock_chunk_factory):
        """验证校对遇到内容安全错误时自动切换到备用模型"""
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def safety_error_then_success(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(success=False, content="相关法律法规不予显示")
            return MagicMock(content=MockProofreadingResult({"你好": "您好"}))

        mock_proofer = MagicMock()
        mock_proofer.arun = safety_error_then_success
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)
        assert output.success is True
        assert call_count[0] == 2

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_all_retries_fail(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        """验证所有重试都失败后，chunk.translated 为空，状态为 UNTRANSLATED"""
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]World[id2]",
            tokens=10,
            global_indices=[0, 1, 2],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<p>"}
        )
        call_count = [0]

        async def all_fail_response(json_input):
            call_count[0] += 1
            # 始终返回无占位符的翻译
            return MagicMock(content=MockTranslationResponse("你好世界"))

        mock_translator = MagicMock()
        mock_translator.arun = all_fail_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        # 所有重试失败，translated 为空，状态为 UNTRANSLATED
        assert output.content.status == TranslationStatus.UNTRANSLATED
        assert output.content.translated == ""
        assert call_count[0] == 3

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_error_msg_passed(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        """验证上一次翻译失败时的错误信息被传递给下一次重试"""
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]World[id2]",
            tokens=10,
            global_indices=[0, 1, 2],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<p>"}
        )
        captured_inputs = []

        async def capture_and_fail(json_input):
            import json
            parsed = json.loads(json_input)
            captured_inputs.append(parsed)
            # 前两次返回无占位符的翻译
            if len(captured_inputs) <= 2:
                return MagicMock(content=MockTranslationResponse("你好世界"))
            # 第三次成功保留占位符
            return MagicMock(content=MockTranslationResponse("[id0]你好[id1]世界[id2]"))

        mock_translator = MagicMock()
        mock_translator.arun = capture_and_fail
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        assert output.success is True
        # 第2次和第3次重试应该收到 validation_error 字段
        assert len(captured_inputs) >= 2
        assert "validation_error" in captured_inputs[1]
        assert "缺少" in captured_inputs[1]["validation_error"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_already_translated(self, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.translated == "[id0]你好[id1]"
        mock_get_translator.assert_not_called()

    async def test_translate_step_no_placeholder_mgr(self, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            tokens=10,
        )
        step_input = MagicMock(input=chunk, additional_data={})
        output = await translate_step(step_input)
        assert output.success is False
        assert "缺少 placeholder_mgr" in output.error

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_success(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(
            return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"}))
        )
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)
        assert output.success is True
        assert isinstance(output.content, dict)
        assert isinstance(output.content["chunk"], Chunk)
        assert isinstance(output.content["proofreading_result"], ProofreadingResult)
        assert output.content["proofreading_result"].corrections == {"你好": "您好"}

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_no_translated_text(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="[id0]Hello[id1]", tokens=10)
        step_input = MagicMock(previous_step_content=chunk)
        with patch.object(logger, "error"):
            output = await proofread_step(step_input)
            assert output.success is False
            assert "没有从上一步收到有效的翻译文本" in output.error

    async def test_apply_corrections_step_success(self, mock_chunk_factory):
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]您好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"你好": "您好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)
        with patch.object(logger, "info"):
            output = apply_corrections_step(step_input)
            assert output.success is True
            assert isinstance(output.content, Chunk)
            # 注意：后处理会把 "您" 替换为 "你"，所以 "您好" → "你好"
            assert output.content.translated == "[id0]你好[id1]你好[id1]"
            assert output.content.status == TranslationStatus.COMPLETED

    async def test_apply_corrections_step_no_translated_text(self, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="[id0]Hello[id1]", tokens=10)
        proofreading_result = MockProofreadingResult({})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)
        with patch.object(logger, "error") as mock_error:
            output = apply_corrections_step(step_input)
            assert output.success is False
            assert "缺少翻译文本" in output.error

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_success(self, mock_get_proofer, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr):
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(content=MockTranslationResponse("[id0]你好[id1]"))
        )
        mock_get_translator.return_value = mock_translator

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(
            return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"}))
        )
        mock_get_proofer.return_value = mock_proofer

        workflow: Workflow = get_translator_workflow()
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )

        response = await workflow.arun(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr, "glossary": {}}
        )
        assert response.status == "COMPLETED"
        assert isinstance(response.content, Chunk)
        assert response.content.status == TranslationStatus.COMPLETED
        assert "[id0]" in response.content.translated

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_already_translated(
        self, mock_get_proofer, mock_get_translator, mock_chunk_factory, mock_placeholder_mgr
    ):
        workflow: Workflow = get_translator_workflow()
        chunk = mock_chunk_factory(
            name="test_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(
            return_value=MagicMock(content=MockProofreadingResult({"你好": "您好"}))
        )
        mock_get_proofer.return_value = mock_proofer

        response = await workflow.arun(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        assert response.status == "COMPLETED"
        assert response.content.status == TranslationStatus.COMPLETED
        mock_get_translator.assert_not_called()
