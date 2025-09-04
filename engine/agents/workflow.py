import json
import re
from textwrap import dedent

from agno.agent import Agent
from agno.workflow import RunResponse, Workflow

from engine.constant import PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .proofer import ProofreadingResult, get_proofer
from .translator import TranslationResponse, get_translator


class TranslatorWorkflow(Workflow):
    """
    一个使用 AI 代理处理翻译的工作流。
    """

    description: str = dedent("""\
    An intelligent translation workflow that produces high-quality,
    proofread Chinese text from an English source.
    It carefully preserves placeholders and XML tags as required.""")

    translator: Agent = get_translator()
    proofer: Agent = get_proofer()

    def _get_placeholders(self, text: str) -> list[str]:
        """
        从文本中提取所有占位符。

        Args:
            text: 输入文本。

        Returns:
            包含所有占位符的列表。
        """
        return re.findall(PLACEHOLDER_PATTERN, text)

    def _validate(self, original: str, translated: str) -> bool:
        """
        验证翻译结果中的占位符是否与原始内容完全一致。

        Args:
            original: 原始文本。
            translated: 翻译后的文本。

        Returns:
            如果占位符数量和内容完全一致，返回 True，否则返回 False。
        """
        # 提取所有占位符
        original_placeholders = self._get_placeholders(original)
        translated_placeholders = self._get_placeholders(translated)

        # 检查内容是否完全一致 (使用 set 比较唯一值，但其实应该检查顺序和重复，如果需要)
        if set(original_placeholders) != set(translated_placeholders):
            logger.error("占位符内容不匹配！")
            logger.error(f"原始占位符列表: {original_placeholders}")
            logger.error(f"翻译占位符列表: {translated_placeholders}")
            return False

        # 可选: 如果需要检查顺序/重复次数，用列表比较: if original_placeholders != translated_placeholders
        return True

    async def arun(self, chunk: Chunk) -> RunResponse:
        """异步生成器入口点，处理分块内容并返回状态。"""

        translator_input = {
            "text_to_translate": chunk.original,
            "untranslatable_placeholders": self._get_placeholders(chunk.original),
        }
        translation_response: RunResponse = await self.translator.arun(
            json.dumps(translator_input, ensure_ascii=False, indent=2)
        )
        # Validate the response type to ensure the agent followed instructions.
        if not isinstance(translation_response.content, TranslationResponse):
            error_msg = "Translation step failed: The agent returned an unexpected response type."
            logger.error(error_msg)
            return RunResponse(content=error_msg, run_id=self.run_id)
        translated = translation_response.content.translation
        translated = translated.replace("您", "你")
        logger.info(f"Translated text received: '{translated[:70]}...'")
        # Validate placeholders
        if not self._validate(chunk.original, translated):
            error_msg = "Translation step failed: Placeholder mismatch detected."
            logger.error(error_msg)
            return RunResponse(run_id=self.run_id, content=error_msg)
        chunk.status = TranslationStatus.TRANSLATED
        chunk.translated = translated

        # --- Step 2: Proofread the translated text ---
        proofer_input = {
            "text_to_proofread": translated,
            "untranslatable_placeholders": self._get_placeholders(translated),
        }
        proofer_response: RunResponse = await self.proofer.arun(json.dumps(proofer_input, ensure_ascii=False, indent=2))

        # Validate the proofreader's response.
        if not isinstance(proofer_response.content, ProofreadingResult):
            error_msg = "Proofreading step failed: The agent returned an unexpected response type."
            logger.error(error_msg)
            # As a fallback, we return the un-proofread translation.
            return RunResponse(run_id=self.run_id, content=translated)

        corrections = proofer_response.content.corrections
        logger.info(f"Proofreader found {len(corrections)} potential corrections.")

        # --- Step 3: Apply corrections and return the final result ---
        final_text = translated
        if corrections:
            for original, corrected in corrections.items():
                final_text = final_text.replace(original, corrected)
            chunk.status = TranslationStatus.COMPLETED
            chunk.translated = final_text
            logger.info("Successfully applied corrections.")
        final_text = final_text.replace("您", "你")

        return RunResponse(run_id=self.run_id, content=final_text)
