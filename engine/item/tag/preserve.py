import re
from typing import List, Tuple

from engine.item.placeholder import PlaceholderManager


class TagPreserver:
    """
    将HTML标签替换为占位符

    合并策略：相邻的标签和空白合并为一个占位符
    """

    BLOCK_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "table", "tr", "td", "th",
        "blockquote", "pre", "code", "section", "article",
        "header", "footer", "nav", "aside", "main", "figure"
    }

    INLINE_TAGS = {
        "span", "a", "em", "strong", "b", "i", "u", "s",
        "sub", "sup", "small", "mark", "cite", "q", "abbr"
    }

    IGNORE_TAGS = {
        "script", "style", "svg", "math", "img", "meta",
        "link", "br", "hr", "input", "textarea", "select"
    }

    IGNORE_CLASSES = {
        ("code", "processedcode"),
        ("div", "no-translate"),
        ("span", "notranslate"),
        ("code", "language-*"),
    }

    def __init__(self):
        self.placeholder_mgr = None

    def preserve_tags(self, html: str, global_mgr=None) -> Tuple[str, PlaceholderManager]:
        """
        将HTML标签替换为占位符

        合并策略：相邻的标签和空白合并为一个占位符

        特殊处理：对于 <span class="koboSpan"> 这样的标签，
        会把整个标签对及其内容一起保留为占位符

        Args:
            html: 输入 HTML 文本
            global_mgr: 全局 PlaceholderManager。如果为 None，则创建新的。

        Returns:
            (处理后的文本, PlaceholderManager实例)
        """
        # 使用全局管理器或创建新的
        if global_mgr is not None:
            self.placeholder_mgr = global_mgr
        else:
            self.placeholder_mgr = PlaceholderManager()

        result_parts = []
        segments = re.split(r'(<[^>]+>)', html)

        current_tag_group = []
        WHITESPACE_PATTERN = re.compile(r'^[\s\r\n\t]+$')

        def flush_tag_group():
            """将当前标签组合并为一个占位符"""
            nonlocal current_tag_group
            if current_tag_group:
                merged_content = ''.join(current_tag_group)
                placeholder = self.placeholder_mgr.create_placeholder(merged_content)
                result_parts.append(placeholder)
                current_tag_group = []

        i = 0
        while i < len(segments):
            segment = segments[i]
            if not segment:
                i += 1
                continue

            # 检查是否是 koboSpan 标签（需要整体保护）
            kobo_match = self._is_kobo_span_tag(segment)
            if kobo_match:
                # 找到完整的 koboSpan 标签对
                full_span, inner_text, closing_idx = self._extract_kobo_span_pair(segments, i)
                if full_span:
                    # 先 flush 之前的标签组
                    flush_tag_group()
                    # 把整个 span 对作为一个占位符
                    placeholder = self.placeholder_mgr.create_placeholder(full_span)
                    result_parts.append(placeholder)
                    i = closing_idx + 1  # 跳到 closing tag 之后
                    continue

            is_tag = segment.startswith('<') and segment.endswith('>')
            is_non_trans = self._is_non_translatable(segment)
            is_whitespace = WHITESPACE_PATTERN.match(segment) is not None

            if is_tag or is_non_trans:
                # 标签或非翻译内容：加入当前组
                current_tag_group.append(segment)
            elif is_whitespace:
                # 空白字符：加入当前组（与标签合并）
                current_tag_group.append(segment)
            else:
                # 可翻译文本：先flush当前标签组为占位符
                flush_tag_group()
                # 添加可翻译文本
                result_parts.append(segment)

            i += 1

        # 处理末尾的标签组
        flush_tag_group()

        return ''.join(result_parts), self.placeholder_mgr

    def _is_kobo_span_tag(self, segment: str) -> bool:
        """检查是否是 koboSpan 开始标签"""
        if not segment.startswith('<'):
            return False
        return 'kobospan' in segment.lower()

    def _extract_kobo_span_pair(self, segments: List[str], start_idx: int) -> Tuple[str, str, int]:
        """
        从 segments 中提取完整的 koboSpan 标签对

        Returns:
            (完整的span标签对, 内部文本, closing_tag的索引)
            例如: ('<span class="koboSpan">12</span>', '12', 2)
        """
        if start_idx >= len(segments):
            return None, None, -1

        opening_tag = segments[start_idx]
        if not self._is_kobo_span_tag(opening_tag):
            return None, None, -1

        if start_idx + 1 >= len(segments):
            return None, None, -1

        inner_text = segments[start_idx + 1]

        # 查找结束标签
        for end_idx in range(start_idx + 2, len(segments)):
            tag = segments[end_idx]
            if tag.startswith('</span') or tag.startswith('</Span'):
                closing_tag = tag
                # 重建完整的 span 对
                full_span = opening_tag + inner_text + closing_tag
                return full_span, inner_text, end_idx

        return None, None, -1

    def _is_non_translatable(self, tag: str) -> bool:
        """检查标签是否不可翻译"""
        # 检查是否是自闭合标签
        if re.match(r'<(meta|link|img|br|hr|input)\b', tag, re.I):
            return True

        # 检查是否是script/style等
        tag_name = re.match(r'<(\w+)', tag)
        if tag_name and tag_name.group(1).lower() in self.IGNORE_TAGS:
            return True

        return False
