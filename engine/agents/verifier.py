import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from engine.core.logger import engine_logger as logger


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
        if html[i] == '<':
            # 找到标签开始
            j = html.find('>', i)
            if j == -1:
                errors.append(f"标签未闭合: {html[i:i+20]}...")
                return False, errors

            tag = html[i:j + 1]

            # 跳过自闭合标签
            if is_self_closing(tag):
                i = j + 1
                continue

            # 跳过注释和DOCTYPE
            if tag.startswith('<!--') or tag.startswith('<!'):
                i = j + 1
                continue

            # 检查是否是结束标签
            if tag.startswith('</'):
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
        'br', 'hr', 'img', 'input', 'meta', 'link',
        'area', 'base', 'col', 'embed', 'param', 'source', 'track', 'wbr'
    }
    tag_name = get_tag_name(tag)
    if tag_name in self_closing:
        return True
    return tag.endswith('/>')


def get_tag_name(tag: str) -> Optional[str]:
    """从标签中提取标签名"""
    match = re.match(r'</?([a-zA-Z][a-zA-Z0-9]*)\b', tag)
    if match:
        return match.group(1).lower()
    return None


def validate_translated_html(original: str, translated: str) -> Tuple[bool, str]:
    """
    验证翻译结果的 HTML 结构完整性（chunk 级别）

    检查项：
    1. 顶层元素数量一致（翻译不应增删元素）
    2. 顶层元素标签名一致（<p> 不应变成 <div>）
    3. PreCodeExtractor 占位符完整保留
    """
    original_soup = BeautifulSoup(original, 'html.parser')
    translated_soup = BeautifulSoup(translated, 'html.parser')

    original_elements = [e for e in original_soup.children if hasattr(e, 'name') and e.name]
    translated_elements = [e for e in translated_soup.children if hasattr(e, 'name') and e.name]

    # 1. 元素数量
    if len(original_elements) != len(translated_elements):
        return False, f"元素数量不一致: 原始 {len(original_elements)}, 翻译 {len(translated_elements)}"

    # 2. 标签名一致
    for i, (orig, trans) in enumerate(zip(original_elements, translated_elements)):
        if orig.name != trans.name:
            return False, f"第 {i+1} 个元素标签不一致: 原始 <{orig.name}>, 翻译 <{trans.name}>"

    # 3. PreCodeExtractor 占位符完整
    for pattern in [r'\[PRE:\d+\]', r'\[CODE:\d+\]', r'\[STYLE:\d+\]']:
        orig_count = len(re.findall(pattern, original))
        trans_count = len(re.findall(pattern, translated))
        if orig_count != trans_count:
            return False, f"占位符数量不一致: {pattern} 原始 {orig_count}, 翻译 {trans_count}"

    return True, ""


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
    remaining = re.findall(r'\[(PRE|CODE|STYLE):\d+\]', restored)
    if remaining:
        return False, f"残留占位符: {remaining}"

    # 2. XML well-formedness 检查
    try:
        ET.fromstring(restored)
    except ET.ParseError as e:
        return False, f"XML 格式错误: {e}"

    return True, ""
