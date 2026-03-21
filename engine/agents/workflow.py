import json
import re
from collections import Counter
from typing import Dict, List

from agno.run import RunStatus
from agno.workflow import Loop, Step, StepInput, StepOutput, Workflow

from engine.constant import PLACEHOLDER_PATTERN
from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .models import fallback_model
from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator

# 需要内容安全审核 fallback 的错误码
CONTENT_SAFETY_ERROR_CODES = {10014, 500, 400}
CONTENT_SAFETY_KEYWORDS = ["相关法律法规", "不予显示", "安全审核", "content policy", "safety policy"]


def is_content_safety_error(error_msg: str = "", status_code: int | None = None) -> bool:
    """判断是否是内容安全审核错误"""
    if status_code in CONTENT_SAFETY_ERROR_CODES:
        return True
    for keyword in CONTENT_SAFETY_KEYWORDS:
        if keyword in error_msg:
            return True
    return False


def _get_placeholders(text: str) -> list[str]:
    """
    从文本中提取所有占位符。
    """
    return re.findall(PLACEHOLDER_PATTERN, text)


def _validate_and_fix_placeholders(original: str, translated: str) -> tuple[bool, str]:
    """
    验证占位符并自动修正大小写和顺序问题。

    新策略：
    1. 统计每个占位符在原始文本中的出现次数（大小写敏感）
    2. 检查翻译文本中占位符的数量和类型是否匹配
    3. 逐个替换翻译文本中的占位符为原始大小写格式
    4. 返回验证结果和修正后的文本

    这样即使顺序错乱，也能正确修正每个占位符。
    """
    original_placeholders = _get_placeholders(original)
    translated_placeholders = _get_placeholders(translated)

    # 1. 检查数量是否匹配
    if len(original_placeholders) != len(translated_placeholders):
        logger.error("占位符数量不匹配！")
        logger.error(f"原始文本占位符数量: {len(original_placeholders)}")
        logger.error(f"翻译文本占位符数量: {len(translated_placeholders)}")
        return False, translated

    # 2. 统计原始文本中每个占位符的出现次数（大小写敏感）
    original_lower_counts = Counter(ph.lower() for ph in original_placeholders)
    translated_lower_counts = Counter(ph.lower() for ph in translated_placeholders)

    if original_lower_counts != translated_lower_counts:
        logger.error("占位符类型和数量不匹配！")
        logger.error(f"原始占位符统计: {dict(original_lower_counts)}")
        logger.error(f"翻译占位符统计: {dict(translated_lower_counts)}")
        return False, translated

    # 4. 创建大小写映射：lower -> correct_case
    # 对于重复的占位符，我们需要确保使用正确的原始格式
    lower_to_correct = {}
    used_placeholders = set()

    for orig_ph in original_placeholders:
        lower_ph = orig_ph.lower()
        if lower_ph not in lower_to_correct:
            lower_to_correct[lower_ph] = orig_ph
        # 如果有多个相同字符但不同大小写的占位符，我们记录所有变体
        elif orig_ph not in used_placeholders:
            # 这里简化处理：使用第一次遇到的格式
            # 在实际应用中，可能需要更复杂的逻辑来匹配重复占位符
            pass
        used_placeholders.add(orig_ph)

    # 5. 修正翻译文本中的占位符
    corrected_text = translated
    corrections_made = []

    # 按照翻译文本中的顺序，逐个替换为正确的格式
    for trans_ph in set(translated_placeholders):  # 使用set去重
        trans_lower = trans_ph.lower()
        if trans_lower in lower_to_correct:
            correct_ph = lower_to_correct[trans_lower]
            if trans_ph != correct_ph:
                # 替换所有实例
                corrected_text = corrected_text.replace(trans_ph, correct_ph)
                corrections_made.append(f"'{trans_ph}' -> '{correct_ph}'")

    if corrections_made:
        logger.info(f"修正了占位符格式: {corrections_made}")

    # 6. 最终验证：确保修正后的占位符与原始完全匹配
    final_placeholders = _get_placeholders(corrected_text)

    # 排序后比较，因为我们只关心集合是否相同，不关心顺序
    if sorted(final_placeholders) != sorted(original_placeholders):
        logger.error("占位符修正后仍然不匹配！")
        logger.error(f"期望: {sorted(original_placeholders)}")
        logger.error(f"实际: {sorted(final_placeholders)}")
        return False, translated

    logger.info("占位符验证和修正成功")
    return True, corrected_text


# 保留向后兼容的验证函数
def _validate_placeholders(original: str, translated: str) -> bool:
    """
    向后兼容的验证函数。
    """
    is_valid, _ = _validate_and_fix_placeholders(original, translated)
    return is_valid


def filter_glossary_terms(text: str, glossary: Dict[str, str]) -> Dict[str, str]:
    """
    从文本中过滤出出现在术语表中的术语。

    Args:
        text: 要检查的文本
        glossary: 术语表字典

    Returns:
        出现在文本中的术语字典
    """
    found_terms = {}
    # 按术语长度降序排序，确保较长的术语先被匹配
    sorted_terms = sorted(glossary.keys(), key=len, reverse=True)

    for term in sorted_terms:
        if term.lower() in text.lower():
            found_terms[term] = glossary[term]

    logger.info(f"在文本中发现 {len(found_terms)} 个术语表中的术语")
    return found_terms


def _has_translatable_content(text: str) -> bool:
    """检查是否有可翻译的实际文本内容（排除纯 HTML 标签）"""
    # 移除所有 HTML 标签后检查是否还有内容
    text_without_tags = re.sub(r"<[^>]+>", "", text)
    return bool(text_without_tags.strip())


# Step 1: Translate
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        # 如果已经翻译过，直接将 chunk 传递给下一步
        return StepOutput(content=chunk)

    # 检查是否有可翻译内容，没有则直接返回原文
    if not _has_translatable_content(chunk.original):
        logger.info(f"Chunk '{chunk.name}' 无可翻译内容，直接返回原文")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return StepOutput(content=chunk)

    glossaries = step_input.additional_data.get("glossary", {}) if step_input.additional_data else {}
    translator_input = {
        "text_to_translate": chunk.original,
        "untranslatable_placeholders": _get_placeholders(chunk.original),
        "glossaries": filter_glossary_terms(chunk.original, glossaries),
    }

    # 先用主模型尝试
    translator = get_translator()
    response = await translator.arun(json.dumps(translator_input, ensure_ascii=False, indent=2))  # type: ignore

    # 检查是否是错误状态，如果是则使用备用模型重试一次
    if response.status == RunStatus.error:
        error_content = str(response.content) if response.content else ""
        if is_content_safety_error(error_content):
            logger.warning(f"主模型翻译失败（内容安全审核）：{error_content[:100]}，尝试使用备用模型重试...")
            translator = get_translator(fallback_model)
            response = await translator.arun(json.dumps(translator_input, ensure_ascii=False, indent=2))  # type: ignore

    if not isinstance(response.content, TranslationResponse):
        error_msg = "翻译步骤失败：代理返回了意外的响应类型。"
        logger.error(error_msg)
        # 失败时也返回原始 chunk，但可以考虑设置一个错误状态
        return StepOutput(content=chunk, success=False, error=error_msg)

    translated = response.content.translation
    # 处理 agent 偶尔返回的 \n -> 真正换行符
    translated = translated.replace("\\n", "\n")
    logger.info(f"接收到翻译文本: '{translated[:70]}...'")

    # 使用新的验证和修正函数
    is_valid, corrected_translation = _validate_and_fix_placeholders(chunk.original, translated)
    if not is_valid:
        error_msg = "翻译步骤失败：检测到占位符不匹配。"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)

    # 使用修正后的翻译文本
    chunk.status = TranslationStatus.TRANSLATED
    chunk.translated = corrected_translation
    # await asyncio.sleep(1)  # 确保异步上下文切换
    return StepOutput(content=chunk)


def check_step(outputs: List[StepOutput]) -> bool:
    if not outputs:
        return False

    output = outputs[-1]
    if output.success:
        return True
    return False


# Step 2: Proofread
async def proofread_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.previous_step_content  # type: ignore
    translated = getattr(chunk, "translated")

    if not translated or not isinstance(translated, str):
        error_msg = "校对步骤失败：没有从上一步收到有效的翻译文本。"
        logger.error(error_msg)
        # 将 chunk 和一个空的校对结果传递下去
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer_input = {"text_to_proofread": translated, "untranslatable_placeholders": _get_placeholders(translated)}

    # 重试逻辑
    max_attempts = 3
    proofreading_result = None
    used_fallback = False

    for attempt in range(max_attempts):
        # 根据是否使用过 fallback 决定使用哪个模型
        proofer = get_proofer(fallback_model) if used_fallback else get_proofer()

        try:
            response = await proofer.arun(json.dumps(proofer_input, ensure_ascii=False, indent=2))  # type: ignore

            if isinstance(response.content, ProofreadingResult):
                proofreading_result = response.content
                break

            # 检查是否是错误状态
            if response.status == RunStatus.error:
                error_content = str(response.content) if response.content else ""
                if not used_fallback and is_content_safety_error(error_content):
                    logger.warning(f"主模型校对失败（内容安全审核）：{error_content[:100]}，尝试使用备用模型重试...")
                    used_fallback = True
                    continue

            logger.warning(f"校对步骤失败：代理返回了意外的响应类型 (attempt {attempt + 1}/{max_attempts})")
        except Exception as e:
            error_str = str(e)
            # 检查是否是内容安全审核错误
            if not used_fallback and is_content_safety_error(error_str):
                logger.warning("主模型校对异常（内容安全审核），尝试使用备用模型重试...")
                used_fallback = True
                continue
            logger.error(f"校对步骤异常 (attempt {attempt + 1}/{max_attempts}): {error_str}")

        # 如果不是最后一次尝试，记录并继续重试
        if attempt < max_attempts - 1:
            logger.info("将在下次尝试中重试校对步骤...")

    # 如果所有尝试都失败，使用空结果并标记失败
    if proofreading_result is None:
        error_msg = f"校对步骤失败：经过 {max_attempts} 次尝试后仍未成功。"
        logger.error(error_msg)
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    # await asyncio.sleep(1)  # 确保异步上下文切换
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

    final_text = final_text.replace("您", "你").replace("。。", "。").replace("大型语言模型", "大语言模型")
    final_text = final_text.replace("，，", "，")

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
            Loop(
                name="Translate Loop",
                steps=[Step(name="translate", executor=translate_step)],
                end_condition=check_step,
                max_iterations=3,
            ),
            Loop(
                name="Proofread Loop",
                steps=[Step(name="proofread", executor=proofread_step)],
                end_condition=check_step,
                max_iterations=3,
            ),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
