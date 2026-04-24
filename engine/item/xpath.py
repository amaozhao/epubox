import re


def _normalize_name(name: str) -> str:
    return (name or "").split(":")[-1].lower()


def get_xpath(element) -> str:
    """
    获取 BeautifulSoup 元素在 DOM 中的路径。

    示例：/html/body/div[2]/p[3]

    实现原理：从目标元素向上遍历到根节点，
    对每一层计算该元素在同名兄弟中的位置索引。
    只有同名兄弟数量 > 1 时才添加索引。
    """
    parts = []
    current = element
    while current.parent:
        if hasattr(current, "name") and current.name:
            siblings = [s for s in current.parent.children if hasattr(s, "name") and s.name == current.name]
            if len(siblings) > 1:
                index = next(i for i, sibling in enumerate(siblings) if sibling is current) + 1
                parts.append(f"{current.name}[{index}]")
            else:
                parts.append(current.name)
        current = current.parent
    return "/" + "/".join(reversed(parts))


def find_by_xpath(soup, xpath: str):
    """
    在 BeautifulSoup DOM 树中按路径查找元素。

    Args:
        soup: BeautifulSoup 对象
        xpath: 路径字符串，如 /html/body/p[2]

    Returns:
        匹配的元素，未找到返回 None

    实现原理：将路径拆分为各级段（如 ['html', 'body', 'p[2]']），
    从根节点逐级向下查找，解析每段的标签名和可选索引。
    无索引时默认为第 1 个。
    """
    parts = [p for p in xpath.split("/") if p]
    current = soup

    for part in parts:
        match = re.match(r"^([\w:.-]+)(?:\[(\d+)\])?$", part)
        if not match:
            return None
        tag_name = _normalize_name(match.group(1))
        index = int(match.group(2)) if match.group(2) else 1

        children = [
            c for c in current.children if hasattr(c, "name") and c.name and _normalize_name(c.name) == tag_name
        ]
        if index > len(children):
            return None
        current = children[index - 1]

    return current
