import json
from typing import Dict

from agno.run import RunStatus
from agno.workflow import Step, StepInput, StepOutput, Workflow

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .models import fallback_model
from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator
from engine.agents.verifier import validate_translated_html

# 需要内容安全审核 fallback 的错误码
CONTENT_SAFETY_ERROR_CODES = {10014, 500, 400}
CONTENT_SAFETY_KEYWORDS = ["相关法律法规", "不予显示", "安全审核", "content policy", "safety policy"]

# 最大重试次数
MAX_TRANSLATION_RETRIES = 3


def is_content_safety_error(error_msg: str = "", status_code: int | None = None) -> bool:
    """判断是否是内容安全审核错误"""
    if status_code in CONTENT_SAFETY_ERROR_CODES:
        return True
    for keyword in CONTENT_SAFETY_KEYWORDS:
        if keyword in error_msg:
            return True
    return False


def filter_glossary_terms(text: str, glossary: Dict[str, str]) -> Dict[str, str]:
    """从文本中过滤出出现在术语表中的术语"""
    found_terms = {}
    sorted_terms = sorted(glossary.keys(), key=len, reverse=True)
    for term in sorted_terms:
        if term.lower() in text.lower():
            found_terms[term] = glossary[term]
    return found_terms


async def _call_translator(
    text: str,
    glossary: Dict[str, str] = None,
    previous_translation: str | None = None,
    error_msg: str | None = None,
    use_fallback: bool = False,
) -> str:
    """调用翻译模型

    Args:
        text: 待翻译文本
        glossary: 术语表
        previous_translation: 上一次翻译失败的结果（用于重试时参考）
        error_msg: 上一次翻译失败的具体错误信息
    """
    filtered_glossary = filter_glossary_terms(text, glossary) if glossary else {}
    translator_input = {
        "text_to_translate": text,
        "glossaries": filtered_glossary,
    }
    if previous_translation:
        translator_input["previous_translation"] = previous_translation
    if error_msg:
        translator_input["validation_error"] = error_msg

    try:
        translator = get_translator(fallback_model) if use_fallback else get_translator()
        response = await translator.arun(json.dumps(translator_input, ensure_ascii=False, indent=2))

        raw_content = response.content
        if response.status == RunStatus.error:
            error_content = str(raw_content) if raw_content else ""
            if is_content_safety_error(error_content):
                raise ValueError(f"内容安全审核失败: {error_content[:100]}")
        if isinstance(raw_content, TranslationResponse):
            return raw_content.translation
        raise ValueError(f"翻译响应格式错误: {type(raw_content)}")
    except Exception as e:
        logger.error(f"翻译模型调用异常: {type(e).__name__}: {e}")
        raise


async def _translate_with_fallback(chunk: Chunk, glossary: Dict[str, str] = None) -> Chunk:
    """翻译并用 validate_translated_html 验证 HTML 结构，失败则标记待手动处理"""
    original = chunk.original
    last_error_msg = None
    used_fallback = False

    for attempt in range(MAX_TRANSLATION_RETRIES):
        try:
            translated = await _call_translator(original, glossary, None, last_error_msg, use_fallback=used_fallback)
            translated = translated.replace("\\n", "\n")
        except Exception as e:
            error_str = str(e)
            if not used_fallback and is_content_safety_error(error_str):
                logger.warning("主模型翻译失败（内容安全审核），尝试使用备用模型...")
                used_fallback = True
                last_error_msg = None
                continue
            logger.warning(f"翻译重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 异常: {e}")
            last_error_msg = None
            continue

        is_valid, error_msg = validate_translated_html(original, translated)
        if is_valid:
            chunk.translated = translated
            chunk.status = (
                TranslationStatus.ACCEPTED_AS_IS if error_msg == "accepted_as_is" else TranslationStatus.TRANSLATED
            )
            return chunk

        logger.warning(f"翻译重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 失败: {error_msg}")
        last_error_msg = error_msg

    # 所有重试都失败 → 标记为 TRANSLATION_FAILED，保留原文保结构
    logger.warning(f"Chunk '{chunk.name}': 翻译重试全部失败，标记为 TRANSLATION_FAILED")
    chunk.translated = ""
    chunk.status = TranslationStatus.TRANSLATION_FAILED
    return chunk


# Step 1: Translate
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    additional_data = step_input.additional_data or {}
    glossary = additional_data.get("glossary", {})

    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        return StepOutput(content=chunk)

    if not chunk.original or not chunk.original.strip():
        logger.info(f"Chunk '{chunk.name}' 无可翻译内容，直接返回原文")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return StepOutput(content=chunk)

    try:
        chunk = await _translate_with_fallback(chunk, glossary)
        return StepOutput(content=chunk)
    except Exception as e:
        error_msg = f"翻译步骤失败：{e}"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)


# Step 2: Proofread
async def proofread_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.previous_step_content  # type: ignore
    translated = getattr(chunk, "translated")

    # 翻译失败或接受原文，跳过校对
    if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.ACCEPTED_AS_IS):
        logger.info(f"Chunk '{chunk.name}' 无需校对，跳过校对步骤")
        return StepOutput(content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})})

    if not translated or not isinstance(translated, str):
        error_msg = "校对步骤失败：没有从上一步收到有效的翻译文本。"
        logger.error(error_msg)
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer_input = {"text_to_proofread": translated}

    max_attempts = 3
    proofreading_result = None
    used_fallback = False

    for attempt in range(max_attempts):
        proofer = get_proofer(fallback_model) if used_fallback else get_proofer()
        try:
            response = await proofer.arun(json.dumps(proofer_input, ensure_ascii=False, indent=2))
            if isinstance(response.content, ProofreadingResult):
                proofreading_result = response.content
                break
            if response.status == RunStatus.error:
                error_content = str(response.content) if response.content else ""
                if not used_fallback and is_content_safety_error(error_content):
                    logger.warning("主模型校对失败（内容安全审核），尝试使用备用模型...")
                    used_fallback = True
                    continue
            logger.warning(f"校对步骤失败：代理返回了意外的响应类型 (attempt {attempt + 1}/{max_attempts})")
        except Exception as e:
            if not used_fallback and is_content_safety_error(str(e)):
                logger.warning("主模型校对异常（内容安全审核），尝试使用备用模型...")
                used_fallback = True
                continue
            logger.error(f"校对步骤异常 (attempt {attempt + 1}/{max_attempts}): {e}")

        if attempt < max_attempts - 1:
            logger.info("将在下次尝试中重试校对步骤...")

    if proofreading_result is None:
        error_msg = f"校对步骤失败：经过 {max_attempts} 次尝试后仍未成功。"
        logger.error(error_msg)
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    return StepOutput(content={"chunk": chunk, "proofreading_result": proofreading_result})


# Step 3: Apply Corrections
def apply_corrections_step(step_input: StepInput) -> StepOutput:
    step_data: dict = step_input.previous_step_content  # type: ignore
    chunk: Chunk = step_data["chunk"]
    proofreading_result: ProofreadingResult = step_data["proofreading_result"]
    translated_text = chunk.translated

    # 翻译失败或接受原文，跳过应用校对建议
    if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.ACCEPTED_AS_IS):
        logger.info(f"Chunk '{chunk.name}' 无需应用校对建议，直接返回")
        return StepOutput(content=chunk)

    if not translated_text or not isinstance(translated_text, str):
        error_msg = "应用校对建议步骤失败：缺少翻译文本。"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)

    corrections = proofreading_result.corrections
    logger.info(f"校对器发现 {len(corrections)} 个潜在的校对建议。")

    final_text = translated_text
    if corrections:
        # 按位置从后往前替换，避免位置偏移影响后续替换
        replacements = []
        for original, corrected in corrections.items():
            start = 0
            while True:
                pos = final_text.find(original, start)
                if pos == -1:
                    break
                replacements.append((pos, len(original), corrected))
                start = pos + len(original)

        # 从后往前替换
        for pos, length, corrected in sorted(replacements, reverse=True):
            final_text = final_text[:pos] + corrected + final_text[pos + length :]

        logger.info(f"成功应用 {len(replacements)} 个校对建议。")

    # 后处理：统一词汇和标点
    final_text = final_text.replace("您", "你").replace("大型语言模型", "大语言模型")
    final_text = final_text.replace("。。", "。").replace("，，", "，")

    chunk.translated = final_text
    chunk.status = TranslationStatus.COMPLETED

    return StepOutput(content=chunk)


def get_translator_workflow() -> Workflow:
    """构建并返回翻译工作流（翻译→校对→修正）"""
    return Workflow(
        name="TranslatorWorkflow",
        description="智能翻译工作流：直接翻译+HTML结构验证，校对提升质量",
        steps=[
            Step(name="translate", executor=translate_step),
            Step(name="proofread", executor=proofread_step),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
