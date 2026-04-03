import re
from typing import List, Optional, Tuple


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
