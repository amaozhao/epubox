import re
from typing import List, Tuple

from engine.agents.html_validator import HtmlValidator


class ValidationError(Exception):
    """HTML validation error"""

    def __init__(self, message: str, errors: List[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or []


def validate_html_pairing(original: str, translated: str) -> Tuple[bool, str]:
    """
    Validate that HTML tags in translated text are properly paired.

    检查两个层面：
    1. 解析错误（标签不匹配、意外闭合等）
    2. 未闭合标签（单个 chunk 必须闭合）

    Returns (is_valid, error_message)
    """
    if not translated:
        return False, "Translated content is empty"

    validator = HtmlValidator()

    # Validate translated HTML structure using stack tracker
    translated_valid, translated_errors = validator.validate_chunk(translated, 0, "translated")
    if not translated_valid:
        error_details = []
        for err in translated_errors:
            if err.get("type") == "tag_mismatch":
                error_details.append(
                    f"标签不匹配: 期望 {err.get('expected')}, 实际 {err.get('actual')}"
                )
            elif err.get("type") == "unexpected_close":
                error_details.append(f"意外的闭合标签 {err.get('actual')}")
            else:
                error_details.append(str(err))
        return False, f"Translated HTML has structure issues: {'; '.join(error_details)}"

    # 检查未闭合标签（单个 chunk 必须完全闭合）
    if validator.stack:
        unclosed_tags = [tag for tag, _ in validator.stack]
        return False, f"Translated HTML has unclosed tags: {unclosed_tags} (chunk must be fully closed)"

    return True, ""


def validate_html_with_context(original: str, translated: str) -> Tuple[bool, str]:
    """
    验证 HTML 标签配对，并提供详细上下文用于 LLM 修复。

    Returns:
        Tuple[bool, str]: (是否有效, 详细的错误上下文信息)
    """
    if not translated:
        return False, "Translated content is empty"

    validator = HtmlValidator()
    translated_valid, translated_errors = validator.validate_chunk(translated, 0, "translated")

    if translated_valid and not validator.stack:
        return True, ""

    # 构建详细的错误信息
    error_parts = []

    # 1. 解析错误
    if translated_errors:
        error_parts.append("解析错误:")
        for err in translated_errors:
            if err.get("type") == "tag_mismatch":
                error_parts.append(
                    f"  - 标签不匹配: 期望 {err.get('expected')}, 实际 {err.get('actual')}"
                )
            elif err.get("type") == "unexpected_close":
                error_parts.append(f"  - 意外的闭合标签: {err.get('actual')}")

    # 2. 未闭合标签
    if validator.stack:
        error_parts.append(f"未闭合的标签: {[tag for tag, _ in validator.stack]}")

    # 3. 提供 HTML 片段对比
    error_parts.append("\n原文片段 (前后各50字符):")
    error_parts.append(f"  {repr(original[max(0, len(original)//2-50):len(original)//2+50])}")
    error_parts.append("\n翻译片段 (前后各50字符):")
    error_parts.append(f"  {repr(translated[max(0, len(translated)//2-50):len(translated)//2+50])}")

    return False, "\n".join(error_parts)


def validate_placeholders(text: str, tag_map: dict) -> Tuple[bool, str]:
    """
    Legacy function - always returns True since we no longer use placeholders.
    Kept for backward compatibility.
    """
    return True, ""
