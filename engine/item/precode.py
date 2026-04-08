from typing import List

from bs4 import BeautifulSoup


class PreCodeExtractor:
    """
    提取和恢复 pre/code 标签

    二级占位符方案：
    - 先提取 pre/code 标签，替换为 [PRE:n] 和 [CODE:n] 占位符
    - 翻译后恢复原始标签
    """

    def __init__(self) -> None:
        self.preserved_pre: List[str] = []   # 原始 pre 标签列表
        self.preserved_code: List[str] = []  # 原始 code 标签列表
        self.preserved_style: List[str] = [] # 原始 style 标签列表

    def extract(self, html: str) -> str:
        """
        提取 pre/code 标签，替换为占位符

        提取顺序：先 pre，后 code（递归处理）

        Returns:
            处理后的 HTML
        """
        # toc.ncx 等纯 XML 导航文件不需要 pre/code/style 提取，跳过
        # 判断依据：没有 <html> 标签且包含 <ncx> 的是纯 XML 文件
        stripped = html.strip()
        if '<html' not in stripped and (stripped.startswith('<?xml') or '<ncx' in stripped):
            return html
        soup = BeautifulSoup(html, 'html.parser')
        self.preserved_pre = []
        self.preserved_code = []
        self.preserved_style = []

        def process_node(node):
            """
            递归处理节点，将 pre/code 整体替换

            重要实现细节：
            - list(node.children) 创建子节点的快照列表
            - 这确保在 replace_with() 修改树结构时，迭代不会受影响
            - 处理顺序：从外层到内层（深度优先），确保嵌套标签正确处理
            """
            for child in list(node.children):
                if hasattr(child, 'name'):
                    if child.name == 'pre':
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 整个 pre 是不透明单元，不递归处理内部子节点
                        placeholder = f"[PRE:{len(self.preserved_pre)}]"
                        self.preserved_pre.append(original)
                        child.replace_with(BeautifulSoup(placeholder, 'html.parser'))
                    elif child.name == 'code':
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 整个 code 是不透明单元，不递归处理内部子节点
                        placeholder = f"[CODE:{len(self.preserved_code)}]"
                        self.preserved_code.append(original)
                        child.replace_with(BeautifulSoup(placeholder, 'html.parser'))
                    elif child.name == 'style':
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 整个 style 是不透明单元，不递归处理内部子节点
                        placeholder = f"[STYLE:{len(self.preserved_style)}]"
                        self.preserved_style.append(original)
                        child.replace_with(BeautifulSoup(placeholder, 'html.parser'))
                    elif hasattr(child, 'children'):
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
