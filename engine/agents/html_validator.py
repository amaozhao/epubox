import re
import xml.etree.ElementTree as etree
from typing import List, Tuple


class HtmlValidator:
    """
    HTML 标签栈验证器

    使用 LIFO 栈验证 HTML 标签配对正确性。
    支持跨 chunk 边界的标签追踪。
    """

    # 块级标签（可以作为分割点）
    BLOCK_TAGS = {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "blockquote",
        "pre",
        "section",
        "article",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "figure",
        "table",
        "tr",
        "td",
        "th",
        "thead",
        "tbody",
        "tfoot",
        # NCX file tags (lowercase for case-insensitive matching)
        "ncx",
        "navmap",
        "navpoint",
    }

    # 内联需要配对的标签
    INLINE_PAIRED_TAGS = {
        "em",
        "strong",
        "a",
        "code",
        "b",
        "i",
        "u",
        "small",
        "sub",
        "sup",
        "mark",
        "span",
        "del",
        "ins",
    }

    # 自闭合/空元素标签（不需要配对，也不会入栈）
    VOID_ELEMENTS = {
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

    # 兼容性别名
    SELF_CLOSING_TAGS = VOID_ELEMENTS

    # 所有需要追踪的标签（排除自闭合标签）
    TRACKED_TAGS = (BLOCK_TAGS | INLINE_PAIRED_TAGS) - VOID_ELEMENTS

    # 容器标签（可以跨 chunk 未闭合，是正常的）
    CONTAINER_TAGS = {
        "nav",
        "ol",
        "ul",
        "div",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        # NCX file tags (lowercase for case-insensitive matching)
        "ncx",
        "navmap",
        "navpoint",
    }

    # 叶子标签（必须在同一个 chunk 内闭合，未闭合是错误的）
    LEAF_TAGS = TRACKED_TAGS - CONTAINER_TAGS

    def __init__(self):
        self.stack: List[Tuple[str, int]] = []  # (tag_name, chunk_index)
        self.errors: List[dict] = []

    def reset(self):
        """重置验证器状态"""
        self.stack = []
        self.errors = []

    def validate_chunk(self, html: str, chunk_index: int, chunk_name: str) -> Tuple[bool, List[dict]]:
        """
        验证单个 chunk 的 HTML 结构

        注意：对于跨 chunk 的情况，容器标签未闭合是正常的，但叶子标签未闭合是错误的。

        Returns:
            Tuple[bool, List[dict]]: (是否有效, 当前 chunk 的错误列表)
        """
        errors_before = len(self.errors)
        self._parse_html(html, chunk_index)
        chunk_errors = self.errors[errors_before:]

        # 检查叶子标签是否在栈中未闭合
        for tag, idx in self.stack:
            if tag in self.LEAF_TAGS:
                chunk_errors.append(
                    {
                        "type": "unclosed_leaf_tag",
                        "message": f"叶子标签 <{tag}> 在 Chunk[{idx}] 打开但未闭合",
                        "tag": tag,
                        "chunk_index": idx,
                    }
                )

        return len(chunk_errors) == 0, chunk_errors

    def validate_merged(self, chunks: List[str], chunk_names: List[str]) -> Tuple[bool, List[dict]]:
        """
        验证合并后内容的 HTML 结构

        注意：叶子标签未闭合已经在 validate_chunk() 中报告为错误，
        这里只检查容器标签是否未闭合（跨 chunk 的容器标签是正常的，
        但如果合并后栈中仍有容器标签，说明可能是真的漏了闭合标签）。

        Returns:
            Tuple[bool, List[dict]]: (是否有效, 错误列表)
        """
        self.reset()

        for i, (chunk_html, chunk_name) in enumerate(zip(chunks, chunk_names)):
            self._parse_html(chunk_html, i)

        # 合并后检查栈是否为空
        # 只对容器标签报错（叶子标签已在 validate_chunk 中检查）
        if self.stack:
            unclosed_containers = []
            for tag, chunk_idx in reversed(self.stack):
                if tag in self.CONTAINER_TAGS:
                    unclosed_containers.append(f"</{tag}> (来自 Chunk[{chunk_idx}])")
            if unclosed_containers:
                self.errors.append(
                    {
                        "type": "unclosed_container_tags",
                        "message": f"有 {len(unclosed_containers)} 个未闭合的容器标签",
                        "details": unclosed_containers,
                        "chunk_index": self.stack[-1][1],
                        "chunk_name": chunk_names[self.stack[-1][1]],
                    }
                )
                return False, self.errors

        return len(self.errors) == 0, self.errors

    def _parse_html(self, html: str, chunk_index: int):
        """解析 HTML 内容，更新栈状态"""
        # 匹配开始标签: <tag> 或 <tag attr="value">
        start_tag_pattern = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*(?<!/)>")
        # 匹配自闭合标签: <tag /> 或 <tag>
        self_closing_pattern = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*/\s*>")
        # 匹配结束标签: </tag>
        end_tag_pattern = re.compile(r"</([a-zA-Z][a-zA-Z0-9]*)\s*>")

        i = 0
        while i < len(html):
            # 检查自闭合标签
            self_closing_match = self_closing_pattern.match(html, i)
            if self_closing_match:
                tag_name = self_closing_match.group(1).lower()
                if tag_name in self.TRACKED_TAGS:
                    # 自闭合标签不入栈，但如果是内联/块级标签需要检查
                    pass
                i = self_closing_match.end()
                continue

            # 检查开始标签
            start_match = start_tag_pattern.match(html, i)
            if start_match:
                tag_name = start_match.group(1).lower()
                if tag_name in self.TRACKED_TAGS:
                    self.stack.append((tag_name, chunk_index))
                i = start_match.end()
                continue

            # 检查结束标签
            end_match = end_tag_pattern.match(html, i)
            if end_match:
                tag_name = end_match.group(1).lower()
                if tag_name in self.TRACKED_TAGS:
                    self._handle_end_tag(tag_name, chunk_index)
                i = end_match.end()
                continue

            i += 1

    def _handle_end_tag(self, tag_name: str, chunk_index: int):
        """处理结束标签，检查是否与栈顶匹配"""
        if not self.stack:
            self.errors.append(
                {
                    "type": "unexpected_close",
                    "message": f"意外的闭合标签 </{tag_name}>",
                    "expected": None,
                    "actual": f"</{tag_name}>",
                    "chunk_index": chunk_index,
                }
            )
            return

        stack_tag, stack_chunk_idx = self.stack[-1]

        if stack_tag == tag_name:
            # 匹配成功，出栈
            self.stack.pop()
        else:
            # 不匹配，记录错误
            self.errors.append(
                {
                    "type": "tag_mismatch",
                    "message": "标签不匹配",
                    "expected": f"</{stack_tag}> (来自 Chunk[{stack_chunk_idx}])",
                    "actual": f"</{tag_name}> (Chunk[{chunk_index}])",
                    "chunk_index": chunk_index,
                    "stack_tag": stack_tag,
                    "stack_chunk_index": stack_chunk_idx,
                }
            )
            # 不出栈，因为当前标签可能属于外层

    def get_stack_state(self) -> List[Tuple[str, int]]:
        """获取当前栈状态，用于调试"""
        return self.stack.copy()

    def validate_with_lxml(self, html: str) -> Tuple[bool, List[str]]:
        """
        使用 lxml 验证 HTML/XML 语法（严格验证）

        Returns:
            Tuple[bool, List[str]]: (是否有效, 错误信息列表)
        """
        errors = []
        if not html or not html.strip():
            return True, errors
        try:
            etree.fromstring(html.encode("utf-8") if isinstance(html, str) else html)
            return True, errors
        except etree.ParseError as e:
            errors.append(f"XML/HTML语法错误: {str(e)}")
            return False, errors
        except Exception as e:
            errors.append(f"解析错误: {str(e)}")
            return False, errors


def validate_html_structure(html: str) -> Tuple[bool, List[str]]:
    """
    验证单个 HTML 片段的结构（不跨 chunk）

    Returns:
        Tuple[bool, List[str]]: (是否有效, 错误信息列表)
    """
    validator = HtmlValidator()
    valid, errors = validator.validate_chunk(html, 0, "single")

    error_messages = []
    for err in errors:
        if err["type"] == "unexpected_close":
            error_messages.append(f"意外的闭合标签 {err['actual']}")
        elif err["type"] == "tag_mismatch":
            error_messages.append(f"标签不匹配: 期望 {err['expected']}, 实际 {err['actual']}")
        elif err["type"] == "unclosed_tags":
            error_messages.append(f"未闭合标签: {', '.join(err['details'])}")

    return valid, error_messages
