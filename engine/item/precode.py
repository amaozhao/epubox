import re
from typing import List

from bs4 import BeautifulSoup
from bs4.element import NavigableString

from engine.core.logger import engine_logger as logger
from engine.core.markup import get_markup_parser


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
        self._code_run_separator_re = re.compile(r"^[\s/|:+(),.;=\\-]+$")
        self._code_like_keyword_re = re.compile(
            r"(code|highlight|listing|programlisting|source|syntax|shell|terminal|console|pygments)",
            re.IGNORECASE,
        )
        self._code_token_keyword_re = re.compile(
            r"\b(import|from|class|def|return|async|await|function|const|let|var|print|SELECT|INSERT|UPDATE|DELETE)\b",
            re.IGNORECASE,
        )
        self._prose_word_re = re.compile(r"[A-Za-z]{3,}")
        self._identifier_like_re = re.compile(
            r"(@?[A-Za-z_][A-Za-z0-9_]*\([^)]*\)|\b[A-Za-z_][A-Za-z0-9_]*::[A-Za-z_][A-Za-z0-9_]*\b|"
            r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b|\b[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+\b)"
        )
        self._inline_code_like_tags = {"code", "tt", "kbd", "samp", "var", "span"}

    def extract(self, html: str) -> str:
        """
        提取 pre/code 标签，替换为占位符

        提取策略：
        - 命中 pre/code/style 后，整体替换为占位符
        - 不再递归进入这些受保护标签的子树，避免嵌套标签重复记账

        Returns:
            处理后的 HTML
        """
        soup = BeautifulSoup(html, get_markup_parser(html))
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
            children = list(node.children)
            index = 0
            while index < len(children):
                child = children[index]
                if hasattr(child, "name"):
                    if child.name == "pre":
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 替换当前 pre 标签
                        placeholder = f"[PRE:{len(self.preserved_pre)}]"
                        self.preserved_pre.append(original)
                        child.replace_with(placeholder)
                        index += 1
                    elif self._is_code_like_container(child):
                        original = str(child)
                        placeholder = f"[PRE:{len(self.preserved_pre)}]"
                        self.preserved_pre.append(original)
                        child.replace_with(placeholder)
                        index += 1
                    elif self._is_code_like_node_for_run(child):
                        original, run_end = self._collect_code_like_run(children, index)
                        placeholder = f"[CODE:{len(self.preserved_code)}]"
                        self.preserved_code.append(original)
                        child.replace_with(placeholder)
                        for extra in children[index + 1 : run_end]:
                            if getattr(extra, "parent", None):
                                extra.extract()
                        index = run_end
                    elif child.name == "style":
                        # 先保存原始内容（必须在 replace_with 之前！）
                        original = str(child)
                        # 替换当前 style 标签
                        placeholder = f"[STYLE:{len(self.preserved_style)}]"
                        self.preserved_style.append(original)
                        child.replace_with(placeholder)
                        index += 1
                    elif hasattr(child, "children"):
                        process_node(child)
                        index += 1
                    else:
                        index += 1
                else:
                    index += 1

        # 处理 body 或直接处理 soup（处理 HTML 片段时 body 可能为 None）
        target = soup.body if soup.body else soup
        process_node(target)

        return str(soup)

    def _collect_code_like_run(self, children: list, start_index: int) -> tuple[str, int]:
        """
        收集保守版 code-like run。

        仅合并以下模式：
        - <code>...</code> 或内联 code-like 包装节点
        - 中间夹着纯文本分隔符（如 '/', '+', ':', 空格）
        - 紧接着另一个 code-like 节点
        """
        run_nodes = [children[start_index]]
        index = start_index + 1

        while index + 1 < len(children):
            separator = children[index]
            next_node = children[index + 1]
            if not self._is_code_run_separator(separator):
                break
            if not self._is_code_like_node_for_run(next_node):
                break
            run_nodes.extend([separator, next_node])
            index += 2

        original = "".join(str(node) for node in run_nodes)
        return original, start_index + len(run_nodes)

    def _is_code_like_node_for_run(self, node) -> bool:
        name = getattr(node, "name", None)
        if not name:
            return False
        if name == "code":
            return True
        return self._is_inline_code_like_node(node)

    def _is_code_run_separator(self, node) -> bool:
        if not isinstance(node, NavigableString):
            return False
        text = str(node)
        if not text:
            return False
        return bool(self._code_run_separator_re.fullmatch(text))

    def _is_inline_code_like_node(self, element) -> bool:
        name = getattr(element, "name", None)
        if not name or name not in self._inline_code_like_tags or name == "code":
            return False

        if any(getattr(desc, "name", None) in {"pre", "style"} for desc in element.descendants):
            return False

        score, _ = self._score_inline_code_like_node(element)
        return score >= 4

    def _score_inline_code_like_node(self, element) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        metadata_hits = self._count_code_like_metadata_hits(element)
        if metadata_hits:
            score += 3
            reasons.append(f"metadata:{metadata_hits}")

        tt_count = len(element.find_all("tt"))
        if tt_count >= 1:
            score += 4
            reasons.append(f"tt:{tt_count}")

        if getattr(element, "name", None) in {"tt", "kbd", "samp", "var"}:
            score += 3
            reasons.append(f"direct-tag:{element.name}")

        semantic_code_tag_count = len(element.find_all(["code", "kbd", "samp", "var"]))
        if semantic_code_tag_count >= 1:
            score += 3
            reasons.append(f"semantic-tags:{semantic_code_tag_count}")

        text_chunks = [chunk.strip() for chunk in element.stripped_strings if chunk.strip()]
        joined_text = " ".join(text_chunks)
        if self._is_codeish_text_chunk(joined_text):
            score += 2
            reasons.append("codeish-text")

        if 0 < len(joined_text) <= 80 and len(text_chunks) <= 3:
            score += 1
            reasons.append("compact")

        prose_runs = sum(1 for chunk in text_chunks if len(self._prose_word_re.findall(chunk)) >= 4)
        if prose_runs >= 1 and not metadata_hits and tt_count == 0 and semantic_code_tag_count == 0:
            score -= 3
            reasons.append(f"prose-penalty:{prose_runs}")

        return score, reasons

    def _is_code_like_container(self, element) -> bool:
        """
        识别语义上是代码块、但没有显式使用 pre/code 的容器。

        典型场景：
        - EPUB/Calibre 导出的 syntax-highlight 代码块：blockquote > span > tt > span
        - 带 highlight/listing/source 等类名的代码容器
        """
        name = getattr(element, "name", None)
        if not name or name in {"pre", "code", "style"}:
            return False

        block_like_tags = {
            "blockquote",
            "div",
            "figure",
            "section",
            "article",
            "aside",
            "table",
            "tbody",
            "thead",
            "tr",
            "td",
            "th",
            "ul",
            "ol",
        }
        if name not in block_like_tags:
            return False

        score, _ = self._score_code_like_container(element)
        return score >= 5

    def _score_code_like_container(self, element) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        metadata_hits = self._count_code_like_metadata_hits(element)
        if metadata_hits:
            score += 5
            reasons.append(f"metadata:{metadata_hits}")

        tt_count = len(element.find_all("tt"))
        if tt_count >= 3:
            score += 5
            reasons.append(f"tt:{tt_count}")
        elif tt_count >= 1:
            score += 2
            reasons.append(f"tt:{tt_count}")

        semantic_code_tag_count = len(element.find_all(["code", "kbd", "samp", "var"]))
        if semantic_code_tag_count >= 3:
            score += 4
            reasons.append(f"semantic-tags:{semantic_code_tag_count}")
        elif semantic_code_tag_count >= 1:
            score += 2
            reasons.append(f"semantic-tags:{semantic_code_tag_count}")

        br_count = len(element.find_all("br"))
        if br_count >= 2:
            score += 2
            reasons.append(f"br:{br_count}")

        text_chunks = [chunk.strip() for chunk in element.stripped_strings if chunk.strip()]
        short_chunk_count = sum(1 for chunk in text_chunks if len(chunk) <= 24)
        if short_chunk_count >= 6:
            score += 1
            reasons.append(f"short-chunks:{short_chunk_count}")

        joined_text = " ".join(text_chunks)
        symbol_hits = len(re.findall(r"[{}\[\]();:=<>/$#]", joined_text))
        if symbol_hits >= 6:
            score += 2
            reasons.append(f"symbols:{symbol_hits}")
        elif symbol_hits >= 3:
            score += 1
            reasons.append(f"symbols:{symbol_hits}")

        keyword_hits = len(self._code_token_keyword_re.findall(joined_text))
        if keyword_hits >= 2:
            score += 2
            reasons.append(f"keywords:{keyword_hits}")

        codeish_chunks = sum(1 for chunk in text_chunks if self._is_codeish_text_chunk(chunk))
        if codeish_chunks >= 4:
            score += 3
            reasons.append(f"codeish-chunks:{codeish_chunks}")
        elif codeish_chunks >= 2:
            score += 2
            reasons.append(f"codeish-chunks:{codeish_chunks}")
        elif codeish_chunks >= 1:
            score += 1
            reasons.append(f"codeish-chunks:{codeish_chunks}")

        prose_runs = sum(1 for chunk in text_chunks if len(self._prose_word_re.findall(chunk)) >= 5)
        if codeish_chunks >= 2 and codeish_chunks >= prose_runs:
            score += 1
            reasons.append(f"code-dominance:{codeish_chunks}/{prose_runs}")

        if element.name in {"table", "tbody", "thead", "tr", "td", "th", "ul", "ol"} and codeish_chunks >= 2:
            score += 2
            reasons.append(f"structure-bonus:{element.name}")

        if prose_runs >= 2 and not metadata_hits and tt_count == 0 and semantic_code_tag_count == 0:
            score -= 3
            reasons.append(f"prose-penalty:{prose_runs}")
        elif prose_runs >= 4 and codeish_chunks <= 1:
            score -= 2
            reasons.append(f"prose-heavy:{prose_runs}")

        return score, reasons

    def _has_code_like_metadata(self, element) -> bool:
        return self._count_code_like_metadata_hits(element) > 0

    def _count_code_like_metadata_hits(self, element) -> int:
        candidate_values: List[str] = []
        for attr in ("class", "id", "role", "epub:type", "title"):
            value = element.get(attr)
            if not value:
                continue
            if isinstance(value, list):
                candidate_values.extend(str(v) for v in value)
            else:
                candidate_values.append(str(value))

        return sum(1 for value in candidate_values if self._code_like_keyword_re.search(value))

    def _is_codeish_text_chunk(self, text: str) -> bool:
        chunk = text.strip()
        if not chunk:
            return False
        if self._code_token_keyword_re.search(chunk):
            return True
        if self._identifier_like_re.search(chunk):
            return True
        if len(re.findall(r"[{}\[\]();:=<>/$#]", chunk)) >= 2 and len(chunk) <= 80:
            return True
        if chunk.startswith(("#", "//", "$ ", ">>>", "...")):
            return True
        return False

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
