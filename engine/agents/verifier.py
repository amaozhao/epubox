from collections import Counter
import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup



def verify_html_integrity(html: str) -> Tuple[bool, List[str]]:
    """
    验证HTML标签是否正确闭合

    Returns:
        Tuple[bool, List[str]]: (是否有效, 错误信息列表)
    """
    errors = []

    # 使用栈来跟踪标签
    stack = []
    i = 0
    n = len(html)

    while i < n:
        if html[i] == "<":
            # 找到标签开始
            j = html.find(">", i)
            if j == -1:
                errors.append(f"标签未闭合: {html[i : i + 20]}...")
                return False, errors

            tag = html[i : j + 1]

            # 跳过自闭合标签
            if is_self_closing(tag):
                i = j + 1
                continue

            # 跳过注释和DOCTYPE
            if tag.startswith("<!--") or tag.startswith("<!"):
                i = j + 1
                continue

            # 检查是否是结束标签
            if tag.startswith("</"):
                tag_name = get_tag_name(tag)
                if not tag_name:
                    i = j + 1
                    continue

                if stack and stack[-1] == tag_name:
                    stack.pop()
                elif tag_name in stack:
                    # 标签交错
                    errors.append(f"标签交错: </{tag_name}> 没有匹配的 <{tag_name}>")
                    return False, errors
                else:
                    # 未匹配的结束标签
                    errors.append(f"未匹配的结束标签: {tag}")

            else:
                # 开始标签
                tag_name = get_tag_name(tag)
                if tag_name and not is_self_closing(tag):
                    stack.append(tag_name)

            i = j + 1
        else:
            i += 1

    # 检查是否所有标签都闭合
    if stack:
        for tag_name in stack:
            errors.append(f"未闭合的标签: <{tag_name}>")
        return False, errors

    return True, errors


def is_self_closing(tag: str) -> bool:
    """检查是否是自闭合标签"""
    self_closing = {
        "br",
        "hr",
        "img",
        "input",
        "meta",
        "link",
        "area",
        "base",
        "col",
        "embed",
        "param",
        "source",
        "track",
        "wbr",
    }
    tag_name = get_tag_name(tag)
    if tag_name in self_closing:
        return True
    return tag.endswith("/>")


def get_tag_name(tag: str) -> Optional[str]:
    """从标签中提取标签名"""
    match = re.match(r"</?([a-zA-Z][a-zA-Z0-9]*)\b", tag)
    if match:
        return match.group(1).lower()
    return None


def _looks_like_technical_ascii_noop(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not stripped.isascii():
        return False

    command_starters = {
        "bash",
        "curl",
        "docker",
        "git",
        "kubectl",
        "make",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "pytest",
        "python",
        "python3",
        "sh",
        "uv",
        "wget",
        "yarn",
    }
    score = 0

    if re.search(r"https?://\S+", stripped):
        score += 2
    if re.search(r"(?:^|\s)--?[A-Za-z0-9][A-Za-z0-9_-]*\b", stripped):
        score += 1
    if re.search(r"\b[\w./-]+/[\w./-]+\b", stripped):
        score += 1
    if re.search(r"\b[\w.-]+\.(?:py|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|md|txt|html|xml|epub|sh)\b", stripped):
        score += 1
    if re.search(r"\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b", stripped):
        score += 1
    if re.search(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", stripped):
        score += 1
    if re.search(r"\b[A-Za-z0-9_.-]+::[A-Za-z0-9_.-]+\b", stripped):
        score += 1

    tokens = stripped.split()
    if tokens and tokens[0] in command_starters and re.fullmatch(r"[A-Za-z0-9_./:=@+-]+(?:\s+[A-Za-z0-9_./:=@+-]+)*", stripped):
        score += 2

    if re.fullmatch(r"[A-Za-z0-9_.:/+-]+", stripped):
        if stripped in command_starters:
            score += 2
        elif re.search(r"[._:/+-]|\d", stripped):
            score += 1

    return score >= 2


def _classify_unchanged_translation(original_soup: BeautifulSoup, translated_soup: BeautifulSoup) -> str | None:
    """区分原样回显是未翻译，还是本就应保持不变的 no-op。"""
    if str(original_soup) != str(translated_soup):
        return None

    visible_text = re.sub(r"\[(PRE|CODE|STYLE):\d+\]", " ", original_soup.get_text(" ", strip=True)).strip()
    if not any(char.isalpha() for char in visible_text):
        return "accepted_as_is"
    if _looks_like_technical_ascii_noop(visible_text):
        return "accepted_as_is"
    return "echo"


def validate_translated_html(original: str, translated: str) -> Tuple[bool, str]:
    """
    验证翻译结果的 HTML 结构完整性（chunk 级别）

    检查项：
    0. 原始字符串标签完整性（先验证，避免 BeautifulSoup 自动修复掩盖错误）
    0.5 XML 特殊字符（& 是否转义，跳过 HTML 实体）
    1. 顶层元素数量一致（翻译不应增删元素）
    2. 顶层元素标签名一致（<p> 不应变成 <div>）
    3. PreCodeExtractor 占位符完整保留
    """
    # 0. 先验证翻译结果的原始字符串（捕获 BeautifulSoup 会自动修复的错误）
    is_valid_raw, errors = verify_html_integrity(translated)
    if not is_valid_raw:
        error_detail = errors[0] if errors else "未知标签错误"
        return False, f"HTML标签结构错误: {error_detail}"

    # 0.5 验证 XML 特殊字符（检查 & 是否转义，跳过 HTML 实体如 &nbsp; &amp;）
    # 用正则检查裸 &: & 后必须有 ; + 数字/字母，否则是未转义的 &
    # &amp; &lt; &gt; &quot; &apos; &nbsp; 等都是合法实体，LLM 输出的 "A & B" 才是错误
    if re.search(r"&(?![#][0-9]+|[a-zA-Z][a-zA-Z0-9]*;)", translated):
        return False, "XML 格式错误: 发现未转义的 & 字符（需使用 &amp;）"

    original_soup = BeautifulSoup(original, "html.parser")
    translated_soup = BeautifulSoup(translated, "html.parser")

    original_elements = [e for e in original_soup.children if hasattr(e, "name") and e.name]
    translated_elements = [e for e in translated_soup.children if hasattr(e, "name") and e.name]

    # 1. 元素数量
    if len(original_elements) != len(translated_elements):
        return False, f"元素数量不一致: 原始 {len(original_elements)}, 翻译 {len(translated_elements)}"

    # 1.5 识别整块原样回显：区分未翻译回显和合法 no-op
    unchanged_result = _classify_unchanged_translation(original_soup, translated_soup)
    if unchanged_result == "echo":
        return False, "翻译结果与原文一致，疑似未翻译"
    if unchanged_result == "accepted_as_is":
        return True, "accepted_as_is"

    # 2. 标签名一致
    for i, (orig, trans) in enumerate(zip(original_elements, translated_elements)):
        if orig.name != trans.name:
            return False, f"第 {i + 1} 个元素标签不一致: 原始 <{orig.name}>, 翻译 <{trans.name}>"

    # 3. PreCodeExtractor 占位符完整且索引不变
    for label, pattern in [
        ("PRE", r"\[PRE:\d+\]"),
        ("CODE", r"\[CODE:\d+\]"),
        ("STYLE", r"\[STYLE:\d+\]"),
    ]:
        if label == "CODE":
            mismatch_details = _collect_element_scoped_code_multiset_mismatches(
                original_elements=original_elements,
                translated_elements=translated_elements,
                pattern=pattern,
            )
            if mismatch_details:
                return False, _format_code_placeholder_error(mismatch_details)
            continue

        mismatch_details = _collect_element_scoped_placeholder_mismatches(
            original_elements=original_elements,
            translated_elements=translated_elements,
            pattern=pattern,
            allow_adjacent_swaps=False,
        )
        if mismatch_details:
            return False, _format_placeholder_sequence_error(label, mismatch_details)

    return True, ""


def _collect_element_scoped_placeholder_mismatches(
    original_elements: list,
    translated_elements: list,
    pattern: str,
    allow_adjacent_swaps: bool = False,
) -> list[tuple[int, str, str]]:
    """按顶层元素作用域校验占位符序列，避免跨元素放宽顺序约束。"""
    all_details: list[tuple[int, str, str]] = []
    position_base = 0

    for orig_element, trans_element in zip(original_elements, translated_elements):
        orig_placeholders = re.findall(pattern, str(orig_element))
        trans_placeholders = re.findall(pattern, str(trans_element))
        element_details = _collect_placeholder_mismatches(
            orig_placeholders,
            trans_placeholders,
            allow_adjacent_swaps=allow_adjacent_swaps,
        )
        for position, orig_token, trans_token in element_details:
            all_details.append((position_base + position, orig_token, trans_token))
        position_base += len(orig_placeholders)

    return all_details


def _collect_element_scoped_code_multiset_mismatches(
    original_elements: list,
    translated_elements: list,
    pattern: str,
) -> list[tuple[int, str, str]]:
    """
    对 CODE 只校验同一顶层元素内的多重集合一致性。

    允许同一元素内部重排，但不允许：
    - 数量变化
    - 索引变化
    - 跨顶层元素迁移
    """
    all_details: list[tuple[int, str, str]] = []

    for element_index, (orig_element, trans_element) in enumerate(zip(original_elements, translated_elements), start=1):
        orig_placeholders = re.findall(pattern, str(orig_element))
        trans_placeholders = re.findall(pattern, str(trans_element))
        if Counter(orig_placeholders) == Counter(trans_placeholders):
            continue

        element_details = _collect_placeholder_mismatches(
            orig_placeholders,
            trans_placeholders,
            allow_adjacent_swaps=False,
        )
        for position, orig_token, trans_token in element_details:
            all_details.append((element_index, position, orig_token, trans_token))

    return all_details


def _collect_placeholder_mismatches(
    original: list[str],
    translated: list[str],
    allow_adjacent_swaps: bool = False,
) -> list[tuple[int, str, str]]:
    """返回占位符不匹配的位置列表；可选择性允许相邻成对换位。"""
    details: list[tuple[int, str, str]] = []
    common_len = min(len(original), len(translated))
    idx = 0

    while idx < common_len:
        orig_token = original[idx]
        trans_token = translated[idx]
        if orig_token == trans_token:
            idx += 1
            continue

        if (
            allow_adjacent_swaps
            and idx + 1 < common_len
            and original[idx] == translated[idx + 1]
            and original[idx + 1] == translated[idx]
        ):
            idx += 2
            continue

        details.append((idx + 1, orig_token, trans_token))
        idx += 1

    if len(original) > len(translated):
        for idx in range(common_len, len(original)):
            details.append((idx + 1, original[idx], "翻译缺失"))
    elif len(translated) > len(original):
        for idx in range(common_len, len(translated)):
            details.append((idx + 1, "原始缺失", translated[idx]))

    return details


def _format_placeholder_sequence_error(label: str, details: list[tuple[int, str, str]]) -> str:
    """输出占位符顺序错误的精确位置，便于日志与重试提示使用。"""
    rendered: list[str] = []

    for position, orig_token, trans_token in details:
        rendered.append(f"位置{position}: 原始 {orig_token}, 翻译 {trans_token}")

    if not rendered:
        rendered.append("位置未知")

    return f"{label} 占位符顺序不一致: {'; '.join(rendered)}"


def _format_code_placeholder_error(details: list[tuple[int, str, str, str]]) -> str:
    rendered: list[str] = []

    for element_index, position, orig_token, trans_token in details:
        rendered.append(f"元素{element_index} 位置{position}: 原始 {orig_token}, 翻译 {trans_token}")

    if not rendered:
        rendered.append("位置未知")

    return "CODE 占位符归属/数量不一致（请保持每个 CODE 占位符留在原始顶层元素内）: " + "; ".join(rendered)


def verify_final_html(original: str, restored: str) -> Tuple[bool, str]:
    """
    验证最终恢复后 HTML 的完整性（文件级别）

    检查项：
    1. 无残留 PreCodeExtractor 占位符
    2. XML well-formedness（XHTML 本质是 XML）

    使用 xml.etree.ElementTree 解析而非 lxml/BeautifulSoup，
    因为后两者会自动修正不合法标签，无法检测出实际错误。
    """
    # 1. 无残留占位符
    remaining = re.findall(r"\[(PRE|CODE|STYLE):\d+\]", restored)
    if remaining:
        return False, f"残留占位符: {remaining}"

    # 2. XML well-formedness 检查
    try:
        ET.fromstring(restored)
    except ET.ParseError as e:
        return False, f"XML 格式错误: {e}"

    return True, ""
