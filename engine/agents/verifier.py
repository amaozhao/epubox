import re
from typing import List, Optional, Tuple

from lxml import etree


def verify_html_integrity(html: str) -> Tuple[bool, List[str]]:
    """
    验证HTML/XML标签是否正确闭合（使用 lxml 解析器）

    Returns:
        Tuple[bool, List[str]]: (是否有效, 错误信息列表)
    """
    errors = []

    # 空内容视为有效（某些 chunk 可能为空）
    if not html or not html.strip():
        return True, errors

    try:
        etree.fromstring(html.encode("utf-8") if isinstance(html, str) else html)
        return True, errors
    except etree.XMLSyntaxError as e:
        errors.append(f"XML/HTML语法错误: {str(e)}")
        return False, errors
    except Exception as e:
        errors.append(f"解析错误: {str(e)}")
        return False, errors


def is_self_closing(tag: str) -> bool:
    """检查是否是自闭合标签"""
    self_closing = {
        "br", "hr", "img", "input", "meta", "link",
        "area", "base", "col", "embed", "param", "source", "track", "wbr"
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
