import json
import re
from typing import Dict, List

from agno.run import RunStatus
from agno.workflow import Loop, Step, StepInput, StepOutput, Workflow

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .aligner import _align, _remove_placeholders
from .models import fallback_model
from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator
from .validator import validate_placeholders

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


def _get_placeholder_indices(text: str) -> List[int]:
    """提取文本中所有占位符的索引"""
    matches = re.findall(r'\[id(\d+)\]', text)
    return [int(m) for m in matches]


def _get_placeholders_from_indices(indices: List[int]) -> List[str]:
    """将索引列表转换为占位符字符串列表"""
    return [f"[id{i}]" for i in indices]


def _has_translatable_content(text: str) -> bool:
    """检查文本是否包含可翻译内容（排除纯占位符）"""
    clean = re.sub(r'\[id\d+\]', '', text)
    return bool(clean.strip())


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
    placeholder_mgr,
    glossary: Dict[str, str] = None,
    previous_translation: str | None = None,
    error_msg: str | None = None,
    use_fallback: bool = False,
) -> str:
    """调用翻译模型

    Args:
        text: 待翻译文本
        placeholder_mgr: 占位符管理器
        glossary: 术语表
        previous_translation: 上一次翻译失败的结果（用于重试时参考）
        error_msg: 上一次翻译失败的具体错误信息
    """
    # 提取文本中的占位符索引
    text_placeholder_indices = _get_placeholder_indices(text)
    # 构建验证用的 tag_map（使用全局索引作为 key，与 chunk.original 一致）
    validation_tag_map = {f"[id{i}]": placeholder_mgr.tag_map.get(f"[id{i}]", "") for i in text_placeholder_indices}
    # 过滤出文本中出现的术语
    filtered_glossary = filter_glossary_terms(text, glossary) if glossary else {}
    translator_input = {
        "text_to_translate": text,
        "placeholder_count": len(validation_tag_map),
        "untranslatable_placeholders": list(validation_tag_map.keys()),
        "glossaries": filtered_glossary,
    }
    # 如果有上一次失败的翻译结果，加入输入中让模型参考
    if previous_translation:
        translator_input["previous_translation"] = previous_translation
    # 如果有错误信息，加入输入中帮助模型理解问题
    if error_msg:
        translator_input["validation_error"] = error_msg

    try:
        translator = get_translator(fallback_model) if use_fallback else get_translator()
        response = await translator.arun(json.dumps(translator_input, ensure_ascii=False, indent=2))

        raw_content = response.content
        # 检查是否是内容安全审核错误
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


async def _translate_with_fallback(chunk: Chunk, placeholder_mgr, glossary: Dict[str, str] = None) -> Chunk:
    """三层防御翻译"""
    original = chunk.original
    last_translated = None  # 保存上一次翻译失败的结果用于重试
    last_error_msg = None  # 保存错误信息供模型参考
    used_fallback = False  # 是否已使用备用模型
    # 聚合所有缺失的占位符 ID
    all_missing_ids: set[int] = set()

    # Phase 1: 正常翻译（最多重试max_retries次）
    for attempt in range(MAX_TRANSLATION_RETRIES):
        try:
            translated = await _call_translator(original, placeholder_mgr, glossary, last_translated, last_error_msg, use_fallback=used_fallback)
            translated = translated.replace("\\n", "\n")
        except Exception as e:
            error_str = str(e)
            # 内容安全审核错误：切换到备用模型重试
            if not used_fallback and is_content_safety_error(error_str):
                logger.warning(f"主模型翻译失败（内容安全审核），尝试使用备用模型...")
                used_fallback = True
                last_translated = None
                last_error_msg = None
                continue
            logger.warning(f"Phase 1 重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 异常: {e}")
            last_translated = None
            last_error_msg = None
            continue

        # 验证占位符 - 使用 chunk.global_indices 构建验证用的 tag_map
        # 注意：chunk.original 中的占位符是全局索引（如 [id23]），而非局部索引
        # 因此需要用全局索引来验证
        if hasattr(chunk, 'global_indices') and chunk.global_indices:
            validation_tag_map = {f"[id{i}]": placeholder_mgr.tag_map.get(f"[id{i}]", "") for i in chunk.global_indices}
        else:
            validation_tag_map = placeholder_mgr.tag_map
        is_valid, error_msg = validate_placeholders(translated, validation_tag_map)
        if is_valid:
            chunk.translated = translated
            chunk.status = TranslationStatus.TRANSLATED
            return chunk
        else:
            logger.warning(f"Phase 1 重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 失败: {error_msg}")
            # 聚合缺失的占位符 ID
            for m in re.findall(r'\[id(\d+)\]', error_msg):
                all_missing_ids.add(int(m))
            # 构建聚合错误消息
            if all_missing_ids:
                missing_str = ", ".join(sorted([f"[id{i}]" for i in sorted(all_missing_ids)]))
                retry_hint = f"（已缺失 {len(all_missing_ids)} 个占位符，请全部保留在输出中）"
                last_error_msg = f"{error_msg} {retry_hint}"
            else:
                last_error_msg = error_msg
            # 不要把包含错误占位符的翻译结果传给下一次重试，否则模型会"强化"错误
            last_translated = None

    # Phase 1 所有重试都失败 → 尝试 Phase 2: Token Alignment
    logger.warning("Phase 1 所有重试失败，尝试 Phase 2...")
    try:
        # Phase 2: 直接翻译纯净文本（不带占位符），然后重新插入占位符
        # 使用 chunk 自己的局部占位符，而不是全文档的占位符
        placeholders = [f"[id{i}]" for i in chunk.global_indices]
        clean_original = _remove_placeholders(original, placeholders)

        # 确保我们真的移除了占位符
        if re.search(r'\[id\d+\]', clean_original):
            logger.error("Phase 2: clean_original 仍然包含占位符！")
            raise ValueError("占位符移除失败")

        # 翻译纯净文本（不带占位符）
        # Phase 2: 需要告诉模型原文有哪些占位符，在翻译后要加回去
        clean_translated = await _call_translator(clean_original, placeholder_mgr, glossary, None, f"注意：原文有以下占位符必须保留在输出的对应位置：{placeholders}。翻译时将这些占位符视为不可见的标记，在翻译后的文本中保持它们的位置不变。")
        clean_translated = clean_translated.replace("\\n", "\n")

        # 如果翻译结果仍然包含占位符，说明模型没有正确处理
        if re.search(r'\[id\d+\]', clean_translated):
            logger.warning("Phase 2: 翻译结果仍然包含占位符，将清除后重新对齐")
            # 强制清除翻译结果中的所有占位符（不仅是 global 的）
            # 使用正则移除所有 [idN] 格式的占位符
            clean_translated = re.sub(r'\[id\d+\]', '', clean_translated)

        # 将占位符从原文对齐到译文（基于相对位置）
        aligned = _align(original, clean_translated, placeholders)
        # Phase 2 也使用 global_indices 构建验证 tag_map
        if hasattr(chunk, 'global_indices') and chunk.global_indices:
            validation_tag_map = {f"[id{i}]": placeholder_mgr.tag_map.get(f"[id{i}]", "") for i in chunk.global_indices}
        else:
            validation_tag_map = placeholder_mgr.tag_map
        is_valid, _ = validate_placeholders(aligned, validation_tag_map)
        if is_valid:
            chunk.translated = aligned
            chunk.status = TranslationStatus.TRANSLATED
            return chunk
    except Exception as e:
        logger.warning(f"Phase 2 异常: {e}")
        import traceback
        logger.warning(f"Phase 2 堆栈: {traceback.format_exc()}")

    # Phase 2 也失败 → 进入 Phase 3: 翻译失败，标记为失败状态等待手动处理
    # 注意：不使用原文覆盖 translated 字段，保留原文让后续可手动翻译
    logger.error(f"Chunk '{chunk.name}': Phase 1 和 Phase 2 都失败，标记为待手动翻译")
    chunk.status = TranslationStatus.UNTRANSLATED
    return chunk


# Step 1: Translate
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    additional_data = step_input.additional_data or {}
    placeholder_mgr = additional_data.get("placeholder_mgr")
    glossary = additional_data.get("glossary", {})

    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        return StepOutput(content=chunk)

    if not _has_translatable_content(chunk.original):
        logger.info(f"Chunk '{chunk.name}' 无可翻译内容，直接返回原文")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return StepOutput(content=chunk)

    if not placeholder_mgr:
        error_msg = "翻译步骤失败：缺少 placeholder_mgr"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)

    try:
        chunk = await _translate_with_fallback(chunk, placeholder_mgr, glossary)
        return StepOutput(content=chunk)
    except Exception as e:
        error_msg = f"翻译步骤失败：{e}"
        logger.error(error_msg)
        return StepOutput(content=chunk, success=False, error=error_msg)


def translate_check_step(outputs: List[StepOutput]) -> bool:
    """检查翻译步骤是否成功"""
    if not outputs:
        return False
    last_output = outputs[-1]
    chunk = last_output.content
    if isinstance(chunk, Chunk):
        if chunk.status == TranslationStatus.UNTRANSLATED:
            return True
        if chunk.status == TranslationStatus.TRANSLATED:
            return True
    return False


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
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer_input = {"text_to_proofread": translated, "untranslatable_placeholders": _get_placeholders_from_indices(_get_placeholder_indices(translated))}

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
            if not response.success:
                error_content = str(response.content) if response.content else ""
                if not used_fallback and is_content_safety_error(error_content):
                    logger.warning(f"主模型校对失败（内容安全审核），尝试使用备用模型...")
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
    """构建并返回翻译工作流（三层防御机制）"""
    return Workflow(
        name="TranslatorWorkflow",
        description="一个智能翻译工作流，带三层防御机制保护EPUB结构完整性",
        steps=[
            Loop(
                name="Translate Loop",
                steps=[Step(name="translate", executor=translate_step)],
                end_condition=translate_check_step,
                max_iterations=3,
            ),
            Step(name="proofread", executor=proofread_step),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
