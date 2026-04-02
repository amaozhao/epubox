import re
from typing import Optional


def verify_html_integrity(html: str) -> bool:
    """
    验证HTML标签是否正确闭合
    """

    # 使用栈来跟踪标签
    stack = []
    i = 0
    n = len(html)

    while i < n:
        if html[i] == '<':
            # 找到标签开始
            j = html.find('>', i)
            if j == -1:
                return False  # 未找到标签结束

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
                    return False
                # else: 未匹配的结束标签，忽略

            else:
                # 开始标签
                tag_name = get_tag_name(tag)
                if tag_name and not is_self_closing(tag):
                    stack.append(tag_name)

            i = j + 1
        else:
            i += 1

    # 检查是否所有标签都闭合
    return len(stack) == 0


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
