import json
import re
from typing import Dict, Optional

from agno.run import RunStatus
from agno.workflow import Step, StepInput, StepOutput, Workflow

from engine.core.logger import engine_logger as logger
from engine.schemas import Chunk, TranslationStatus

from .models import fallback_model
from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator
from .validator import validate_html_pairing, validate_html_with_context

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


def _has_translatable_content(text: str) -> bool:
    """检查文本是否包含可翻译内容（排除 HTML 标签）"""
    # Remove HTML tags for content check
    clean = re.sub(r"<[^>]+>", "", text)
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
    glossary: Optional[Dict[str, str]] = None,
    previous_translation: str | None = None,
    error_msg: str | None = None,
    use_fallback: bool = False,
) -> str:
    """调用翻译模型

    Args:
        text: 待翻译文本（HTML 格式，标签需要保留）
        glossary: 术语表
        previous_translation: 上一次翻译失败的结果（用于重试时参考）
        error_msg: 上一次翻译失败的具体错误信息
    """
    # 过滤出文本中出现的术语
    filtered_glossary = filter_glossary_terms(text, glossary) if glossary else {}
    translator_input = {
        "text_to_translate": text,
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
        # 当 raw_content 是字符串时，可能是 Agno JSON 解析失败的情况
        if isinstance(raw_content, str):
            # 去掉可能的前缀文字（如 "Here is the translation:"）
            cleaned = raw_content.strip()
            # 尝试 JSON 解析
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and "translation" in parsed:
                    return parsed["translation"]
            except (json.JSONDecodeError, ValueError):
                pass
            # 如果 JSON 解析失败，检查字符串内容是否像 HTML
            if cleaned.startswith("<") and cleaned.endswith(">"):
                # 看起来像是直接的 HTML 翻译结果，直接使用
                logger.warning("Agno returned raw string, using as translation directly")
                return cleaned
            # 尝试直接提取 translation 字段
            match = re.search(r'"translation"\s*:\s*"([^"]*)"', cleaned, re.DOTALL)
            if match:
                translation = match.group(1)
                try:
                    translation = translation.encode().decode("unicode_escape")
                except (UnicodeDecodeError, ValueError) as e:
                    logger.warning(f"unicode_escape decode failed: {e}, using raw translation")
                return translation
            # 最后尝试：把整个字符串作为翻译结果返回
            logger.warning("Could not parse translation response, using raw content")
            return cleaned
        raise ValueError(f"翻译响应格式错误: {type(raw_content)}")
    except Exception as e:
        logger.error(f"翻译模型调用异常: {type(e).__name__}: {e}")
        raise


async def _translate_with_fallback(
    chunk: Chunk,
    glossary: Optional[Dict[str, str]] = None,
) -> Chunk:
    """翻译：翻译 HTML 并验证标签配对"""
    original = chunk.original
    last_error_msg = None
    last_translation = None
    used_fallback = False

    for attempt in range(MAX_TRANSLATION_RETRIES):
        try:
            translated = await _call_translator(
                original, glossary, last_translation, last_error_msg, use_fallback=used_fallback
            )
            translated = translated.replace("\\n", "\n")

            # 验证 HTML 标签配对
            # 第一次验证用简单错误，后续重试用详细上下文
            if attempt == 0:
                is_valid, error_msg = validate_html_pairing(original, translated)
            else:
                is_valid, error_msg = validate_html_with_context(original, translated)

            if is_valid:
                chunk.translated = translated
                chunk.status = TranslationStatus.TRANSLATED
                return chunk

            # 验证失败，将错误信息传给下一次重试
            logger.warning(f"Chunk '{chunk.name}' HTML 验证失败: {error_msg}，重试...")
            last_error_msg = error_msg
            last_translation = translated
            continue
        except Exception as e:
            error_str = str(e)
            if not used_fallback and is_content_safety_error(error_str):
                logger.warning("主模型翻译失败（内容安全审核），尝试使用备用模型...")
                used_fallback = True
                last_error_msg = None
                last_translation = None
                continue
            logger.warning(f"翻译重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 异常: {e}")
            last_error_msg = None
            last_translation = None
            continue

    # 翻译失败，使用原文代替
    logger.warning(f"Chunk '{chunk.name}': 翻译失败，使用原文代替")
    chunk.translated = original
    chunk.status = TranslationStatus.UNTRANSLATED
    return chunk


# Step 1: Translate
async def translate_step(step_input: StepInput) -> StepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    additional_data = step_input.additional_data or {}
    glossary = additional_data.get("glossary", {})

    # 只有 PENDING 状态才执行翻译
    if chunk.status != TranslationStatus.PENDING:
        logger.info(f"Chunk '{chunk.name}' status={chunk.status}，跳过翻译步骤")
        return StepOutput(content={"chunk": chunk, "validation_error": None})

    if not _has_translatable_content(chunk.original):
        logger.info(f"Chunk '{chunk.name}' 无可翻译内容，直接返回原文")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return StepOutput(content={"chunk": chunk, "validation_error": None})

    try:
        chunk = await _translate_with_fallback(chunk, glossary)
        return StepOutput(content={"chunk": chunk, "validation_error": None})
    except Exception as e:
        error_msg = f"翻译步骤失败：{e}"
        logger.error(error_msg)
        return StepOutput(content={"chunk": chunk, "validation_error": error_msg}, success=False, error=error_msg)


# Step 2: Validate
def validation_step(step_input: StepInput) -> StepOutput:
    """独立验证步骤：验证 HTML 标签配对"""
    step_data = step_input.previous_step_content

    # 处理不同格式的输入（兼容重试场景）
    if isinstance(step_data, dict):
        chunk = step_data.get("chunk")
        if chunk is None:
            error_msg = "验证步骤失败：无法从 previous_step_content 获取 chunk"
            logger.error(error_msg)
            return StepOutput(content={"chunk": None, "validation_error": error_msg}, success=False, error=error_msg)
    else:
        chunk = step_data

    if not isinstance(chunk, Chunk):
        error_msg = f"验证步骤失败：chunk 不是 Chunk 对象，而是 {type(chunk)}"
        logger.error(error_msg)
        return StepOutput(content={"chunk": chunk, "validation_error": error_msg}, success=False, error=error_msg)

    # 只有 TRANSLATED 状态才执行验证
    if chunk.status != TranslationStatus.TRANSLATED:
        logger.info(f"Chunk '{chunk.name}' status={chunk.status}，跳过验证步骤")
        return StepOutput(content={"chunk": chunk, "validation_error": None})

    if not chunk.translated:
        error_msg = "验证步骤失败：没有翻译文本可验证"
        logger.error(error_msg)
        return StepOutput(content={"chunk": chunk, "validation_error": error_msg}, success=False, error=error_msg)

    # 验证 HTML 标签配对
    is_valid, error_msg = validate_html_pairing(chunk.original, chunk.translated)
    if not is_valid:
        logger.warning(f"Chunk '{chunk.name}' HTML 验证失败: {error_msg}")
        chunk.status = TranslationStatus.UNTRANSLATED
        chunk.translated = ""
        return StepOutput(content={"chunk": chunk, "validation_error": error_msg})

    return StepOutput(content={"chunk": chunk, "validation_error": None})


# Step 3: Proofread
async def proofread_step(step_input: StepInput) -> StepOutput:
    step_data: dict = step_input.previous_step_content  # type: ignore
    chunk: Chunk = step_data["chunk"]

    # 只有 TRANSLATED 状态才执行校对
    if chunk.status != TranslationStatus.TRANSLATED:
        logger.info(f"Chunk '{chunk.name}' status={chunk.status}，跳过校对步骤")
        return StepOutput(content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})})

    translated = getattr(chunk, "translated")
    if not translated or not isinstance(translated, str):
        error_msg = "校对步骤失败：没有从上一步收到有效的翻译文本。"
        logger.error(error_msg)
        return StepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer_input = {
        "text_to_proofread": translated,
    }

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


# Step 4: Apply Corrections
def apply_corrections_step(step_input: StepInput) -> StepOutput:
    step_data: dict = step_input.previous_step_content  # type: ignore
    chunk: Chunk = step_data["chunk"]
    proofreading_result: ProofreadingResult = step_data["proofreading_result"]

    # 只有 TRANSLATED 状态才执行应用校对建议
    if chunk.status != TranslationStatus.TRANSLATED:
        logger.info(f"Chunk '{chunk.name}' status={chunk.status}，跳过应用校对建议步骤")
        return StepOutput(content=chunk)

    translated_text = chunk.translated
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
    """构建并返回翻译工作流（翻译→验证→校对→修正）"""
    return Workflow(
        name="TranslatorWorkflow",
        description="智能翻译工作流：直接翻译 HTML + 标签验证 + 校对提升质量",
        steps=[
            Step(name="translate", executor=translate_step),
            Step(name="validate", executor=validation_step),
            Step(name="proofread", executor=proofread_step),
            Step(name="apply_corrections", executor=apply_corrections_step),
        ],
    )
