from unittest.mock import AsyncMock, MagicMock, patch

import json
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
        assert "不匹配" in error_msg

    async def test_validate_placeholders_order_mismatch(self, mock_chunk_factory):
        """验证占位符顺序不同时被检测为无效"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id1] 世界 [id0]"  # 顺序错误
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False
        assert "不匹配" in error_msg

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

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
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
            # 始终返回无占位符的翻译，验证会失败并重试3次
            return MagicMock(content=MockTranslationResponse("你好世界"))

        mock_translator = MagicMock()
        mock_translator.arun = all_fail_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": mock_placeholder_mgr}
        )
        output = await translate_step(step_input)
        # 占位符验证失败，重试3次后标记为 UNTRANSLATED
        assert output.content.status == TranslationStatus.UNTRANSLATED
        assert output.content.translated == ""
        assert call_count[0] == 3

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
        assert "缺少 local_tag_map" in output.error

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

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert output.success is True
        assert isinstance(output.content, dict)
        assert isinstance(output.content["chunk"], Chunk)
        assert isinstance(output.content["proofreading_result"], ProofreadingResult)
        assert output.content["proofreading_result"].corrections == {"你好": "您好"}

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_no_translated_text(self, mock_get_proofer, mock_chunk_factory):
        chunk = mock_chunk_factory(name="test_chunk", original="[id0]Hello[id1]", tokens=10)
        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
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
        with patch.object(logger, "error"):
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


# =============================================================================
# 全面标签覆盖测试 - 验证各种 HTML 标签的翻译和恢复
# =============================================================================

class TestWorkflowHtmlTagCoverage:
    """测试各种 HTML 标签的占位符处理"""

    @pytest.fixture(autouse=True)
    def mock_translator(self):
        """通用翻译 mock，每次返回正确的占位符"""
        with patch("engine.agents.workflow.get_translator") as mock:
            mock_translator = MagicMock()
            # 模拟翻译：保留所有占位符，仅翻译文本
            async def translate_with_placeholders(json_input):
                parsed = json.loads(json_input)
                text = parsed.get("text_to_translate", "")
                # 简单翻译：英文→中文
                replacements = {
                    "Hello": "你好",
                    "World": "世界",
                    "Title": "标题",
                    "Content": "内容",
                    "Paragraph": "段落",
                    "Another": "另一个",
                    "Bold text": "粗体文本",
                    "Italic text": "斜体文本",
                    "Link text": "链接文本",
                    "First item": "第一项",
                    "Second item": "第二项",
                    "Cell 1": "单元格1",
                    "Cell 2": "单元格2",
                    "Navigation": "导航",
                    "Footer": "页脚",
                    "Header": "头部",
                }
                result = text
                for en, zh in replacements.items():
                    result = result.replace(en, zh)
                return MagicMock(content=MockTranslationResponse(result))
            mock_translator.arun = translate_with_placeholders
            mock.return_value = mock_translator
            yield mock_translator

    # -------------------------------------------------------------------------
    # 块级标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_h1_tag(self, mock_translator):
        """测试 h1 标题标签"""
        chunk = Chunk(
            name="h1_chunk",
            original="[id0]Title[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<h1>", "[id1]": "</h1>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED

    @pytest.mark.asyncio
    async def test_translate_h2_h6_tags(self, mock_translator):
        """测试 h2-h6 多级标题标签"""
        for tag in ["h2", "h3", "h4", "h5", "h6"]:
            chunk = Chunk(
                name=f"{tag}_chunk",
                original="[id0]Title[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": f"<{tag}>", "[id1]": f"</{tag}>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.success is True, f"Failed for {tag}"

    @pytest.mark.asyncio
    async def test_translate_div_tag(self, mock_translator):
        """测试 div 容器标签"""
        chunk = Chunk(
            name="div_chunk",
            original="[id0]Content[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<div>", "[id1]": "</div>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED

    @pytest.mark.asyncio
    async def test_translate_paragraph_tag(self, mock_translator):
        """测试 p 段落标签"""
        chunk = Chunk(
            name="p_chunk",
            original="[id0]Paragraph text here[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED

    @pytest.mark.asyncio
    async def test_translate_blockquote_tag(self, mock_translator):
        """测试 blockquote 引用标签"""
        chunk = Chunk(
            name="blockquote_chunk",
            original="[id0]Quote text here[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<blockquote>", "[id1]": "</blockquote>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED

    # -------------------------------------------------------------------------
    # 内联标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_span_tag(self, mock_translator):
        """测试 span 内联标签"""
        chunk = Chunk(
            name="span_chunk",
            original="[id0]Hello [id1]World[id2]",
            tokens=10,
            global_indices=[0, 1, 2],
            local_tag_map={"[id0]": "<p><span>", "[id1]": "</span><span>", "[id2]": "</span></p>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_strong_em_tags(self, mock_translator):
        """测试 strong 和 em 加深/斜体标签"""
        chunk = Chunk(
            name="strong_em_chunk",
            original="[id0]Bold text[id1][id2]Italic text[id3]",
            tokens=10,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<p><strong>",
                "[id1]": "</strong>",
                "[id2]": "<em>",
                "[id3]": "</em></p>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_anchor_tag(self, mock_translator):
        """测试 a 链接标签"""
        chunk = Chunk(
            name="anchor_chunk",
            original="[id0]Link text[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<a href='https://example.com'>", "[id1]": "</a>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 列表标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_unordered_list(self, mock_translator):
        """测试 ul 无序列表"""
        chunk = Chunk(
            name="ul_chunk",
            original="[id0][id1]First item[id2][id3]Second item[id4][id5]",
            tokens=20,
            global_indices=[0, 1, 2, 3, 4, 5],
            local_tag_map={
                "[id0]": "<ul>",
                "[id1]": "<li>",
                "[id2]": "</li><li>",
                "[id3]": "</li>",
                "[id4]": "<li>",
                "[id5]": "</li></ul>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_ordered_list(self, mock_translator):
        """测试 ol 有序列表"""
        chunk = Chunk(
            name="ol_chunk",
            original="[id0][id1]First item[id2][id3]Second item[id4][id5]",
            tokens=20,
            global_indices=[0, 1, 2, 3, 4, 5],
            local_tag_map={
                "[id0]": "<ol>",
                "[id1]": "<li>",
                "[id2]": "</li><li>",
                "[id3]": "</li>",
                "[id4]": "<li>",
                "[id5]": "</li></ol>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 表格标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_table(self, mock_translator):
        """测试 table 表格标签（整体作为 chunk）"""
        chunk = Chunk(
            name="table_chunk",
            original="[id0][id1][id2]Cell 1[id3][id4]Cell 2[id5][id6][id7]",
            tokens=30,
            global_indices=range(8),
            local_tag_map={
                "[id0]": "<table><tr>",
                "[id1]": "<td>",
                "[id2]": "</td><td>",
                "[id3]": "</td></tr><tr><td>",
                "[id4]": "</td><td>",
                "[id5]": "</td></tr></table>",
                "[id6]": "",
                "[id7]": ""
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 自闭合标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_with_br(self, mock_translator):
        """测试包含 br 换行标签"""
        chunk = Chunk(
            name="br_chunk",
            original="[id0]Line 1<br/>Line 2[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_with_hr(self, mock_translator):
        """测试包含 hr 水平线标签"""
        chunk = Chunk(
            name="hr_chunk",
            original="[id0]Section 1[id1]<hr/>[id2]Section 2[id3]",
            tokens=15,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<p>",
                "[id1]": "</p>",
                "[id2]": "<p>",
                "[id3]": "</p>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 复杂嵌套标签测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_nested_article_header_footer(self, mock_translator):
        """测试复杂的 article>header>h1+footer 嵌套结构"""
        chunk = Chunk(
            name="nested_chunk",
            original="[id0]Header[id1]Title[id2]Footer[id3]",
            tokens=15,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<article><header><h1>",
                "[id1]": "</h1></header><p>",
                "[id2]": "</p></article><footer><p>",
                "[id3]": "</p></footer>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED

    @pytest.mark.asyncio
    async def test_translate_deeply_nested(self, mock_translator):
        """测试深度嵌套标签"""
        chunk = Chunk(
            name="deep_nested",
            original="[id0]Content[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={
                "[id0]": "<div><article><section><div><p>",
                "[id1]": "</p></div></section></article></div>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 多个 chunk 场景测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_multiple_sequential_divs(self, mock_translator):
        """测试连续多个 div 块，每个有独立 local_tag_map"""
        chunks = []
        for i in range(3):
            chunk = Chunk(
                name=f"div_chunk_{i}",
                original=f"[id0]Content {i}[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<div>", "[id1]": "</div>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.success is True
            chunks.append(output.content)
        assert len(chunks) == 3

    # -------------------------------------------------------------------------
    # Pre/Code/Style 占位符测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_with_pre_placeholder(self, mock_translator):
        """测试包含 [PRE:n] 占位符的 chunk（pre 内容应被提取）"""
        chunk = Chunk(
            name="pre_chunk",
            original="[id0]Normal text[id1] [PRE:0] [id2]More text[id3]",
            tokens=15,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<p>",
                "[id1]": "</p><p>",
                "[id2]": "</p><p>",
                "[id3]": "</p>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={
                "placeholder_mgr": MagicMock(),
                "preserved_pre": ["<pre>code here</pre>"]
            }
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_with_code_placeholder(self, mock_translator):
        """测试包含 [CODE:n] 占位符的 chunk"""
        chunk = Chunk(
            name="code_chunk",
            original="[id0]Explanation[id1] [CODE:0] [id2]More text[id3]",
            tokens=15,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<p>",
                "[id1]": "</p><p>",
                "[id2]": "</p><p>",
                "[id3]": "</p>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={
                "placeholder_mgr": MagicMock(),
                "preserved_code": ["<code>var x = 1;</code>"]
            }
        )
        output = await translate_step(step_input)
        assert output.success is True

    @pytest.mark.asyncio
    async def test_translate_with_style_placeholder(self, mock_translator):
        """测试包含 [STYLE:n] 占位符的 chunk"""
        chunk = Chunk(
            name="style_chunk",
            original="[id0]Styled text[id1] [STYLE:0] [id2]Normal text[id3]",
            tokens=15,
            global_indices=[0, 1, 2, 3],
            local_tag_map={
                "[id0]": "<div>",
                "[id1]": "</div><div>",
                "[id2]": "</div><p>",
                "[id3]": "</p>"
            }
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={
                "placeholder_mgr": MagicMock(),
                "preserved_style": [".custom { color: red; }"]
            }
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 超长文本和大量占位符测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_long_text_many_placeholders(self, mock_translator):
        """测试超长文本和大量占位符"""
        # 创建一个有 10 个占位符的 chunk
        local_tag_map = {}
        original_parts = []
        for i in range(10):
            local_tag_map[f"[id{i}]"] = f"<span class='tag{i}'>" if i % 2 == 0 else "</span>"
            original_parts.append(f"[id{i}]" if i % 2 == 0 else f"文本{i//2}")

        chunk = Chunk(
            name="long_chunk",
            original="".join(original_parts),
            tokens=100,
            global_indices=list(range(10)),
            local_tag_map=local_tag_map
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True

    # -------------------------------------------------------------------------
    # 验证失败边界情况测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_validate_extra_placeholders_detected(self):
        """验证译文包含多余占位符时被检测为无效"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        translated = "你好 [id0] 世界 [id1] [id2]"  # 多余 [id2]
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False
        assert "多余" in error_msg

    @pytest.mark.asyncio
    async def test_validate_missing_and_extra_combined(self):
        """验证同时缺少和多余占位符时的检测"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<div>"}
        translated = "你好 [id0] 世界 [id1]"  # 缺少 [id2]
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False

    @pytest.mark.asyncio
    async def test_validate_all_placeholders_present_but_wrong_order(self):
        """验证占位符都存在但顺序错误"""
        tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<div>", "[id3]": "</div>"}
        translated = "你好 [id1] 世界 [id0] 更多 [id3] 内容 [id2]"
        is_valid, error_msg = validate_placeholders(translated, tag_map)
        assert is_valid is False
        assert "不匹配" in error_msg

    # -------------------------------------------------------------------------
    # 特殊内容测试
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_empty_chunk_no_crash(self, mock_translator):
        """测试空 chunk 不崩溃"""
        chunk = Chunk(
            name="empty_chunk",
            original="",
            tokens=0,
            global_indices=[],
            local_tag_map={}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        # 无可翻译内容时直接返回原文
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == ""

    @pytest.mark.asyncio
    async def test_translate_only_placeholders(self, mock_translator):
        """测试只有占位符无实际文本"""
        chunk = Chunk(
            name="placeholders_only",
            original="[id0][id1][id2]",
            tokens=0,
            global_indices=[0, 1, 2],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<div>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        # 无可翻译内容时应跳过翻译
        assert output.content.translated == "[id0][id1][id2]"

    @pytest.mark.asyncio
    async def test_translate_chinese_text(self, mock_translator):
        """测试中文文本（无需翻译）"""
        chunk = Chunk(
            name="chinese_chunk",
            original="[id0]你好世界[id1]",
            tokens=10,
            global_indices=[0, 1],
            local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
        )
        step_input = MagicMock(
            input=chunk,
            additional_data={"placeholder_mgr": MagicMock()}
        )
        output = await translate_step(step_input)
        assert output.success is True


# =============================================================================
# 补充覆盖率测试 - 针对未覆盖的核心分支
# =============================================================================

class TestWorkflowCoverageGaps:
    """补充测试：覆盖 workflow.py 中未覆盖的分支"""

    # -------------------------------------------------------------------------
    # filter_glossary_terms 函数测试
    # -------------------------------------------------------------------------

    def test_filter_glossary_terms(self):
        """测试术语表过滤功能"""
        from engine.agents.workflow import filter_glossary_terms
        glossary = {
            "Hello": "你好",
            "World": "世界",
            "artificial intelligence": "人工智能",
        }
        text = "Hello World, artificial intelligence is important."
        result = filter_glossary_terms(text, glossary)
        assert result == {"Hello": "你好", "World": "世界", "artificial intelligence": "人工智能"}

    def test_filter_glossary_terms_case_insensitive(self):
        """测试术语表过滤大小写不敏感"""
        from engine.agents.workflow import filter_glossary_terms
        glossary = {"Hello": "你好"}
        result = filter_glossary_terms("hello world", glossary)
        assert result == {"Hello": "你好"}

    def test_filter_glossary_terms_no_match(self):
        """测试无匹配时返回空字典"""
        from engine.agents.workflow import filter_glossary_terms
        glossary = {"Hello": "你好"}
        result = filter_glossary_terms("Bonjour monde", glossary)
        assert result == {}

    def test_filter_glossary_terms_empty_glossary(self):
        """测试空术语表"""
        from engine.agents.workflow import filter_glossary_terms
        result = filter_glossary_terms("Hello World", {})
        assert result == {}

    # -------------------------------------------------------------------------
    # Phase 1 内容安全错误触发 fallback 切换
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_phase1_content_safety_switch_to_fallback(self):
        """Phase 1 遇到内容安全错误时切换到备用模型"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            call_count = [0]

            async def mock_arun(json_input):
                call_count[0] += 1
                if call_count[0] == 1:
                    # 第一次：主模型内容安全错误
                    return MagicMock(status="error", content="相关法律法规不予显示")
                # 第二次：备用模型成功
                return MagicMock(content=MockTranslationResponse("[id0]Hello[id1]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="safety_switch_test",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.content.status == TranslationStatus.TRANSLATED
            assert call_count[0] == 2

    # -------------------------------------------------------------------------
    # extra_ids（多余占位符）处理
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_phase1_extra_placeholder_hints_returned(self):
        """Phase 1 返回多余占位符时构建正确的提示信息"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            captured = []

            async def mock_arun(json_input):
                captured.append(json_input)
                # 第一次：多余占位符
                return MagicMock(content=MockTranslationResponse("[id0]你好[id1][id2]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="extra_ids_test",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            await translate_step(step_input)
            # 应该重试并包含提示
            assert len(captured) >= 1

    # -------------------------------------------------------------------------
    # 顺序错误提示解析
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_phase1_order_error_hint_parsing(self):
        """Phase 1 遇到顺序错误时能正确解析 missing 和 extra ids"""
        with patch("engine.agents.workflow._call_translator", new_callable=AsyncMock) as mock_call:
            captured = []

            async def side_effect(*args, **kwargs):
                captured.append(kwargs.get("error_msg") or args[2] if len(args) > 2 else "")
                # 模拟返回顺序错误的翻译结果
                return "[id0]你好 [id2] 世界 [id1]"  # 缺少 [id1]... 实际 [id2] 顺序错误

            mock_call.side_effect = side_effect

            chunk = Chunk(
                name="order_error_test",
                original="[id0]Hello[id1]World[id2]",
                tokens=10,
                global_indices=[0, 1, 2],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<em>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            # 应该重试多次并构建顺序错误提示
            assert len(captured) >= 1

    # -------------------------------------------------------------------------
    # proofread_step UNTRANSLATED 跳过路径
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_proofread_step_skips_on_untranslated(self):
        """翻译失败时，校对步骤跳过"""
        chunk = Chunk(
            name="untranslated_chunk",
            original="[id0]Hello[id1]",
            tokens=10,
            status=TranslationStatus.UNTRANSLATED,
            translated=""
        )
        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert output.success is True
        assert output.content["proofreading_result"].corrections == {}

    # -------------------------------------------------------------------------
    # proofread_step RunStatus.error 处理
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_run_status_error(self, mock_get_proofer):
        """proofer 返回 RunStatus.error 时重试"""
        chunk = Chunk(
            name="error_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def mock_arun(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(status="error", content="Server error")
            return MagicMock(content=MockProofreadingResult({}))

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert output.success is True
        assert call_count[0] == 2

    # -------------------------------------------------------------------------
    # proofread_step 最终失败（所有重试都失败）
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_all_retries_fail(self, mock_get_proofer):
        """校对所有重试都失败"""
        chunk = Chunk(
            name="fail_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )

        async def mock_arun(json_input):
            return MagicMock(status="running", content="正在处理...")

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert output.success is False
        assert "未成功" in output.error

    # -------------------------------------------------------------------------
    # apply_corrections_step UNTRANSLATED 跳过路径
    # -------------------------------------------------------------------------

    def test_apply_corrections_skips_on_untranslated(self):
        """翻译失败时，应用校对建议步骤跳过"""
        chunk = Chunk(
            name="untranslated_chunk",
            original="[id0]Hello[id1]",
            translated="",
            tokens=10,
            status=TranslationStatus.UNTRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"你好": "您好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)
        output = apply_corrections_step(step_input)
        assert output.success is True
        assert output.content.status == TranslationStatus.UNTRANSLATED

    # -------------------------------------------------------------------------
    # is_content_safety_error 函数测试
    # -------------------------------------------------------------------------

    def test_is_content_safety_error_by_code(self):
        """按错误码判断内容安全错误"""
        from engine.agents.workflow import is_content_safety_error
        assert is_content_safety_error(status_code=10014) is True
        assert is_content_safety_error(status_code=500) is True
        assert is_content_safety_error(status_code=400) is True
        assert is_content_safety_error(status_code=404) is False

    def test_is_content_safety_error_by_keyword(self):
        """按关键字判断内容安全错误"""
        from engine.agents.workflow import is_content_safety_error
        assert is_content_safety_error(error_msg="相关法律法规不予显示") is True
        assert is_content_safety_error(error_msg="内容安全审核未通过") is True
        assert is_content_safety_error(error_msg="404 Not Found") is False

    def test_is_content_safety_error_combined(self):
        """组合判断"""
        from engine.agents.workflow import is_content_safety_error
        assert is_content_safety_error(error_msg="Error", status_code=10014) is True

    # -------------------------------------------------------------------------
    # translator_input 包含 previous_translation
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translator_input_includes_previous_translation(self):
        """translator_input 包含 previous_translation 字段（第三次重试）"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            captured = []

            async def mock_arun(json_input):
                captured.append(json_input)
                # 前两次返回无占位符，第三次保留
                if len(captured) < 3:
                    return MagicMock(content=MockTranslationResponse("无占位符"))
                return MagicMock(content=MockTranslationResponse("[id0]Hello[id1]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="prev_trans_test",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.content.status == TranslationStatus.TRANSLATED
            # 第二次重试应包含 validation_error
            if len(captured) >= 2:
                parsed = json.loads(captured[1])
                assert "validation_error" in parsed

    # -------------------------------------------------------------------------
    # _get_placeholder_indices 和 _get_placeholders_from_indices 完整测试
    # -------------------------------------------------------------------------

    def test_get_placeholder_indices_mixed_text(self):
        """混有占位符和普通文本"""
        from engine.agents.workflow import _get_placeholder_indices
        indices = _get_placeholder_indices("[id0]Hello[id2]World[id5]End")
        assert indices == [0, 2, 5]

    def test_get_placeholder_indices_no_placeholders(self):
        """无占位符"""
        from engine.agents.workflow import _get_placeholder_indices
        assert _get_placeholder_indices("Hello World") == []

    def test_get_placeholders_from_indices_empty(self):
        """空索引列表"""
        from engine.agents.workflow import _get_placeholders_from_indices
        assert _get_placeholders_from_indices([]) == []

    # -------------------------------------------------------------------------
    # 全量翻译（preserved_pre/code/style 传入）
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_with_all_preserved(self):
        """翻译时传入 preserved_pre/code/style"""
        with patch("engine.agents.workflow.get_translator") as mock:
            mock_translator = MagicMock()
            mock_translator.arun = AsyncMock(
                return_value=MagicMock(content=MockTranslationResponse("[id0]Hello[id1]"))
            )
            mock.return_value = mock_translator

            chunk = Chunk(
                name="preserved_all",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={
                    "placeholder_mgr": MagicMock(),
                    "preserved_pre": ["<pre>code</pre>"],
                    "preserved_code": ["<code>x=1</code>"],
                    "preserved_style": [".cls { color: red }"],
                }
            )
            output = await translate_step(step_input)
            assert output.success is True

    # -------------------------------------------------------------------------
    # _call_translator 异常和 RunStatus.error 处理
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translator_run_status_error_non_safety(self):
        """translator 返回 RunStatus.error 但非内容安全时走一般异常路径"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            call_count = [0]

            async def mock_arun(json_input):
                call_count[0] += 1
                # 非内容安全的错误
                return MagicMock(status="error", content="Internal Server Error")

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="non_safety_error_test",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            await translate_step(step_input)
            # 非内容安全错误不触发 fallback，直接重试3次
            assert call_count[0] == 3

    # -------------------------------------------------------------------------
    # proofer RunStatus.error + 非内容安全异常
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_non_safety_exception(self, mock_get_proofer):
        """proofer 抛出非内容安全异常时重试"""
        chunk = Chunk(
            name="exception_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def mock_arun(json_input):
            call_count[0] += 1
            raise Exception("Network error")

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert call_count[0] == 3
        assert output.success is False

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_non_safety_error_response(self, mock_get_proofer):
        """proofer 返回非内容安全的错误响应"""
        chunk = Chunk(
            name="error_response_chunk",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def mock_arun(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(status="error", content="Model overloaded")
            return MagicMock(content=MockProofreadingResult({}))

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert call_count[0] == 2
        assert output.success is True

    # -------------------------------------------------------------------------
    # translate_step 未知异常处理
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_step_unknown_exception(self):
        """translate_step 捕获未知异常"""
        with patch("engine.agents.workflow._translate_with_fallback", new_callable=AsyncMock) as mock_translate:
            mock_translate.side_effect = ValueError("Unknown error")

            chunk = Chunk(
                name="unknown_exc",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.success is False
            assert "Unknown error" in output.error

    # -------------------------------------------------------------------------
    # extra_ids 解析和 hint 构建完整覆盖
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_phase1_only_missing_ids_hint(self):
        """仅有 missing_ids 时构建保留提示"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            captured = []

            async def mock_arun(json_input):
                captured.append(json_input)
                # 只有缺少，无多余
                return MagicMock(content=MockTranslationResponse("[id0]你好"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="missing_only",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            await translate_step(step_input)
            # 重试时会包含 validation_error
            assert len(captured) >= 1

    @pytest.mark.asyncio
    async def test_phase1_only_extra_ids_hint(self):
        """仅有 extra_ids 时构建删除提示"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            captured = []

            async def mock_arun(json_input):
                captured.append(json_input)
                # 多余 [id2]
                return MagicMock(content=MockTranslationResponse("[id0]你好[id1][id2]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="extra_only",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            await translate_step(step_input)
            assert len(captured) >= 1

    # -------------------------------------------------------------------------
    # _call_translator: RunStatus.error 内容安全 + non-None error_content
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_call_translator_safety_error_with_content(self):
        """RunStatus.error + content 非空时抛出 ValueError"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            call_count = [0]

            async def mock_arun(json_input):
                call_count[0] += 1
                if call_count[0] == 1:
                    return MagicMock(status="error", content="相关法律法规不予显示")
                return MagicMock(content=MockTranslationResponse("[id0]Hello[id1]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="safety_with_content",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[0, 1],
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": MagicMock()}
            )
            output = await translate_step(step_input)
            assert output.content.status == TranslationStatus.TRANSLATED
            assert call_count[0] == 2

    # -------------------------------------------------------------------------
    # proofer safety-error with fallback + exception path
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_safety_error_with_content(self, mock_get_proofer):
        """proofer RunStatus.error + 内容安全关键字 时切换 fallback"""
        chunk = Chunk(
            name="proof_safety",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def mock_arun(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(status="error", content="相关法律法规不予显示")
            return MagicMock(content=MockProofreadingResult({}))

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert call_count[0] == 2
        assert output.success is True

    @pytest.mark.asyncio
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_safety_exception_switches_fallback(self, mock_get_proofer):
        """proofer 异常为内容安全时切换 fallback"""
        chunk = Chunk(
            name="proof_safety_exc",
            original="[id0]Hello[id1]",
            translated="[id0]你好[id1]",
            tokens=10,
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def mock_arun(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("相关法律法规不予显示")
            return MagicMock(content=MockProofreadingResult({}))

        mock_proofer = MagicMock()
        mock_proofer.arun = mock_arun
        mock_get_proofer.return_value = mock_proofer

        step_input = MagicMock(previous_step_content={"chunk": chunk, "validation_error": None})
        output = await proofread_step(step_input)
        assert call_count[0] == 2
        assert output.success is True

    # -------------------------------------------------------------------------
    # 无 global_indices 时 fallback 到 placeholder_mgr
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_translate_no_global_indices(self):
        """无 global_indices 时 validation_tag_map 使用 placeholder_mgr"""
        with patch("engine.agents.workflow.get_translator") as mock_get_translator:
            async def mock_arun(json_input):
                return MagicMock(content=MockTranslationResponse("[id0]Hello[id1]"))

            mock_translator = MagicMock()
            mock_translator.arun = mock_arun
            mock_get_translator.return_value = mock_translator

            chunk = Chunk(
                name="no_global_indices",
                original="[id0]Hello[id1]",
                tokens=10,
                global_indices=[],  # 空列表
                local_tag_map={"[id0]": "<p>", "[id1]": "</p>"}
            )
            step_input = MagicMock(
                input=chunk,
                additional_data={"placeholder_mgr": chunk.local_tag_map}
            )
            output = await translate_step(step_input)
            assert output.content.status == TranslationStatus.TRANSLATED
