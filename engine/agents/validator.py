import re
from typing import List, Tuple

from lxml import etree

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
    1. lxml 严格 XML 解析（能检测标签缺失、结构错误等）
    2. 栈验证（未闭合标签检测）

    Returns (is_valid, error_message)
    """
    if not translated:
        return False, "Translated content is empty"

    # 首先用 lxml 严格验证 - 这能检测出 <ol> 被删除导致 <li> 孤立等问题
    try:
        # 用 XML 解析器严格验证
        etree.fromstring(translated.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        error_msg = str(e)
        # 提取有用的错误信息
        if "Opening and ending tag mismatch" in error_msg:
            return False, f"XML结构错误: {error_msg}"
        if "unexpected end tag" in error_msg.lower():
            return False, f"XML结构错误: {error_msg}"
        return False, f"XML解析错误: {error_msg}"

    # 额外用栈验证器检查未闭合标签
    validator = HtmlValidator()
    translated_valid, translated_errors = validator.validate_chunk(translated, 0, "translated")

    if validator.stack:
        unclosed_tags = [tag for tag, _ in validator.stack]
        return False, f"Translated HTML has unclosed tags: {unclosed_tags}"

    return True, ""


def validate_html_with_context(original: str, translated: str) -> Tuple[bool, str]:
    """
    验证 HTML 标签配对，并提供详细上下文用于 LLM 修复。

    Returns:
        Tuple[bool, str]: (是否有效, 详细的错误上下文信息)
    """
    if not translated:
        return False, "Translated content is empty"

    # 首先用 lxml 严格验证 XML 结构
    xml_error = None
    try:
        etree.fromstring(translated.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        xml_error = str(e)
    except Exception as e:
        xml_error = str(e)

    # 用栈验证器检查
    validator = HtmlValidator()
    translated_valid, translated_errors = validator.validate_chunk(translated, 0, "translated")

    if not xml_error and translated_valid and not validator.stack:
        return True, ""

    # 构建详细的错误信息
    error_parts = []

    # 1. XML 解析错误
    if xml_error:
        error_parts.append(f"XML结构错误: {xml_error}")

    # 2. 栈验证器解析错误
    if translated_errors:
        error_parts.append("解析错误:")
        for err in translated_errors:
            if err.get("type") == "tag_mismatch":
                error_parts.append(
                    f"  - 标签不匹配: 期望 {err.get('expected')}, 实际 {err.get('actual')}"
                )
            elif err.get("type") == "unexpected_close":
                error_parts.append(f"  - 意外的闭合标签: {err.get('actual')}")

    # 3. 未闭合标签
    if validator.stack:
        error_parts.append(f"未闭合的标签: {[tag for tag, _ in validator.stack]}")

    # 4. 提供 HTML 片段对比
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
