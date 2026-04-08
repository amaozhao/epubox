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


def _count_unclosed_tags(html: str) -> dict:
    """统计未闭合标签的数量（按标签类型分组）"""
    validator = HtmlValidator()
    validator._parse_html(html, 0)
    counts = {}
    for tag, _ in validator.stack:
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def validate_html_pairing(original: str, translated: str) -> Tuple[bool, str]:
    """
    Validate that HTML tags in translated text are properly paired.

    注意：此验证用于单个 chunk 片段，chunk 可能是 HTML 的不完整部分
    （如缺少 </body></html>）。因此不使用 lxml 严格验证，只用 HtmlValidator
    检查 chunk 内部的标签配对错误。

    检查两个层面：
    1. 标签配对错误检测（unexpected_close, tag_mismatch）
    2. 内容完整性检查（检测 LLM 删减内容的问题）
    3. 未闭合标签数量对比（原文 vs 译文）

    Returns (is_valid, error_message)
    """
    if not translated:
        return False, "Translated content is empty"

    # 内容完整性检查：检测 LLM 删减内容的问题
    # 对比原文和译文的段落/行数差异
    # 使用 [\s>] 来匹配 <p> 或 <p class="..."> 等情况
    original_blocks = len(re.findall(r'<p[\s>][^>]*>', original)) + len(re.findall(r'<div[\s>][^>]*>', original))
    translated_blocks = len(re.findall(r'<p[\s>][^>]*>', translated)) + len(re.findall(r'<div[\s>][^>]*>', translated))

    # 如果译文段落数明显少于原文（差距超过 2 个），可能是 LLM 删减了内容
    if original_blocks > 0 and translated_blocks < original_blocks - 2:
        return False, f"内容被删减: 原文有 {original_blocks} 个段落块，译文只有 {translated_blocks} 个"

    # 用 HtmlValidator 检查 chunk 内部的标签配对错误
    # 注意：不检查 validator.stack，因为跨 chunk 的标签（如 <body> 在 Chunk 0 打开，
    # </body> 在 Chunk N 闭合）是正常的，不应该算作错误
    # 真正能判断 LLM 翻译错误的是 unexpected_close（意外闭合）和 tag_mismatch（闭合标签不匹配）
    validator = HtmlValidator()
    translated_valid, translated_errors = validator.validate_chunk(translated, 0, "translated")

    # 只检查标签配对错误，不检查未闭合标签（那些可能是跨 chunk 的正常标签）
    if not translated_valid:
        error_msgs = []
        for err in translated_errors:
            if err.get("type") == "unexpected_close":
                error_msgs.append(f"意外的闭合标签: {err.get('actual')}")
            elif err.get("type") == "tag_mismatch":
                error_msgs.append(f"标签不匹配: 期望 {err.get('expected')}, 实际 {err.get('actual')}")
            elif err.get("type") == "unclosed_leaf_tag":
                error_msgs.append(f"叶子标签未闭合: {err.get('tag')}")
        if error_msgs:
            return False, "; ".join(error_msgs)
        return False, str(translated_errors) if translated_errors else "HTML structure error"

    # 原文/译文未闭合标签数量对比
    original_unclosed = _count_unclosed_tags(original)
    translated_unclosed = _count_unclosed_tags(translated)

    # 如果原文有未闭合标签，检查译文是否也有相似数量
    if original_unclosed:
        original_total = sum(original_unclosed.values())
        translated_total = sum(translated_unclosed.values())
        # 译文的未闭合标签数量应该与原文相近（允许一定差异）
        if translated_total > original_total + 2:
            return False, f"译文未闭合标签过多: 原文有 {original_total} 个未闭合，译文有 {translated_total} 个"

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
