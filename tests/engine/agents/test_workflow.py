import json
from typing import Literal
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
from engine.schemas.chunk import NavTextTarget


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
    name: str = "test_chunk",
    original: str = "<p>Hello World</p>",
    translated: str | None = None,
    tokens: int = 10,
    status: TranslationStatus = TranslationStatus.PENDING,
    xpaths: list[str] | None = None,
    chunk_mode: Literal["html_fragment", "nav_text"] = "html_fragment",
    nav_targets: list[NavTextTarget] | None = None,
) -> Chunk:
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


def require_text(value: str | None) -> str:
    assert value is not None
    return value


def require_error(value: str | None) -> str:
    assert value is not None
    return value


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
    async def test_translate_step_retry_includes_previous_translation(self, mock_get_translator):
        """translate_step: retry payload includes the previous invalid translation for targeted repair."""
        chunk = make_chunk(original="<p>Hello World</p>")
        seen_inputs = []
        responses = iter(
            [
                MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("<p>Hello World</p>"),
                ),
                MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("<p>你好世界</p>"),
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
        assert "previous_translation" not in seen_inputs[0]
        assert seen_inputs[1]["previous_translation"] == "<p>Hello World</p>"
        assert seen_inputs[1]["validation_error"] == "翻译结果与原文一致，疑似未翻译"

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
    async def test_translate_step_unicode_original_is_now_accepted_as_is(self, mock_get_translator):
        """translate_step: Chinese source text should now be accepted up front instead of being retried as untranslated."""
        chunk = make_chunk(original="<p>你好世界</p>")
        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<p>你好世界</p>"
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_already_chinese_chunk_is_accepted_without_calling_translator(
        self, mock_get_translator
    ):
        """translate_step: already-Chinese content should bypass translation and be accepted as-is."""
        chunk = make_chunk(original="<p>你好世界，这是一段已经翻译完成的内容。</p>")
        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})

        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<p>你好世界，这是一段已经翻译完成的内容。</p>"
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_short_chinese_title_is_accepted_without_calling_translator(
        self, mock_get_translator
    ):
        """translate_step: short Chinese-only titles such as 索引 should also bypass translation."""
        chunk = make_chunk(original="<title>索引</title>")
        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})

        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<title>索引</title>"
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_chinese_dominant_mixed_content_is_accepted_without_calling_translator(
        self, mock_get_translator
    ):
        """translate_step: Chinese-dominant content with light English terminology should still bypass translation."""
        chunk = make_chunk(original="<p>我们将使用 Rust crate 与 Cargo 工作流来完成这个示例。</p>")
        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})

        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.ACCEPTED_AS_IS
        assert output.content.translated == "<p>我们将使用 Rust crate 与 Cargo 工作流来完成这个示例。</p>"
        mock_get_translator.assert_not_called()

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_chinese_dominant_with_untranslated_sentence_is_not_accepted_as_is(
        self, mock_get_translator
    ):
        """translate_step: Chinese-heavy chunks with an English sentence must still go through translation."""
        original = (
            "<p>这是一段已经翻译的中文内容，用来说明部署流程和安全检查的背景，"
            "并描述团队如何配置流水线、审查权限、验证发布结果、记录运行风险，"
            "确保读者能够理解上下文。This sentence remains untranslated and should be sent back through the translator.</p>"
        )
        chunk = make_chunk(original=original)
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse(
                    "<p>这是一段已经翻译的中文内容，用来说明部署流程和安全检查的背景，"
                    "并描述团队如何配置流水线、审查权限、验证发布结果、记录运行风险，"
                    "确保读者能够理解上下文。这句话仍未翻译，必须重新交给翻译器处理。</p>"
                ),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert mock_translator.arun.await_count == 1

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
        assert (
            output.content.translated
            == "<p>python -m pytest tests/engine/agents/test_workflow.py -k accepted_as_is</p>"
        )
        assert mock_translator.arun.await_count == 1

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_content_safety_retries_without_fallback(self, mock_get_translator):
        """translate_step: content safety errors should retry on the primary translator and eventually fail."""
        chunk = make_chunk(original="<p>Hello</p>")
        call_count = [0]

        async def safety_always_fails(json_input):
            call_count[0] += 1
            mock_response = MagicMock()
            mock_response.status = RunStatus.error
            mock_response.content = "相关法律法规不予显示"
            return mock_response

        mock_translator = MagicMock()
        mock_translator.arun = safety_always_fails
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert call_count[0] == 3

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_validation_failure_uses_text_node_retry_on_last_attempt(self, mock_get_translator):
        """translate_step: final retry switches to text-node mode on the primary translator for structure failures."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        seen_payloads = []

        async def invalid_response(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界\n[TEXT:2]。"),
                )
            return MagicMock(status=RunStatus.completed, content=MockTranslationResponse("<p>你好世界。</p>"))

        mock_translator = MagicMock()
        mock_translator.arun = invalid_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好<em>世界</em>。</p>"
        assert len(seen_payloads) == 3
        assert "[TEXT:0]" in seen_payloads[2]["text_to_translate"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_uses_text_node_translator_mode_on_final_retry(self, mock_get_translator):
        """translate_step: final structure-repair retry should request the dedicated text-node translator mode."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        requested_modes = []

        async def invalid_response(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界\n[TEXT:2]。"),
                )
            return MagicMock(status=RunStatus.completed, content=MockTranslationResponse("<p>你好世界。</p>"))

        mock_translator = MagicMock()
        mock_translator.arun = invalid_response

        def translator_factory(*args, **kwargs):
            requested_modes.append(kwargs.get("mode"))
            return mock_translator

        mock_get_translator.side_effect = translator_factory

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert requested_modes == ["html", "html", "text_node"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_text_node_retry_includes_previous_translation_and_error_history(
        self, mock_get_translator
    ):
        """translate_step: text-node fallback should retry with previous output and accumulated validation context."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        seen_payloads = []

        async def translator_response(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            if "[TEXT:0]" in payload["text_to_translate"]:
                if "previous_translation" not in payload:
                    return MagicMock(
                        status=RunStatus.completed,
                        content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界"),
                    )
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界\n[TEXT:2]。"),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = translator_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        text_payloads = [payload for payload in seen_payloads if "[TEXT:0]" in payload["text_to_translate"]]
        assert len(text_payloads) == 2
        assert "previous_translation" not in text_payloads[0]
        assert text_payloads[1]["previous_translation"] == "[TEXT:0]你好\n[TEXT:1]世界"
        assert "标签属性不一致" in text_payloads[1]["validation_error"]
        assert "TEXT 标记不一致" in text_payloads[1]["validation_error"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_text_node_retry_repairs_nested_code_placeholder_mismatch(self, mock_get_translator):
        """translate_step: text-node fallback should reject payloads that drop nested CODE placeholders and retry."""
        chunk = make_chunk(original="<p>Hello [CODE:0]<em>world</em>.</p>")
        seen_payloads = []

        async def translator_response(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            if "[TEXT:0]" in payload["text_to_translate"]:
                if "previous_translation" not in payload:
                    return MagicMock(
                        status=RunStatus.completed,
                        content=MockTranslationResponse("[TEXT:0]你好 \n[TEXT:1]世界\n[TEXT:2]。"),
                    )
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好 [CODE:0]\n[TEXT:1]世界\n[TEXT:2]。"),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = translator_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好 [CODE:0]<em>世界</em>。</p>"
        text_payloads = [payload for payload in seen_payloads if "[TEXT:0]" in payload["text_to_translate"]]
        assert len(text_payloads) == 2
        assert "CODE 占位符不一致" in text_payloads[1]["validation_error"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_routes_high_risk_chunk_directly_to_text_node_mode(self, mock_get_translator):
        """translate_step: inline-heavy complex chunks should skip HTML regeneration and start in text-node mode."""
        chunk = make_chunk(
            original=(
                "<p>"
                "One <i>two</i> three <b>four</b> five <em>six</em> seven <span>eight</span> "
                "nine <strong>ten</strong> eleven <a href='#'>twelve</a> thirteen <code>fourteen</code>."
                "</p>"
            )
        )
        requested_modes = []

        async def translator_response(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                lines = []
                for line in payload["text_to_translate"].splitlines():
                    marker, text = line.split("]", 1)
                    lines.append(f"{marker}]中文{text}")
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("\n".join(lines)),
                )
            return MagicMock(status=RunStatus.completed, content=MockTranslationResponse("<p>不应走到这里</p>"))

        mock_translator = MagicMock()
        mock_translator.arun = translator_response

        def translator_factory(*args, **kwargs):
            requested_modes.append(kwargs.get("mode"))
            return mock_translator

        mock_get_translator.side_effect = translator_factory

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert requested_modes
        assert all(mode == "text_node" for mode in requested_modes)

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_text_node_output_decodes_literal_newline_escapes(self, mock_get_translator):
        """translate_step: model output containing literal \\n is normalized before text-node parsing."""
        chunk = make_chunk(
            original=(
                "<p>"
                "One <i>two</i> three <b>four</b> five <em>six</em> seven <span>eight</span> "
                "nine <strong>ten</strong> eleven <a href='#'>twelve</a> thirteen <code>fourteen</code>."
                "</p>"
            )
        )

        async def translator_response(json_input):
            payload = json.loads(json_input)
            lines = []
            for line in payload["text_to_translate"].splitlines():
                marker, text = line.split("]", 1)
                lines.append(f"{marker}]中文{text}")
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("\\n".join(lines)),
            )

        mock_translator = MagicMock()
        mock_translator.arun = translator_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert "\\n" not in require_text(output.content.translated)

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_preserves_literal_newline_escape_in_content(self, mock_get_translator):
        """translate_step: literal \\n used as content is not converted into a real newline."""
        chunk = make_chunk(original="<p>Use newline escapes.</p>")
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>使用 \\n 表示换行。</p>"),
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>使用 \\n 表示换行。</p>"

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_keeps_simple_chunk_on_html_mode(self, mock_get_translator):
        """translate_step: simple low-risk chunks should still prefer HTML mode for naturalness."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        requested_modes = []

        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好 <em>世界</em>。</p>"),
            )
        )

        def translator_factory(*args, **kwargs):
            requested_modes.append(kwargs.get("mode"))
            return mock_translator

        mock_get_translator.side_effect = translator_factory

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert requested_modes == ["html"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_routes_code_and_math_heavy_chunk_directly_to_text_node_mode(
        self, mock_get_translator
    ):
        """translate_step: code-placeholder headings with dense mathy inline markup should bypass HTML mode directly."""
        chunk = make_chunk(
            original=(
                "<section><h3>[CODE:0]</h3>"
                "<p>Let <i>x</i><sub>1</sub> and <i>y</i><sup>2</sup> define the series.</p>"
                "<p>Then <i>z</i><sub>3</sub> = <i>x</i><sub>1</sub> + <i>y</i><sup>2</sup>.</p>"
                "</section>"
            )
        )
        requested_modes = []

        async def translator_response(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                lines = []
                for line in payload["text_to_translate"].splitlines():
                    marker, text = line.split("]", 1)
                    lines.append(f"{marker}]中文{text}")
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("\n".join(lines)),
                )
            return MagicMock(
                status=RunStatus.completed, content=MockTranslationResponse("<section>不应走到这里</section>")
            )

        mock_translator = MagicMock()
        mock_translator.arun = translator_response

        def translator_factory(*args, **kwargs):
            requested_modes.append(kwargs.get("mode"))
            return mock_translator

        mock_get_translator.side_effect = translator_factory

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert requested_modes
        assert all(mode == "text_node" for mode in requested_modes)

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_error_status_retries_without_fallback_and_keeps_provider_error_message(
        self, mock_get_translator
    ):
        """translate_step: non-safety provider errors should preserve the real error text across primary retries."""
        chunk = make_chunk(original="<p>Hello</p>")
        call_count = [0]
        seen_payloads = []

        async def provider_error(json_input):
            call_count[0] += 1
            seen_payloads.append(json.loads(json_input))
            return MagicMock(
                status=RunStatus.error,
                content="Server disconnected without sending a response.",
            )

        mock_translator = MagicMock()
        mock_translator.arun = provider_error
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATION_FAILED
        assert call_count[0] == 3
        assert "validation_error" not in seen_payloads[0]
        assert seen_payloads[1]["validation_error"] == "Server disconnected without sending a response."

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_accepts_stringified_json_response_with_trailing_noise(self, mock_get_translator):
        """translate_step: tolerate provider responses that return JSON as a raw string with trailing noise."""
        chunk = make_chunk(original="<p>Hello</p>")
        mock_translator = MagicMock()
        mock_translator.arun = AsyncMock(
            return_value=MagicMock(
                status=RunStatus.completed,
                content='{"translation":"<p>你好</p>"}\n\nextra trailing text',
            )
        )
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.success is True
        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好</p>"

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_freezes_high_risk_void_tags_before_translation(self, mock_get_translator):
        """translate_step: img/br/hr/meta/link should be frozen before sending HTML to the translator."""
        chunk = make_chunk(
            original='<p>Hello</p><p><img alt="Publisher logo." src="../images/pub.jpg"/><br/></p>',
            xpaths=["/html/body/p[1]", "/html/body/p[2]"],
        )
        seen_payloads = []

        async def translated_with_placeholders(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好</p><p>[TAG:0][TAG:1]</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = translated_with_placeholders
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert "[TAG:0]" in seen_payloads[0]["text_to_translate"]
        assert "[TAG:1]" in seen_payloads[0]["text_to_translate"]
        assert (
            output.content.translated == '<p>你好</p><p><img alt="Publisher logo." src="../images/pub.jpg"/><br/></p>'
        )

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_recovers_when_frozen_tag_placeholder_is_missing(self, mock_get_translator):
        """translate_step: missing frozen-tag placeholders can still recover via text-node mode on the final retry."""
        chunk = make_chunk(
            original='<p>Hello</p><p><img alt="Publisher logo." src="../images/pub.jpg"/></p>',
            xpaths=["/html/body/p[1]", "/html/body/p[2]"],
        )
        seen_payloads = []

        async def missing_placeholder(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好"),
                )
            return MagicMock(status=RunStatus.completed, content=MockTranslationResponse("<p>你好</p><p></p>"))

        mock_translator = MagicMock()
        mock_translator.arun = missing_placeholder
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == '<p>你好</p><p><img alt="Publisher logo." src="../images/pub.jpg"/></p>'
        assert len(seen_payloads) == 3
        assert "[TEXT:0]" in seen_payloads[2]["text_to_translate"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_uses_text_node_fallback_after_repeated_structure_failures(self, mock_get_translator):
        """translate_step: repeated HTML structure mismatches should fall back to text-node translation on last try."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        seen_standard_payloads = []
        seen_text_payloads = []

        async def structurally_broken_response(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                seen_text_payloads.append(payload)
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界\n[TEXT:2]。"),
                )
            seen_standard_payloads.append(payload)
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = structurally_broken_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好<em>世界</em>。</p>"
        assert len(seen_standard_payloads) == 2
        assert len(seen_text_payloads) == 1
        assert "[TEXT:0]" in seen_text_payloads[0]["text_to_translate"]
        assert "[TEXT:1]" in seen_text_payloads[0]["text_to_translate"]
        assert "[TEXT:2]" in seen_text_payloads[0]["text_to_translate"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_batches_text_node_fallback_for_large_html(self, mock_get_translator):
        """translate_step: high-risk large HTML should be split into multiple direct text-node batches."""
        original = "<div>" + "".join(f"<span>Paragraph {i}</span>" for i in range(30)) + "</div>"
        chunk = make_chunk(original=original, xpaths=["/html/body/div"])
        text_payloads = []

        async def structurally_broken_response(json_input):
            payload_json = json.loads(json_input)
            if "[TEXT:0]" in payload_json["text_to_translate"]:
                text_payloads.append(payload_json)
                lines = []
                for line in payload_json["text_to_translate"].splitlines():
                    if "]" not in line:
                        continue
                    marker, text = line.split("]", 1)
                    lines.append(f"{marker}]中文{text}")
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("\n".join(lines)),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<div>broken</div>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = structurally_broken_response
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert len(text_payloads) == 4
        assert text_payloads[0]["text_to_translate"].count("[TEXT:") == 8
        assert text_payloads[1]["text_to_translate"].count("[TEXT:") == 8
        assert text_payloads[2]["text_to_translate"].count("[TEXT:") == 8
        assert text_payloads[3]["text_to_translate"].count("[TEXT:") == 6
        translated = require_text(output.content.translated)
        assert "中文Paragraph 0" in translated
        assert "中文Paragraph 29" in translated

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_recovers_missing_leading_text_marker(self, mock_get_translator):
        """translate_step: text-node fallback tolerates the model dropping only the first TEXT marker."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")
        seen_payloads = []

        async def structurally_broken_then_shifted_markers(json_input):
            payload = json.loads(json_input)
            seen_payloads.append(payload)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("你好[TEXT:1]世界\n[TEXT:2]。"),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = structurally_broken_then_shifted_markers
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好<em>世界</em>。</p>"
        assert len(seen_payloads) == 3
        assert "[TEXT:0]" in seen_payloads[2]["text_to_translate"]

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_recovers_missing_trailing_text_marker(self, mock_get_translator):
        """translate_step: text-node fallback tolerates the model dropping only the last TEXT marker line prefix."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")

        async def structurally_broken_then_missing_last_marker(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:1]世界\n。"),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = structurally_broken_then_missing_last_marker
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好<em>世界</em>。</p>"

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_recovers_wrong_text_marker_numbering_per_line(self, mock_get_translator):
        """translate_step: text-node fallback normalizes per-line marker numbering when line order is still intact."""
        chunk = make_chunk(original="<p>Hello <em>world</em>.</p>")

        async def structurally_broken_then_wrong_marker_numbers(json_input):
            payload = json.loads(json_input)
            if "[TEXT:0]" in payload["text_to_translate"]:
                return MagicMock(
                    status=RunStatus.completed,
                    content=MockTranslationResponse("[TEXT:0]你好\n[TEXT:0]世界\n[TEXT:0]。"),
                )
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("<p>你好世界。</p>"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = structurally_broken_then_wrong_marker_numbers
        mock_get_translator.return_value = mock_translator

        step_input = MagicMock(input=chunk, additional_data={"glossary": {}})
        output = await translate_step(step_input)

        assert output.content.status == TranslationStatus.TRANSLATED
        assert output.content.translated == "<p>你好<em>世界</em>。</p>"

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

    @patch("engine.agents.workflow.get_translator")
    async def test_translate_step_nav_text_untranslated_payload_fails(self, mock_get_translator):
        """translate_step: nav_text payload cannot remain as an untranslated English title."""
        chunk = make_chunk(original="[NAVTXT:0] Chapter 1 Advanced Security", xpaths=[], chunk_mode="nav_text")
        call_count = [0]

        async def untranslated_payload(json_input):
            call_count[0] += 1
            return MagicMock(
                status=RunStatus.completed,
                content=MockTranslationResponse("[NAVTXT:0] Chapter 1 Advanced Security"),
            )

        mock_translator = MagicMock()
        mock_translator.arun = untranslated_payload
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
        assert "没有从上一步收到有效的翻译文本" in require_error(output.error)

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
            original="<p>找到<i>一个</i>客户</p>",
            translated="<p>找到<i>一个</i>客户</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"客户": "用户"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        assert output.content.translated == "<p>找到<i>一个</i>用户</p>"
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
        assert "缺少翻译文本" in require_error(output.error)

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

    @patch("engine.agents.workflow.logger")
    def test_apply_corrections_step_logs_unmatched_corrections(self, mock_logger):
        """apply_corrections_step: logs when proofreading suggestions survive filtering but hit no text nodes."""
        chunk = make_chunk(
            original="<p>Hello</p>",
            translated="<p>你好</p>",
            status=TranslationStatus.TRANSLATED,
        )
        proofreading_result = MockProofreadingResult({"您好": "你好"})
        step_data = {"chunk": chunk, "proofreading_result": proofreading_result}
        step_input = MagicMock(previous_step_content=step_data)

        output = apply_corrections_step(step_input)

        assert output.success is True
        mock_logger.info.assert_any_call("校对器发现 1 个潜在的校对建议。")
        mock_logger.info.assert_any_call(
            "校对建议统计：总计 1，过滤 0，进入替换 1，文本命中 0，未命中 1，实际替换 0 处。"
        )

    @patch("engine.agents.workflow.validate_translated_html", return_value=(False, "mock validation failure"))
    @patch("engine.agents.workflow.logger")
    def test_apply_corrections_step_logs_rollback_after_validation_failure(self, mock_logger, _mock_validate):
        """apply_corrections_step: logs how many applied corrections were rolled back after structural validation fails."""
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
        assert output.content.translated == "<p>你好</p>"
        mock_logger.warning.assert_any_call(
            "Chunk 'test_chunk' 校对后校验失败，回退到校对前译文: mock validation failure；已撤销 1 处替换（命中 1 条建议）。"
        )


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
        assert "你好世界" in require_text(response.content.translated)

    @patch("engine.agents.workflow.get_translator")
    @patch("engine.agents.workflow.get_proofer")
    async def test_full_workflow_keeps_accepted_as_is_without_proofreading(
        self, mock_get_proofer, mock_get_translator
    ):
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
