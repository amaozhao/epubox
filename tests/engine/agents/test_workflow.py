import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from agno.run import RunStatus
from agno.workflow import Workflow

from engine.agents.schemas import ProofreadingResult, TranslationResponse
from engine.agents.workflow import (
    apply_corrections_step,
    filter_glossary_terms,
    get_translator_workflow,
    is_content_safety_error,
    proofread_step,
    translate_step,
)
from engine.schemas import Chunk, TranslationStatus


class MockTranslationResponse(TranslationResponse):
    def __init__(self, translation: str):
        super().__init__(translation=translation)


class MockProofreadingResult(ProofreadingResult):
    def __init__(self, corrections: dict):
        super().__init__(corrections=corrections)


@pytest_asyncio.fixture(autouse=True)
async def reset_fallback_runtime_between_tests(monkeypatch):
    from engine.agents import fallback_runtime

    await fallback_runtime.reset_fallback_runtime_state()
    monkeypatch.setattr(fallback_runtime, "FALLBACK_MIN_INTERVAL_SECONDS", 0.0)


def make_chunk(
    name="test_chunk",
    original="<p>Hello World</p>",
    translated=None,
    tokens=10,
    status=TranslationStatus.PENDING,
    xpaths=None,
    chunk_mode="html_fragment",
    nav_targets=None,
):
    return Chunk(
        name=name,
        original=original,
        translated=translated,
        tokens=tokens,
        status=status,
        xpaths=xpaths if xpaths is not None else ["/html/body/p"],
        chunk_mode=chunk_mode,
        nav_targets=nav_targets or [],
    )


@pytest.mark.asyncio
class TestTranslateStep:
    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_success(self, mock_get_translator):
        """translate_step: HTML input -> Chinese HTML output, status TRANSLATED"""
        chunk = make_chunk(original="<p>Hello World</p>")
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界</p>"),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.success is True
        assert isinstance(output.content, Chunk)
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好世界</p>"

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_already_translated(self, mock_get_translator):
        """translate_step: already translated chunk is returned directly without calling translator"""
        chunk = make_chunk(
            original="<p>Hello World</p>",
            translated="<p>你好世界</p>",
            status=TranslationStatus.TRANSLATED,
        )
        step_input = MagicMock(input=chunk, additional_data={})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.translated == "<p>你好世界</p>"
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_empty_content_skipped(self, mock_get_translator):
        """translate_step: empty original is skipped, translated = original, status TRANSLATED"""
        chunk = make_chunk(original="   ")
        step_input = MagicMock(input=chunk, additional_data={})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "   "
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_all_retries_fail_untranslated(self, mock_get_translator):
        """translate_step: all retries fail -> status UNTRANSLATED, translated = ''"""
        # Return a wrong number of top-level elements so validate_translated_html fails
        chunk = make_chunk(original="<p>Hello</p>")
        call_count = [0]

        async def bad_response(json_input):
            call_count[0] += 1
            # Return extra element so element count mismatch triggers validation failure
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好</p><p>额外</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = bad_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert output.content.translated == ""
        assert call_count[0] == 3  # MAX_TRANSLATION_RETRIES

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_retry_includes_placeholder_position_error(self, mock_get_translator):
        """translate_step: retry payload includes precise placeholder order mismatch details."""
        chunk = make_chunk(original="<p>Alpha [CODE:1]</p><p>Beta [CODE:2] [CODE:3]</p>")
        seen_inputs = []
        responses = iter(
            [
                MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("<p>甲 [CODE:2]</p><p>乙 [CODE:1] [CODE:3]</p>"),
                ),
                MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("<p>甲 [CODE:1]</p><p>乙 [CODE:2] [CODE:3]</p>"),
                ),
            ]
        )

        async def translator_response(json_input):
            seen_inputs.append(json.loads(json_input))
            return next(responses)

        mock_translator = MagicMock()
        mock_translator.arun = translator_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED
        assert len(seen_inputs) == 2
        assert "validation_error" not in seen_inputs[0]
        assert "CODE 占位符归属/数量不一致" in seen_inputs[1]["validation_error"]
        assert "元素1 位置1" in seen_inputs[1]["validation_error"]
        assert "原始 [CODE:1]" in seen_inputs[1]["validation_error"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_accepts_code_reorder_within_same_element_without_retry(self, mock_get_translator):
        """translate_step: CODE reorder within one element should pass validation without retry."""
        chunk = make_chunk(original="<p>Run [CODE:31], [CODE:32], and [CODE:33]</p>")
        call_count = [0]

        async def swapped_response(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>在 [CODE:33] 所在目录中运行 [CODE:31] 和 [CODE:32]</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = swapped_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>在 [CODE:33] 所在目录中运行 [CODE:31] 和 [CODE:32]</p>"
        assert call_count[0] == 1

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_original_echo_becomes_untranslated(self, mock_get_translator):
        """translate_step: exact original echo should be treated as untranslated and retried"""
        chunk = make_chunk(original="<p>Hello World</p>")
        call_count = [0]

        async def echoed_response(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>Hello World</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = echoed_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert output.content.translated == ""
        assert call_count[0] == 3

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_unicode_original_echo_becomes_untranslated(self, mock_get_translator):
        """translate_step: unicode original echo should also be treated as untranslated and retried"""
        chunk = make_chunk(original="<p>你好世界</p>")
        call_count = [0]

        async def echoed_response(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = echoed_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert output.content.translated == ""
        assert call_count[0] == 3

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_symbol_only_noop_becomes_accepted_as_is(self, mock_get_translator):
        """translate_step: legitimate unchanged symbol-only content is accepted as-is"""
        chunk = make_chunk(original="<p>2024 [PRE:0] !!!</p>")

        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>2024 [PRE:0] !!!</p>"),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<p>2024 [PRE:0] !!!</p>"
        assert mock_translator.arun.await_count == 1

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_technical_ascii_noop_becomes_accepted_as_is(self, mock_get_translator):
        """translate_step: legitimate unchanged technical ASCII content is accepted as-is"""
        chunk = make_chunk(original="<p>python -m pytest tests/engine/agents/test_workflow.py -k accepted_as_is</p>")

        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse(
                    "<p>python -m pytest tests/engine/agents/test_workflow.py -k accepted_as_is</p>"
                ),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<p>python -m pytest tests/engine/agents/test_workflow.py -k accepted_as_is</p>"
        assert mock_translator.arun.await_count == 1

    @patch("engine.agents.workflow.run_fallback_agent")
    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_content_safety_fallback(self, mock_get_translator, mock_run_fallback_agent):
        """translate_step: content safety error on first call triggers fallback model"""
        chunk = make_chunk(original="<p>Hello</p>")
        call_count = [0]

        async def safety_then_success(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: content safety error
                mock_response = MagicMock()
                mock_response.status = RunStatus.error
                mock_response.content = "相关法律法规不予显示"
                return mock_response
            # Second call (fallback): success
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = safety_then_success
        mock_get_translator.return_value = mock_translator
        async def fallback_success(kind, agent, payload):
            return await safety_then_success(payload)

        mock_run_fallback_agent.side_effect = fallback_success

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert call_count[0] == 2

    @patch("engine.agents.workflow.run_fallback_agent")
    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_validation_failure_uses_fallback_on_last_attempt(
        self, mock_get_translator, mock_run_fallback_agent
    ):
        """translate_step: final retry switches to fallback agent for non-safety validation failures."""
        chunk = make_chunk(original="<p>Hello</p>")
        call_count = [0]

        async def invalid_response(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好</p><p>额外</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = invalid_response
        mock_get_translator.return_value = mock_translator
        mock_run_fallback_agent.return_value = MagicMock(
            status=RunStatus.completed,
            content=MockTranslationResponse("<p>你好</p>"),
        )

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert call_count[0] == 2
        mock_run_fallback_agent.assert_awaited_once()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_nav_text_success(self, mock_get_translator):
        """translate_step: nav_text chunks validate NAV markers instead of HTML shape."""
        chunk = make_chunk(original="[NAVTXT:0] Chapter 1", xpaths=[], chunk_mode="nav_text")
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("[NAVTXT:0] 第1章"),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "[NAVTXT:0] 第1章"

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_nav_text_invalid_marker_fails(self, mock_get_translator):
        """translate_step: nav_text marker mismatch should fail retries."""
        chunk = make_chunk(original="[NAVTXT:0] Chapter 1", xpaths=[], chunk_mode="nav_text")
        call_count = [0]

        async def wrong_marker(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("[NAVTXT:1] 第1章"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = wrong_marker
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert output.content.translated == ""
        assert call_count[0] == 3


@pytest.mark.asyncio
class TestProofreadStep:
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_success(self, mock_get_proofer):
        """proofread_step: translated chunk returns proofreading result"""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )
        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockProofreadingResult({"你好": "您好"}),
            )
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
    async def test_proofread_step_no_translated_text(self, mock_get_proofer):
        """proofread_step: no translated text -> error returned"""
        chunk = make_chunk(original="<p>Hello</p>", translated=None)
        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)

        assert output.success is False
        assert "没有从上一步收到有效的翻译文本" in output.error

    @patch("engine.agents.workflow.run_fallback_agent")
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_content_safety_fallback(self, mock_get_proofer, mock_run_fallback_agent):
        """proofread_step: content safety error on main model triggers fallback"""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )
        call_count = [0]

        async def safety_then_success(json_input):
            call_count[0] += 1
            if call_count[0] == 1:
                mock_response = MagicMock()
                mock_response.status = RunStatus.error
                mock_response.content = "相关法律法规不予显示"
                return mock_response
            return MagicMock(
                status=RunStatus.completed,
                content=MockProofreadingResult({"你好": "您好"}),
            )

        mock_proofer = MagicMock()
        mock_proofer.arun = safety_then_success
        mock_get_proofer.return_value = mock_proofer
        async def fallback_success(kind, agent, payload):
            return await safety_then_success(payload)

        mock_run_fallback_agent.side_effect = fallback_success

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)

        assert output.success is True
        assert call_count[0] == 2

    @patch("engine.agents.workflow.run_fallback_agent")
    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_general_failure_uses_fallback(self, mock_get_proofer, mock_run_fallback_agent):
        """proofread_step: general main-model failures eventually use the shared fallback channel."""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(side_effect=RuntimeError("timeout"))
        mock_get_proofer.return_value = mock_proofer
        mock_run_fallback_agent.return_value = MagicMock(
            status=RunStatus.completed,
            content=MockProofreadingResult({"你好": "您好"}),
        )

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)

        assert output.success is True
        assert output.content["proofreading_result"].corrections == {"你好": "您好"}
        mock_run_fallback_agent.assert_awaited_once()

    @patch("engine.agents.workflow.get_proofer")
    async def test_proofread_step_nav_text_skipped(self, mock_get_proofer):
        """proofread_step: nav_text chunks skip proofer to preserve marker contract."""
        chunk = make_chunk(
            original="[NAVTXT:0] Chapter 1",
            translated="[NAVTXT:0] 第1章",
            status=TranslationStatus.TRANSLATED,
            xpaths=[],
            chunk_mode="nav_text",
        )

        step_input = MagicMock(previous_step_content=chunk)
        output = await proofread_step(step_input)

        assert output.success is True
        assert output.content["proofreading_result"].corrections == {}
        mock_get_proofer.assert_not_called()


class TestApplyCorrectionsStep:
    def test_apply_corrections_step_success(self):
        """apply_corrections_step: corrections applied, post-processing runs"""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"你好": "您好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert isinstance(output.content, Chunk)
        # Post-processing replaces 您 -> 你, so 您好 -> 你好
        assert output.content.translated == "<p>你好</p>"
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_invalid_placeholder_correction_filtered(self):
        """apply_corrections_step: invalid placeholder-polluting correction is filtered out before replacement"""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"你好": "你好 [PRE:1]"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == "<p>你好</p>"
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_keeps_valid_corrections_while_filtering_bad_ones(self):
        """apply_corrections_step: valid corrections still apply when a bad placeholder correction is rejected."""
        chunk = make_chunk(
            original="<p>Hello world</p>",
            translated="<p>你好世界</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult(
            {
                "你好": "您好",
                "世界": "世界 [PRE:1]",
            }
        )
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == "<p>你好世界</p>"
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_rejects_any_correction_touching_placeholders(self):
        """apply_corrections_step: placeholder-bearing corrections are skipped even if placeholder count/order matches."""
        chunk = make_chunk(
            original="<p>你好[CODE:1]世界</p>",
            translated="<p>你好[CODE:1]世界</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult(
            {
                "你好[CODE:1]世界": "您好[CODE:1]世界",
            }
        )
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == "<p>你好[CODE:1]世界</p>"
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_only_edits_text_nodes_not_attributes(self):
        """apply_corrections_step: proofreading corrections must not rewrite HTML attribute values such as img alt."""
        chunk = make_chunk(
            original='<p>你好</p><img alt="Publisher’s logo." src="../images/pub.jpg"/>',
            translated='<p>你好</p><img alt="Publisher’s logo." src="../images/pub.jpg"/>',
            status=TranslationStatus.TRANSLATED,
            xpaths=["/html/body/p", "/html/body/img"],
        )
        proofreading_result = MockProofreadingResult({"Publisher’s logo.": "出版商 Logo。", "你好": "哈喽"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == '<p>哈喽</p><img alt="Publisher’s logo." src="../images/pub.jpg"/>'
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_preserves_inline_markup_boundaries(self):
        """apply_corrections_step: corrections apply inside text nodes without collapsing inline tags."""
        chunk = make_chunk(
            original='<p>找到<i>一个</i>客户</p>',
            translated='<p>找到<i>一个</i>客户</p>',
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"客户": "用户"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == '<p>找到<i>一个</i>用户</p>'
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_nav_text_skips_corrections(self):
        """apply_corrections_step: nav_text chunk keeps translated text and only flips status."""
        chunk = make_chunk(
            original="[NAVTXT:0] Chapter 1",
            translated="[NAVTXT:0] 第1章",
            status=TranslationStatus.TRANSLATED,
            xpaths=[],
            chunk_mode="nav_text",
        )
        proofreading_result = MockProofreadingResult({"第1章": "第一章"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == "[NAVTXT:0] 第1章"
        assert output.content.status == TranslationStatus.COMPLETED

    def test_apply_corrections_step_no_translated_text(self):
        """apply_corrections_step: missing translated text -> error"""
        chunk = make_chunk(original="<p>Hello</p>", translated=None)
        proofreading_result = MockProofreadingResult({})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is False
        assert "缺少翻译文本" in output.error

    def test_apply_corrections_step_untranslated_skipped(self):
        """apply_corrections_step: UNTRANSLATED chunk skips corrections"""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="",
            status=TranslationStatus.TRANSLATION_FAILED,
        )
        proofreading_result = MockProofreadingResult({"你好": "您好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATION_FAILED


@pytest.mark.asyncio
class TestGetTranslatorWorkflow:
    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_success(self, mock_get_proofer, mock_get_translator):
        """get_translator_workflow: full pipeline translates, proofreads, applies corrections"""
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界</p>"),
            )
        )
        mock_get_translator.return_value = mock_translator

        mock_proofer = MagicMock()
        mock_proofer.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockProofreadingResult({}),
            )
        )
        mock_get_proofer.return_value = mock_proofer

        workflow: Workflow = get_translator_workflow()
        chunk = make_chunk(original="<p>Hello World</p>")

        response = await workflow.arun(
            input=chunk,
            additional_data={"glossary": {}},
        )

        assert response.status == "COMPLETED"
        assert isinstance(response.content, Chunk)
        assert response.content.status == TranslationStatus.COMPLETED
        assert "你好世界" in response.content.translated

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_keeps_accepted_as_is_without_proofreading(self, mock_get_proofer, mock_get_translator):
        """get_translator_workflow: legitimate no-op content stays ACCEPTED_AS_IS and skips later steps"""
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>2024 [PRE:0] !!!</p>"),
            )
        )
        mock_get_translator.return_value = mock_translator

        workflow: Workflow = get_translator_workflow()
        chunk = make_chunk(original="<p>2024 [PRE:0] !!!</p>")

        response = await workflow.arun(
            input=chunk,
            additional_data={"glossary": {}},
        )

        assert response.status == "COMPLETED"
        assert isinstance(response.content, Chunk)
        assert response.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert response.content.translated == "<p>2024 [PRE:0] !!!</p>"
        mock_get_proofer.assert_not_called()


class TestHelpers:
    def test_is_content_safety_error_by_keyword(self):
        assert is_content_safety_error("相关法律法规不予显示") is True
        assert is_content_safety_error("安全审核失败") is True
        assert is_content_safety_error("content policy violation") is True

    def test_is_content_safety_error_by_status_code(self):
        assert is_content_safety_error(status_code=10014) is True
        assert is_content_safety_error(status_code=500) is True
        assert is_content_safety_error(status_code=200) is False

    def test_is_content_safety_error_normal(self):
        assert is_content_safety_error("network timeout") is False
        assert is_content_safety_error("") is False

    def test_filter_glossary_terms(self):
        text = "The LLM model is a large language model"
        glossary = {"LLM": "大语言模型", "API": "应用程序接口", "large language model": "大语言模型"}
        result = filter_glossary_terms(text, glossary)
        # Longer terms matched first
        assert "large language model" in result
        assert "LLM" in result
        assert "API" not in result

    def test_filter_glossary_terms_empty(self):
        result = filter_glossary_terms("hello world", {})
        assert result == {}
