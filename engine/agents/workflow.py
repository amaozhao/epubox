import json
import re
from typing import Dict, TypedDict

from agno.run import RunStatus
from agno.workflow import Step, StepInput, StepOutput, Workflow
from bs4 import BeautifulSoup
from bs4.element import NavigableString

from engine.agents.verifier import find_untranslated_english_texts, validate_translated_html
from engine.core.logger import engine_logger as logger
from engine.core.markup import get_markup_parser
from engine.schemas import Chunk, TranslationStatus

from .fallback_runtime import run_fallback_agent
from .models import fallback_model
from .proofer import get_proofer
from .schemas import ProofreadingResult, TranslationResponse
from .translator import get_translator


class ProofreadStepContent(TypedDict):
    chunk: Chunk
    proofreading_result: ProofreadingResult


class ChunkStepOutput(StepOutput):
    content: Chunk


class ProofreadStepOutput(StepOutput):
    content: ProofreadStepContent


# 需要内容安全审核 fallback 的错误码
CONTENT_SAFETY_ERROR_CODES = {10014, 500, 400}
CONTENT_SAFETY_KEYWORDS = ["相关法律法规", "不予显示", "安全审核", "content policy", "safety policy"]

# 最大重试次数
MAX_TRANSLATION_RETRIES = 3
SECONDARY_PLACEHOLDER_PATTERN = re.compile(r"\[(?:PRE|CODE|STYLE):\d+\]")
SECONDARY_PLACEHOLDER_LABEL_PATTERNS = {
    "PRE": re.compile(r"\[PRE:\d+\]"),
    "CODE": re.compile(r"\[CODE:\d+\]"),
    "STYLE": re.compile(r"\[STYLE:\d+\]"),
}
NAV_MARKER_PATTERN = re.compile(r"\[NAVTXT:\d+\]")
TEXT_MARKER_PATTERN = re.compile(r"\[TEXT:\d+\]")
FROZEN_TAG_PATTERN = re.compile(r"\[TAG:\d+\]")
FROZEN_TRANSLATION_TAGS = {"img", "br", "hr", "meta", "link"}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
MODEL_FORMAT_NEWLINE_ESCAPE_RE = re.compile(
    r"(?:(?<=>)\\n|\\n(?=\s*(?:\[(?:TEXT|NAVTXT):\d+\]|</?[A-Za-z][A-Za-z0-9:_-]*\b|<!--)))"
)
STRUCTURE_ERROR_KEYWORDS = (
    "标签属性不一致",
    "子标签数量不一致",
    "HTML标签结构错误",
    "冻结标签占位符不一致",
)
TEXT_NODE_FALLBACK_UNIT_LIMIT = 8
TEXT_NODE_FALLBACK_RETRIES = 3
VALIDATION_ERROR_HISTORY_LIMIT = 4
DIRECT_TEXT_NODE_INLINE_TAG_THRESHOLD = 6
DIRECT_TEXT_NODE_TOTAL_TAG_THRESHOLD = 12
DIRECT_TEXT_NODE_TEXT_NODE_THRESHOLD = 8
DIRECT_TEXT_NODE_PLACEHOLDER_RISK_THRESHOLD = 1
DIRECT_TEXT_NODE_MATH_TAG_THRESHOLD = 4
HIGH_RISK_INLINE_TAGS = {
    "a",
    "b",
    "code",
    "em",
    "i",
    "kbd",
    "q",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
    "var",
}
MATHY_INLINE_TAGS = {"sub", "sup"}


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


def _filter_invalid_corrections(corrections: dict[str, str]) -> tuple[dict[str, str], int]:
    """丢弃涉及 PRE/CODE/STYLE 占位符的校对建议。"""
    valid: dict[str, str] = {}
    rejected = 0

    for original, corrected in corrections.items():
        # 含占位符的短语是受保护片段，禁止校对阶段重写，避免重排风险。
        if SECONDARY_PLACEHOLDER_PATTERN.search(original) or SECONDARY_PLACEHOLDER_PATTERN.search(corrected):
            rejected += 1
            continue

        original_matches = SECONDARY_PLACEHOLDER_PATTERN.findall(original)
        corrected_matches = SECONDARY_PLACEHOLDER_PATTERN.findall(corrected)
        if original_matches != corrected_matches:
            rejected += 1
            continue
        valid[original] = corrected

    return valid, rejected


def _extract_nav_segments(text: str) -> list[tuple[str, str]]:
    matches = list(NAV_MARKER_PATTERN.finditer(text))
    segments: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        marker = match.group(0)
        payload = text[start:end].strip()
        segments.append((marker, payload))
    return segments


def _extract_text_segments(text: str) -> list[tuple[str, str]]:
    matches = list(TEXT_MARKER_PATTERN.finditer(text))
    segments: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        marker = match.group(0)
        payload = text[start:end].rstrip("\n")
        segments.append((marker, payload))
    return segments


def _normalize_text_marker_lines(original: str, translated: str) -> str:
    """按行修复缺失的 TEXT marker，仅在每行顺序仍与原始输入对齐时生效。"""
    original_lines = [line for line in original.splitlines() if line.strip()]
    translated_lines = [line for line in translated.splitlines() if line.strip()]
    if not original_lines or len(original_lines) != len(translated_lines):
        return translated

    normalized_lines: list[str] = []
    for index, (original_line, translated_line) in enumerate(zip(original_lines, translated_lines)):
        match = TEXT_MARKER_PATTERN.match(original_line)
        if match is None:
            return translated
        expected_marker = match.group(0)

        translated_match = TEXT_MARKER_PATTERN.match(translated_line)
        if translated_match is None:
            normalized_lines.append(f"{expected_marker}{translated_line}")
            continue
        normalized_lines.append(f"{expected_marker}{translated_line[translated_match.end() :]}")

    return "\n".join(normalized_lines)


def _normalize_missing_leading_text_marker(original: str, translated: str) -> str:
    """兼容历史逻辑：先尝试按行修复，再处理首个 marker 被吞掉的旧模式。"""
    translated = _normalize_text_marker_lines(original, translated)

    original_markers = [marker for marker, _ in _extract_text_segments(original)]
    translated_markers = [marker for marker, _ in _extract_text_segments(translated)]
    if not original_markers or translated_markers == original_markers:
        return translated
    if len(translated_markers) + 1 != len(original_markers):
        return translated
    if translated_markers != original_markers[1:]:
        return translated

    first_match = TEXT_MARKER_PATTERN.search(translated)
    if first_match is None:
        return translated

    prefix = translated[: first_match.start()]
    if not prefix.strip():
        return translated

    return f"{original_markers[0]}{translated}"


def _visible_text_for_language_check(text: str) -> str:
    text = SECONDARY_PLACEHOLDER_PATTERN.sub(" ", text)
    text = NAV_MARKER_PATTERN.sub(" ", text)
    text = TEXT_MARKER_PATTERN.sub(" ", text)
    return BeautifulSoup(text, get_markup_parser(text)).get_text(" ", strip=True)


def _looks_like_already_simplified_chinese(text: str) -> bool:
    visible_text = _visible_text_for_language_check(text)
    if not visible_text:
        return False

    cjk_count = sum(1 for ch in visible_text if "\u4e00" <= ch <= "\u9fff")
    latin_count = sum(1 for ch in visible_text if ("a" <= ch.lower() <= "z"))
    if cjk_count < 2:
        return False
    if latin_count == 0:
        return True
    return cjk_count >= max(4, latin_count * 0.5)


def _is_structure_validation_error(error_msg: str | None) -> bool:
    if not error_msg:
        return False
    return any(keyword in error_msg for keyword in STRUCTURE_ERROR_KEYWORDS)


def _collect_translatable_text_nodes(html: str) -> tuple[BeautifulSoup, list[tuple[NavigableString, str, str]]]:
    soup = BeautifulSoup(html, get_markup_parser(html))
    nodes: list[tuple[NavigableString, str, str]] = []

    for text_node in list(soup.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue

        parent = text_node.parent
        parent_name = getattr(parent, "name", None)
        if not parent_name or parent_name == "[document]":
            continue
        if str(parent_name).lower() in {"script", "style"}:
            continue

        text = str(text_node)
        if not text.strip():
            continue

        clean_text = SECONDARY_PLACEHOLDER_PATTERN.sub("", text)
        if not clean_text.strip():
            continue

        marker = f"[TEXT:{len(nodes)}]"
        nodes.append((text_node, marker, text))

    return soup, nodes


def _should_translate_chunk_via_text_nodes_directly(html: str) -> bool:
    """Direct text-node translation when inline markup density makes HTML regeneration fragile."""
    soup, text_nodes = _collect_translatable_text_nodes(html)
    tags = list(soup.find_all(True))
    inline_tag_count = sum(1 for tag in tags if str(tag.name).lower() in HIGH_RISK_INLINE_TAGS)
    math_tag_count = sum(1 for tag in tags if str(tag.name).lower() in MATHY_INLINE_TAGS)
    placeholder_count = len(SECONDARY_PLACEHOLDER_PATTERN.findall(html))

    if placeholder_count >= DIRECT_TEXT_NODE_PLACEHOLDER_RISK_THRESHOLD and (
        inline_tag_count >= 4 or math_tag_count >= DIRECT_TEXT_NODE_MATH_TAG_THRESHOLD
    ):
        return True

    if len(text_nodes) < DIRECT_TEXT_NODE_TEXT_NODE_THRESHOLD:
        return False

    return (
        inline_tag_count >= DIRECT_TEXT_NODE_INLINE_TAG_THRESHOLD or len(tags) >= DIRECT_TEXT_NODE_TOTAL_TAG_THRESHOLD
    )


def _validate_text_node_translation(original: str, translated: str) -> tuple[bool, str]:
    translated = _normalize_missing_leading_text_marker(original, translated)
    original_segments = _extract_text_segments(original)
    translated_segments = _extract_text_segments(translated)

    original_markers = [marker for marker, _ in original_segments]
    translated_markers = [marker for marker, _ in translated_segments]
    if translated_markers != original_markers:
        return False, f"TEXT 标记不一致: 原始 {original_markers}, 翻译 {translated_markers}"

    for marker, payload in translated_segments:
        if not payload.strip():
            return False, f"TEXT 标记 {marker} 译文为空"

    translated_payloads = {marker: payload for marker, payload in translated_segments}
    for marker, original_payload in original_segments:
        translated_payload = translated_payloads.get(marker, "")
        for label, pattern in SECONDARY_PLACEHOLDER_LABEL_PATTERNS.items():
            original_placeholders = pattern.findall(original_payload)
            translated_placeholders = pattern.findall(translated_payload)
            if original_placeholders != translated_placeholders:
                return (
                    False,
                    f"TEXT 标记 {marker} 内 {label} 占位符不一致: 原始 {original_placeholders}, 翻译 {translated_placeholders}",
                )

    return True, ""


def _append_error_history(history: list[str], error_msg: str | None) -> list[str]:
    if not error_msg:
        return history
    if history and history[-1] == error_msg:
        return history

    updated = [*history, error_msg]
    if len(updated) > VALIDATION_ERROR_HISTORY_LIMIT:
        updated = updated[-VALIDATION_ERROR_HISTORY_LIMIT:]
    return updated


def _build_validation_feedback(history: list[str]) -> str | None:
    if not history:
        return None
    if len(history) == 1:
        return history[0]

    bullets = "\n".join(f"- {item}" for item in history)
    return f"修复以下历史错误，优先解决最后一条，同时保持已正确的结构、标签和标记不变：\n{bullets}"


async def _translate_with_text_node_fallback(
    original: str,
    glossary: Dict[str, str] | None = None,
    error_history: list[str] | None = None,
) -> tuple[str | None, str | None]:
    soup, text_nodes = _collect_translatable_text_nodes(original)
    if not text_nodes:
        return original, None

    for start in range(0, len(text_nodes), TEXT_NODE_FALLBACK_UNIT_LIMIT):
        batch = text_nodes[start : start + TEXT_NODE_FALLBACK_UNIT_LIMIT]
        batch_with_local_markers = [
            (text_node, f"[TEXT:{index}]", text) for index, (text_node, _, text) in enumerate(batch)
        ]
        marked_text = "\n".join(f"{marker}{text}" for _, marker, text in batch_with_local_markers)
        batch_error_history = list(error_history or [])
        batch_previous_translation = None
        batch_error_msg = None

        for _ in range(TEXT_NODE_FALLBACK_RETRIES):
            translated = await _call_translator(
                marked_text,
                glossary,
                batch_previous_translation,
                _build_validation_feedback(batch_error_history),
                mode="text_node",
            )
            translated = _normalize_missing_leading_text_marker(marked_text, translated)

            is_valid, validation_error = _validate_text_node_translation(marked_text, translated)
            if is_valid:
                translated_segments = _extract_text_segments(translated)
                for (text_node, expected_marker, _), (actual_marker, payload) in zip(
                    batch_with_local_markers, translated_segments
                ):
                    if actual_marker != expected_marker:
                        batch_error_msg = f"TEXT 标记不一致: 期望 {expected_marker}, 实际 {actual_marker}"
                        batch_error_history = _append_error_history(batch_error_history, batch_error_msg)
                        batch_previous_translation = translated
                        break
                    text_node.replace_with(payload)
                else:
                    batch_error_msg = None
                    break
            else:
                batch_error_msg = validation_error
                batch_error_history = _append_error_history(batch_error_history, validation_error)
                batch_previous_translation = translated

        if batch_error_msg:
            return None, batch_error_msg

    return str(soup), None


def _apply_corrections_to_text_nodes(html: str, corrections: dict[str, str]) -> tuple[str, int, int]:
    """只在文本节点中应用校对建议，避免误改 HTML 属性或标签结构。"""
    soup = BeautifulSoup(html, get_markup_parser(html))
    replacement_count = 0
    matched_corrections: set[str] = set()

    for text_node in list(soup.find_all(string=True)):
        if not isinstance(text_node, NavigableString):
            continue

        updated = str(text_node)
        local_count = 0
        for original, corrected in corrections.items():
            occurrences = updated.count(original)
            if occurrences:
                updated = updated.replace(original, corrected)
                local_count += occurrences
                matched_corrections.add(original)

        if local_count:
            text_node.replace_with(updated)
            replacement_count += local_count

    return str(soup), replacement_count, len(matched_corrections)


def _freeze_translation_tags(html: str) -> tuple[str, list[tuple[str, str]]]:
    """将高风险空标签整体替换为占位符，避免模型破坏其属性或边界。"""
    soup = BeautifulSoup(html, get_markup_parser(html))
    replacements: list[tuple[str, str]] = []

    for tag in list(soup.find_all(FROZEN_TRANSLATION_TAGS)):
        placeholder = f"[TAG:{len(replacements)}]"
        replacements.append((placeholder, str(tag)))
        tag.replace_with(placeholder)

    return str(soup), replacements


def _restore_translation_tags(html: str, replacements: list[tuple[str, str]]) -> tuple[str, str | None]:
    restored = html
    translated_placeholders = FROZEN_TAG_PATTERN.findall(html)
    expected_placeholders = [placeholder for placeholder, _ in replacements]
    if translated_placeholders != expected_placeholders:
        return restored, f"冻结标签占位符不一致: 原始 {expected_placeholders}, 翻译 {translated_placeholders}"

    for placeholder, original in replacements:
        restored = restored.replace(placeholder, original)
    return restored, None


def _validate_nav_translation(original: str, translated: str) -> tuple[bool, str]:
    original_segments = _extract_nav_segments(original)
    translated_segments = _extract_nav_segments(translated)

    original_markers = [marker for marker, _ in original_segments]
    translated_markers = [marker for marker, _ in translated_segments]
    if translated_markers != original_markers:
        return False, f"NAV 标记不一致: 原始 {original_markers}, 翻译 {translated_markers}"

    for marker, payload in translated_segments:
        if not payload:
            return False, f"NAV 标记 {marker} 译文为空"
        untranslated_hits = find_untranslated_english_texts(payload)
        if untranslated_hits:
            return False, f"NAV 标记 {marker} 疑似残留未翻译英文: {untranslated_hits[0][:160]}"

    return True, ""


def _extract_translation_from_raw_content(raw_content: str) -> str | None:
    cleaned = raw_content.strip()
    if not cleaned:
        return None

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(cleaned)
    except json.JSONDecodeError:
        return cleaned

    if isinstance(parsed, TranslationResponse):
        return parsed.translation
    if isinstance(parsed, dict):
        translation = parsed.get("translation")
        if isinstance(translation, str):
            return translation
    if isinstance(parsed, str):
        return parsed
    return cleaned


def _sanitize_model_text(text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text)
    cleaned = "".join(ch for ch in cleaned if ch in ("\n", "\r", "\t") or ord(ch) >= 32)
    return MODEL_FORMAT_NEWLINE_ESCAPE_RE.sub("\n", cleaned)


async def _call_translator(
    text: str,
    glossary: Dict[str, str] | None = None,
    previous_translation: str | None = None,
    error_msg: str | None = None,
    mode: str = "html",
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
        translator = get_translator(mode=mode)
        payload = json.dumps(translator_input, ensure_ascii=False, indent=2)
        response = await translator.arun(payload)

        raw_content = response.content
        if response.status == RunStatus.error:
            error_content = str(raw_content) if raw_content else ""
            if is_content_safety_error(error_content):
                raise ValueError(f"内容安全审核失败: {error_content[:100]}")
            raise RuntimeError(error_content or "翻译模型返回错误状态")
        if isinstance(raw_content, TranslationResponse):
            return _sanitize_model_text(raw_content.translation)
        if isinstance(raw_content, str):
            parsed_translation = _extract_translation_from_raw_content(raw_content)
            if isinstance(parsed_translation, str) and parsed_translation.strip():
                return _sanitize_model_text(parsed_translation)
        raise ValueError(f"翻译响应格式错误: {type(raw_content)}")
    except Exception as e:
        logger.error(f"翻译模型调用异常: {type(e).__name__}: {e}")
        raise


async def _translate_with_fallback(chunk: Chunk, glossary: Dict[str, str] | None = None) -> Chunk:
    """翻译并用 validate_translated_html 验证 HTML 结构，失败则标记待手动处理"""
    original = chunk.original
    protected_original = original
    frozen_tag_replacements: list[tuple[str, str]] = []
    if chunk.chunk_mode != "nav_text":
        protected_original, frozen_tag_replacements = _freeze_translation_tags(original)
    last_error_msg = None
    last_translation = None
    error_history: list[str] = []
    prefer_text_node_directly = chunk.chunk_mode != "nav_text" and _should_translate_chunk_via_text_nodes_directly(
        original
    )

    for attempt in range(MAX_TRANSLATION_RETRIES):
        translated: str | None = None
        is_valid = False
        error_msg = "翻译未执行"
        try:
            use_text_node_fallback = prefer_text_node_directly or (
                chunk.chunk_mode != "nav_text"
                and attempt == MAX_TRANSLATION_RETRIES - 1
                and _is_structure_validation_error(last_error_msg)
            )
            if use_text_node_fallback:
                if prefer_text_node_directly and attempt == 0:
                    logger.info("检测到高风险复杂 chunk，直接执行 text-node translate 调用")
                else:
                    logger.info("开始执行 text-node fallback translate 调用")
                translated, text_node_error = await _translate_with_text_node_fallback(
                    original,
                    glossary,
                    error_history,
                )
                if text_node_error:
                    is_valid, error_msg = False, text_node_error
                else:
                    if translated is not None:
                        is_valid, error_msg = validate_translated_html(original, translated)
                    else:
                        is_valid, error_msg = False, "Translation failed: translated is None"
            else:
                translated = await _call_translator(
                    protected_original,
                    glossary,
                    last_translation,
                    _build_validation_feedback(error_history),
                    mode="nav_text" if chunk.chunk_mode == "nav_text" else "html",
                )
        except Exception as e:
            error_str = str(e)
            logger.warning(f"翻译重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 异常: {e}")
            last_error_msg = error_str
            error_history = _append_error_history(error_history, error_str)
            continue

        if not use_text_node_fallback:
            if chunk.chunk_mode == "nav_text":
                if translated is not None:
                    is_valid, error_msg = _validate_nav_translation(original, translated)
                else:
                    is_valid, error_msg = False, "translated is None"
            else:
                if translated is not None:
                    translated, tag_restore_error = _restore_translation_tags(translated, frozen_tag_replacements)
                    if tag_restore_error:
                        is_valid, error_msg = False, tag_restore_error
                    else:
                        if translated is not None:
                            is_valid, error_msg = validate_translated_html(original, translated)
                        else:
                            is_valid, error_msg = False, "translated is None"
                else:
                    is_valid, error_msg = False, "translated is None"
        last_translation = translated
        if is_valid and translated is not None:
            chunk.translated = translated
            chunk.status = TranslationStatus.TRANSLATED
            if chunk.chunk_mode != "nav_text" and error_msg == "accepted_as_is":
                chunk.status = TranslationStatus.ACCEPTED_AS_IS
            return chunk
        if is_valid:
            error_msg = "translated is None"

        logger.warning(f"翻译重试 {attempt + 1}/{MAX_TRANSLATION_RETRIES} 失败: {error_msg}")
        last_error_msg = error_msg
        error_history = _append_error_history(error_history, error_msg)

    # 所有重试都失败 → 标记为 TRANSLATION_FAILED，保留原文保结构
    logger.warning(f"Chunk '{chunk.name}': 翻译重试全部失败，标记为 TRANSLATION_FAILED")
    chunk.translated = ""
    chunk.status = TranslationStatus.TRANSLATION_FAILED
    return chunk


# Step 1: Translate
async def translate_step(step_input: StepInput) -> ChunkStepOutput:
    chunk: Chunk = step_input.input  # type: ignore
    additional_data = step_input.additional_data or {}
    glossary = additional_data.get("glossary", {})

    if chunk.status == TranslationStatus.TRANSLATED and chunk.translated:
        return ChunkStepOutput(content=chunk)

    if not chunk.original or not chunk.original.strip():
        logger.info(f"Chunk '{chunk.name}' 无可翻译内容，直接返回原文")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.TRANSLATED
        return ChunkStepOutput(content=chunk)

    untranslated_hits = find_untranslated_english_texts(chunk.original)
    if _looks_like_already_simplified_chinese(chunk.original) and not untranslated_hits:
        logger.info(f"Chunk '{chunk.name}' 检测到原文已是目标语言，直接接受原文。")
        chunk.translated = chunk.original
        chunk.status = TranslationStatus.ACCEPTED_AS_IS
        return ChunkStepOutput(content=chunk)
    if untranslated_hits:
        logger.info(f"Chunk '{chunk.name}' 检测到疑似残留未翻译英文，将继续调用翻译器。")

    try:
        chunk = await _translate_with_fallback(chunk, glossary)
        return ChunkStepOutput(content=chunk)
    except Exception as e:
        error_msg = f"翻译步骤失败：{e}"
        logger.error(error_msg)
        return ChunkStepOutput(content=chunk, success=False, error=error_msg)


# Step 2: Proofread
async def proofread_step(step_input: StepInput) -> ProofreadStepOutput:
    chunk: Chunk = step_input.previous_step_content  # type: ignore
    translated = getattr(chunk, "translated")

    if chunk.chunk_mode == "nav_text":
        return ProofreadStepOutput(content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})})

    # 翻译失败或接受原文，跳过校对
    if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.ACCEPTED_AS_IS):
        logger.info(f"Chunk '{chunk.name}' 无需校对，跳过校对步骤")
        return ProofreadStepOutput(content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})})

    if not translated or not isinstance(translated, str):
        error_msg = "校对步骤失败：没有从上一步收到有效的翻译文本。"
        logger.error(error_msg)
        return ProofreadStepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    proofer_input = {"text_to_proofread": translated}

    max_attempts = 3
    proofreading_result = None
    used_fallback = False

    for attempt in range(max_attempts):
        use_fallback_this_attempt = used_fallback or attempt == max_attempts - 1
        proofer = get_proofer(fallback_model) if use_fallback_this_attempt else get_proofer()
        try:
            payload = json.dumps(proofer_input, ensure_ascii=False, indent=2)
            if use_fallback_this_attempt:
                response = await run_fallback_agent("proofread", proofer, payload)
            else:
                response = await proofer.arun(payload)
            if isinstance(response.content, ProofreadingResult):
                proofreading_result = response.content
                break
            if response.status == RunStatus.error:
                error_content = str(response.content) if response.content else ""
                if not use_fallback_this_attempt and not used_fallback and is_content_safety_error(error_content):
                    logger.warning("主模型校对失败（内容安全审核），尝试使用备用模型...")
                    used_fallback = True
                    continue
            logger.warning(f"校对步骤失败：代理返回了意外的响应类型 (attempt {attempt + 1}/{max_attempts})")
        except Exception as e:
            if not use_fallback_this_attempt and not used_fallback and is_content_safety_error(str(e)):
                logger.warning("主模型校对异常（内容安全审核），尝试使用备用模型...")
                used_fallback = True
                continue
            logger.error(f"校对步骤异常 (attempt {attempt + 1}/{max_attempts}): {e}")

        if attempt < max_attempts - 1:
            logger.info("将在下次尝试中重试校对步骤...")

    if proofreading_result is None:
        error_msg = f"校对步骤失败：经过 {max_attempts} 次尝试后仍未成功。"
        logger.error(error_msg)
        return ProofreadStepOutput(
            content={"chunk": chunk, "proofreading_result": ProofreadingResult(corrections={})},
            success=False,
            error=error_msg,
        )

    return ProofreadStepOutput(content={"chunk": chunk, "proofreading_result": proofreading_result})


# Step 3: Apply Corrections
def apply_corrections_step(step_input: StepInput) -> ChunkStepOutput:
    step_data: dict = step_input.previous_step_content  # type: ignore
    chunk: Chunk = step_data["chunk"]
    proofreading_result: ProofreadingResult = step_data["proofreading_result"]
    translated_text = chunk.translated

    # 翻译失败或接受原文，跳过应用校对建议
    if chunk.status in (TranslationStatus.TRANSLATION_FAILED, TranslationStatus.ACCEPTED_AS_IS):
        logger.info(f"Chunk '{chunk.name}' 无需应用校对建议，直接返回")
        return ChunkStepOutput(content=chunk)

    if not translated_text or not isinstance(translated_text, str):
        error_msg = "应用校对建议步骤失败：缺少翻译文本。"
        logger.error(error_msg)
        return ChunkStepOutput(content=chunk, success=False, error=error_msg)

    if chunk.chunk_mode == "nav_text":
        chunk.status = TranslationStatus.COMPLETED
        return ChunkStepOutput(content=chunk)

    raw_corrections = proofreading_result.corrections
    total_corrections = len(raw_corrections)
    logger.info(f"校对器发现 {total_corrections} 个潜在的校对建议。")
    corrections, rejected_corrections = _filter_invalid_corrections(raw_corrections)
    eligible_corrections = len(corrections)
    if rejected_corrections:
        logger.warning(f"Chunk '{chunk.name}' 丢弃了 {rejected_corrections} 个破坏占位符完整性的校对建议。")

    final_text = translated_text
    replacement_count = 0
    matched_correction_count = 0
    if corrections:
        final_text, replacement_count, matched_correction_count = _apply_corrections_to_text_nodes(
            final_text, corrections
        )
    unmatched_corrections = eligible_corrections - matched_correction_count
    logger.info(
        "校对建议统计："
        f"总计 {total_corrections}，"
        f"过滤 {rejected_corrections}，"
        f"进入替换 {eligible_corrections}，"
        f"文本命中 {matched_correction_count}，"
        f"未命中 {unmatched_corrections}，"
        f"实际替换 {replacement_count} 处。"
    )

    # 后处理：统一词汇和标点
    final_text = final_text.replace("您", "你").replace("大型语言模型", "大语言模型")
    final_text = final_text.replace("。。", "。").replace("，，", "，")

    is_valid, error_msg = validate_translated_html(chunk.original, final_text)
    if not is_valid:
        logger.warning(
            f"Chunk '{chunk.name}' 校对后校验失败，回退到校对前译文: {error_msg}；"
            f"已撤销 {replacement_count} 处替换（命中 {matched_correction_count} 条建议）。"
        )
        final_text = translated_text

    chunk.translated = final_text
    chunk.status = TranslationStatus.COMPLETED

    return ChunkStepOutput(content=chunk)


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
