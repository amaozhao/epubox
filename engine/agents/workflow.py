import json
import re

from agno.workflow import Step, StepInput, StepOutput, Workflow

from engine.constant import PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator


def _get_placeholders(text: str) -> list[str]:
    """
    从文本中提取所有占位符。
    """
    return re.findall(PLACEHOLDER_PATTERN, text)


def _validate_placeholders(original: str, translated: str) -> bool:
    """
    验证翻译结果中的占位符是否与原始内容完全一致。
    """
    original_placeholders = _get_placeholders(original)
    translated_placeholders = _get_placeholders(translated)

    if set(original_placeholders) != set(translated_placeholders):
        logger.error("占位符内容不匹配！")
        logger.error(f"原始占位符列表: {original_placeholders}")
        logger.error(f"翻译占位符列表: {translated_placeholders}")
        return False
    return True


# Step 1: Translate
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        # 如果已经翻译过，直接将 chunk 传递给下一步
        return StepOutput(content=chunk)

    translator = get_translator()
    translator_input = {
        "text_to_translate": chunk.original,
        "untranslatable_placeholders": _get_placeholders(chunk.original),
    }
    response = await translator.arun(json.dumps(translator_input, ensure_ascii=False, indent=2))

    if not isinstance(response.content, TranslationResponse):
        error_msg = "翻译步骤失败：代理返回了意外的响应类型。"
        logger.error(error_msg)
        # 失败时也返回原始 chunk，但可以考虑设置一个错误状态
        return StepOutput(content=chunk, success=False, error=error_msg)

    translated = response.content.translation
    logger.info(f"接收到翻译文本: '{translated[:70]}...'")

    if not _validate_placeholders(chunk.original, translated):
        error_msg = "翻译步骤失败：检测到占位符不匹配。"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)

    chunk.status = TranslationStatus.TRANSLATED
    chunk.translated = translated
    return StepOutput(content=chunk)


# Step 2: Proofread
async def proofread_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.previous_step_content  # type: ignore
    translated_text = chunk.translated

    if not translated_text or not isinstance(translated_text, str):
        error_msg = "校对步骤失败：没有从上一步收到有效的翻译文本。"
        logger.error(error_msg)
        # 将 chunk 和一个空的校对结果传递下去
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer = get_proofer()
    proofer_input = {
        "text_to_proofread": translated_text,
        "untranslatable_placeholders": _get_placeholders(translated_text),
    }
    response = await proofer.arun(json.dumps(proofer_input, ensure_ascii=False, indent=2))

    proofreading_result: ProofreadingResult
    if not isinstance(response.content, ProofreadingResult):
        error_msg = "校对步骤失败：代理返回了意外的响应类型。"
        logger.error(error_msg)
        # 作为回退，我们使用一个空的 ProofreadingResult 对象。
        proofreading_result = ProofreadingResult(corrections={})
    else:
        proofreading_result = response.content

    return StepOutput(content={"chunk": chunk, "proofreading_result": proofreading_result})


# Step 3: Apply Corrections
def apply_corrections_step(step_input: StepInput) -> StepOutput:
    step_data: dict = step_input.previous_step_content  # type: ignore
    chunk: Chunk = step_data["chunk"]
    proofreading_result: ProofreadingResult = step_data["proofreading_result"]
    translated_text = chunk.translated

    if not translated_text or not isinstance(translated_text, str):
        error_msg = "应用校对建议步骤失败：缺少翻译文本。"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)

    corrections = proofreading_result.corrections
    logger.info(f"校对器发现 {len(corrections)} 个潜在的校对建议。")

    final_text = translated_text
    if corrections:
        for original, corrected in corrections.items():
            final_text = final_text.replace(original, corrected)
        logger.info("成功应用校对建议。")

    final_text = final_text.replace("您", "你").replace("。。", "。")

    chunk.translated = final_text
    chunk.status = TranslationStatus.COMPLETED

    return StepOutput(content=chunk)


def get_translator_workflow() -> Workflow:
    """
    构建并返回翻译工作流。
    """
    return Workflow(
        name="TranslatorWorkflow",
        description="一个智能翻译工作流，可从英文源文本生成高质量、经过校对的中文文本。它会根据要求仔细保留占位符和 XML 标签。",
        steps=[
            Step(name="translate", executor=translate_step),
            Step(name="proofread", executor=proofread_step),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
