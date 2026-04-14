import re
from typing import List

from bs4 import BeautifulSoup

from engine.core.logger import engine_logger as logger


class PreCodeExtractor:
    """
    提取和恢复 pre/code 标签

    二级占位符方案：
    - 先提取 pre/code 标签，替换为 [PRE:n] 和 [CODE:n] 占位符
    - 翻译后恢复原始标签
    """

    def __init__(self):
        self.preserved_pre: List[str] = []  # 原始 pre 标签列表
        self.preserved_code: List[str] = []  # 原始 code 标签列表
        self.preserved_style: List[str] = []  # 原始 style 标签列表

    def extract(self, html: str) -> str:
        """
        提取 pre/code 标签，替换为占位符

        提取策略：
        - 命中 pre/code/style 后，整体替换为占位符
        - 不再递归进入这些受保护标签的子树，避免嵌套标签重复记账

        Returns:
            处理后的 HTML
        """
        soup = BeautifulSoup(html, "html.parser")
        self.preserved_pre = []
        self.preserved_code = []
        self.preserved_style = []

        def process_node(node):
            """
            递归处理节点，将 pre/code 整体替换

            重要实现细节：
            - list(node.children) 创建子节点的快照列表
            - 这确保在 replace_with() 修改树结构时，迭代不会受影响
            - 普通节点继续深度优先遍历
            - pre/code/style 视为原子块，命中后直接整体替换
            """
            for child in list(node.children):
                if hasattr(child, "name"):
                    if child.name == "pre":
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 替换当前 pre 标签
                        placeholder = f"[PRE:{len(self.preserved_pre)}]"
                        self.preserved_pre.append(original)
                        child.replace_with(placeholder)
                    elif child.name == "code":
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 替换当前 code 标签
                        placeholder = f"[CODE:{len(self.preserved_code)}]"
                        self.preserved_code.append(original)
                        child.replace_with(placeholder)
                    elif child.name == "style":
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 替换当前 style 标签
                        placeholder = f"[STYLE:{len(self.preserved_style)}]"
                        self.preserved_style.append(original)
                        child.replace_with(placeholder)
                    elif hasattr(child, "children"):
                        process_node(child)

        # 处理 body 或直接处理 soup（处理 HTML 片段时 body 可能为 None）
        target = soup.body if soup.body else soup
        process_node(target)

        return str(soup)

    def restore(self, html: str) -> str:
        """
        恢复 pre/code/style 标签

        恢复顺序：先 style，后 code，后 pre
        替换顺序：按索引从大到小，避免子串匹配问题

        Returns:
            恢复后的 HTML
        """
        # 先恢复 style（从大到小）
        for i in range(len(self.preserved_style) - 1, -1, -1):
            html = html.replace(f"[STYLE:{i}]", self.preserved_style[i])

        # 再恢复 code（从大到小）
        for i in range(len(self.preserved_code) - 1, -1, -1):
            html = html.replace(f"[CODE:{i}]", self.preserved_code[i])

        # 后恢复 pre（从大到小）
        for i in range(len(self.preserved_pre) - 1, -1, -1):
            html = html.replace(f"[PRE:{i}]", self.preserved_pre[i])

        return html

    @property
    def pre_count(self) -> int:
        return len(self.preserved_pre)

    @property
    def code_count(self) -> int:
        return len(self.preserved_code)

    @property
    def style_count(self) -> int:
        return len(self.preserved_style)


def validate_placeholders(html: str, expected_pre: int, expected_code: int, expected_style: int = 0) -> bool:
    """
    验证占位符是否完整

    Returns:
        True 如果所有占位符都存在且格式正确
    """
    pre_found = len(re.findall(r"\[PRE:\d+\]", html))
    code_found = len(re.findall(r"\[CODE:\d+\]", html))
    style_found = len(re.findall(r"\[STYLE:\d+\]", html))

    if pre_found != expected_pre:
        logger.error(f"PRE占位符数量不匹配: 期望{expected_pre}, 实际{pre_found}")
        return False

    if code_found != expected_code:
        logger.error(f"CODE占位符数量不匹配: 期望{expected_code}, 实际{code_found}")
        return False

    if style_found != expected_style:
        logger.error(f"STYLE占位符数量不匹配: 期望{expected_style}, 实际{style_found}")
        return False

    return True


def attempt_recovery(
    html: str, preserved_pre: List[str], preserved_code: List[str], preserved_style: List[str] | None = None
) -> str:
    r"""
    尝试恢复可能被破坏的占位符（仅处理格式变形，不处理缺失）

    可修复的模式：
    - [PRE;\d+] → [PRE:\d+]  （分号变冒号）
    - [PRE: \d+] → [PRE:\d+] （多余空格）
    - [CODE;\d+] → [CODE:\d+]
    - [CODE: \d+] → [CODE:\d+]
    - [STYLE;\d+] → [STYLE:\d+]
    - [STYLE: \d+] → [STYLE:\d+]

    不可修复的模式（只能报告错误）：
    - PRE:0 （丢失左方括号）
    - [PRE: （丢失右方括号）
    - [PRE0] （丢失冒号）

    注意：修复后需要重新验证！
    """
    # 先修复多余空格（包括分号后面的空格）
    html = re.sub(r"\[PRE:\s+(\d+)\]", r"[PRE:\1]", html)
    html = re.sub(r"\[CODE:\s+(\d+)\]", r"[CODE:\1]", html)
    html = re.sub(r"\[STYLE:\s+(\d+)\]", r"[STYLE:\1]", html)
    html = re.sub(r"\[PRE;\s+(\d+)\]", r"[PRE;\1]", html)
    html = re.sub(r"\[CODE;\s+(\d+)\]", r"[CODE;\1]", html)
    html = re.sub(r"\[STYLE;\s+(\d+)\]", r"[STYLE;\1]", html)

    # 再修复分号
    html = re.sub(r"\[PRE;(\d+)\]", r"[PRE:\1]", html)
    html = re.sub(r"\[CODE;(\d+)\]", r"[CODE:\1]", html)
    html = re.sub(r"\[STYLE;(\d+)\]", r"[STYLE:\1]", html)

    return html
