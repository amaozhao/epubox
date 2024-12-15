"""
HTML processor module.
Handles HTML content processing and transformation.
"""

import copy
import html
import re
from typing import Dict, Tuple, Any

from bs4 import BeautifulSoup, NavigableString, Tag, Comment

from app.core.logging import get_logger

log = get_logger(__name__)

# 不需要翻译的标签集合
SKIP_TAGS = {
    # 脚本和样式
    "script",
    "style",
    # 代码相关
    "code",
    "pre",
    "kbd",
    "var",
    "samp",
    # 特殊内容
    "svg",
    "math",
    "canvas",
    "address",
    "applet",
    # 多媒体标签
    "img",
    "audio",
    "video",
    "track",
    "source",
    # 表单相关
    "input",
    "button",
    "select",
    "option",
    "textarea",
    "form",
    # 元数据和链接
    "meta",
    "link",
    # "a",
    # 嵌入内容
    "iframe",
    "embed",
    "object",
    "param",
    # 技术标记
    "time",
    "data",
    "meter",
    "progress",
    # XML相关
    "xml",
    "xmlns",
    # EPUB特有标签
    "epub:switch",
    "epub:case",
    "epub:default",
    # 注释标签
    "annotation",
    "note",
}


class HTMLProcessor:
    """Main class for processing HTML content."""

    # 内联标签集合
    INLINE_TAGS = {"em", "strong", "span", "a", "i", "b", "sub", "sup"}

    def __init__(
        self, translator, source_lang="en", target_lang="zh", max_chunk_size: int = 4500
    ):
        """
        初始化HTML处理器.

        Args:
            max_chunk_size: 单个翻译块的最大大小，默认4500字符
        """
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_chunk_size = max_chunk_size
        self.placeholder_counter = 0
        self.placeholders = {}

    def cleanup(self):
        self.placeholder_counter = 0
        self.placeholders: Dict[str, str] = {}

    def create_placeholder(self, content: str) -> str:
        """
        为内容创建占位符.

        Args:
            content: 需要替换为占位符的内容

        Returns:
            str: 生成的占位符
        """
        placeholder = f"†{self.placeholder_counter}†"
        self.placeholders[placeholder] = content
        self.placeholder_counter += 1
        return placeholder

    def create_node_separator(self) -> str:
        """创建节点分隔符"""
        separator = f"‡{self.placeholder_counter}‡"
        self.placeholder_counter += 1
        return separator

    def replace_skip_tags_recursive(self, node: Tag) -> None:
        """
        递归替换HTML中的不可翻译标签为占位符.

        Args:
            node: 当前HTML节点
        """
        if not node or isinstance(node, NavigableString):
            return

        for child in list(
            node.children
        ):  # 使用 list(node.children) 避免动态修改 children 导致遍历问题
            if isinstance(child, Tag):
                # 如果当前节点是 SKIP_TAGS，则替换为占位符
                if child.name in SKIP_TAGS:
                    placeholder = self.create_placeholder(str(child))
                    child.replace_with(placeholder)
                else:
                    # 递归处理子节点
                    self.replace_skip_tags_recursive(child)

    def _needs_translation(self, content: str) -> bool:
        """
        检查内容是否需要翻译.

        Args:
            content: 要检查的内容

        Returns:
            bool: 如果内容需要翻译返回True，否则返回False
        """
        # 1. 移除所有占位符
        content_without_placeholders = re.sub(r"†\d+†", "", content)
        # 2. 移除所有内联标签标记
        content_without_markers = re.sub(
            r"‹\d+›|‹/\d+›", "", content_without_placeholders
        )

        # 3. 解析 HTML 并获取纯文本
        soup = BeautifulSoup(content_without_markers, "lxml")
        text = soup.get_text().strip()

        # 如果纯文本为空，说明不需要翻译
        return bool(text)

    async def _group_nodes(self, node: Tag, parent_tags: list = []) -> list:
        """分组处理节点，返回分组结果"""
        if parent_tags is None:
            parent_tags = []

        groups = []
        current_group = []
        current_tokens = 0

        async def process_node_and_siblings(nodes, current_tags):
            """处理节点及其兄弟节点"""
            nonlocal current_group, current_tokens, groups

            for node in nodes:
                if isinstance(node, Comment):
                    # 注释节点，保持原样
                    continue
                elif isinstance(node, NavigableString):
                    if not node.strip():
                        continue
                    # 处理文本节点
                    separator = self.create_node_separator()
                    text = f"{separator}{node}{separator}"
                    tokens = self.translator._count_tokens(text)

                    if current_tokens + tokens <= self.translator.limit_value:
                        current_group.append({
                            "node": node,
                            "separator": separator,
                            "parent_tags": current_tags.copy(),
                        })
                        current_tokens += tokens
                    else:
                        if current_group:
                            groups.append(current_group)
                        current_group = [{
                            "node": node,
                            "separator": separator,
                            "parent_tags": current_tags.copy(),
                        }]
                        current_tokens = tokens
                elif isinstance(node, Tag):
                    # 对于标签节点，递归处理其子节点
                    if node.name in SKIP_TAGS:
                        continue
                        
                    current_tags.append(node.name)
                    await process_node_and_siblings(node.children, current_tags)
                    current_tags.pop()

        # 开始处理节点
        if isinstance(node, Tag):
            parent_tags.append(node.name)
            await process_node_and_siblings(node.children, parent_tags)
            parent_tags.pop()
        else:
            await process_node_and_siblings([node], parent_tags)

        # 添加最后一组
        if current_group:
            groups.append(current_group)

        return groups

    async def _translate_group(self, group: list) -> str:
        if not group:
            return ""

        text_to_translate = ""
        inline_tags_map = {}
        # 使用唯一的分隔符
        for i, item in enumerate(group):
            node = item["node"]
            if isinstance(node, Comment):
                # 注释节点，跳过处理
                continue
            elif isinstance(node, Tag):
                processed_content, tags = self._handle_inline_tags(node)
                inline_tags_map.update(tags)
            else:
                # 如果是普通文本节点，直接使用其内容
                processed_content = str(node)
            
            # 使用索引作为唯一标识符
            separator = f"‹GROUP_{i}›"
            item["separator"] = separator  # 更新 item 的 separator
            text_to_translate += f"{separator}{processed_content}{separator}\n"

        # 调用翻译服务
        translated = await self.translator.translate(
            text_to_translate,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

        return self._restore_inline_tags(translated, inline_tags_map)

    async def _process_translated_groups(self, groups: list) -> None:
        """处理翻译后的文本组"""
        for group in groups:
            translated = await self._translate_group(group)
            
            for item in group:
                node = item["node"]
                separator = item["separator"]  # 现在每个 item 都有唯一的 separator
                
                pattern = f"{re.escape(separator)}(.*?){re.escape(separator)}"
                match = re.search(pattern, translated, re.DOTALL)
                if match:
                    translated_content = match.group(1)
                    # 如果是文本节点，直接替换
                    if isinstance(node, NavigableString):
                        node.replace_with(NavigableString(translated_content))
                    else:
                        # 如果是标签节点，更新第一个文本节点
                        for content in node.contents:
                            if isinstance(content, NavigableString):
                                content.replace_with(NavigableString(translated_content))
                                break

    async def process_node(self, node: Tag) -> None:
        """入口函数，处理HTML节点"""
        if isinstance(node, NavigableString):
            # 处理纯文本节点
            content = str(node)
            if content.strip():
                # 创建新的文本节点并替换
                new_string = NavigableString(content)
                node.replace_with(new_string)
            return

        if not isinstance(node, Tag):
            return

        if node.name in SKIP_TAGS:
            return

        # 调用分组处理
        groups = await self._group_nodes(node)
        # 处理翻译结果
        await self._process_translated_groups(groups)

    async def process(self, html_content: str, parser="lxml") -> str:
        """处理HTML内容，生成翻译任务列表.

        Args:
            html_content: HTML内容字符串

        Returns:
            str: 还原占位符后的文本
        """
        # 解析HTML
        soup = BeautifulSoup(html_content, parser)

        # 如果是 HTML 文档，只处理 body 内容
        body = soup.find("body")
        if body:
            root = body
        else:
            # 如果不是 HTML 文档（比如 ncx），则处理整个文档
            root = soup.find("ncx") or soup.find("package")
            if not root:
                root = soup

        # 如果没有任何内容，将纯文本作为一个任务
        if not root:
            translated_text = await self.translator.translate(
                html_content,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            return html.unescape(translated_text)

        # 第一阶段：替换所有skip标签
        self.replace_skip_tags_recursive(root)  # type: ignore

        # 第二阶段：处理剩余内容，生成翻译结果
        await self.process_node(root)  # type: ignore

        # 还原占位符内容
        translated_html = str(root)
        translated_html = await self.restore_content(translated_html)

        # 解除HTML转义并清理结果
        translated_html = self._clean_translation_result(html.unescape(translated_html))
        return translated_html

    async def restore_content(self, translated_text: str) -> str:
        """
        还原占位符内容.

        Args:
            translated_text: 翻译后的文本

        Returns:
            str: 还原占位符后的文本
        """
        result = translated_text
        pattern = r"†(\d+)†"

        for match in re.finditer(pattern, result):
            placeholder = match.group(0)
            if placeholder in self.placeholders:
                result = result.replace(placeholder, self.placeholders[placeholder])

        return result

    def _clean_translation_result(self, text: str) -> str:
        """清理翻译结果中的代码标记.

        Args:
            text: 翻译结果文本

        Returns:
            清理后的文本
        """
        text = text.strip()
        # 处理开头的代码块标记
        # 可能的格式：```html、```xml、```、等
        if text.startswith("```"):
            # 找到第一个换行符
            first_newline = text.find("\n")
            if first_newline != -1:
                # 移除开头到第一个换行符之间的内容
                text = text[first_newline + 1 :]
            else:
                # 如果没有换行符，说明整个文本就是一个标记
                text = text[3:]

        # 处理结尾的代码块标记
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]

        return text.strip()

    def _handle_inline_tags(self, node: Tag) -> Tuple[str, Dict[int, Dict[str, Any]]]:
        """处理内联标签，返回处理后的文本和标签映射"""
        inline_tags = {}
        counter = 0
        text_content = str(node.get_text())  # 获取纯文本内容

        # 收集内联标签信息但不修改原始节点
        for tag in node.find_all(self.INLINE_TAGS):
            start_marker = f"‹{counter}›"
            end_marker = f"‹/{counter}›"
            # 保存标签和它的位置信息
            inline_tags[counter] = {
                'tag': tag,
                'text': tag.get_text()
            }
            # 在文本内容中添加标记
            text_content = text_content.replace(tag.get_text(), f"{start_marker}{tag.get_text()}{end_marker}")
            counter += 1

        return text_content, inline_tags

    def _restore_inline_tags(self, content: str, inline_tags: Dict[int, Dict[str, Any]]) -> str:
        """还原内联标签，使用原始标签"""
        # 按标记号从大到小还原，避免嵌套标记的干扰
        for i in sorted(inline_tags.keys(), reverse=True):
            start_marker = f"‹{i}›"
            end_marker = f"‹/{i}›"
            tag_info = inline_tags[i]
            original_tag = tag_info['tag']

            # 构建正则表达式匹配标记及其内容
            pattern = f"{re.escape(start_marker)}(.*?){re.escape(end_marker)}"
            
            # 还原标签，直接使用原始标签
            def replace(match):
                inner_content = match.group(1)
                original_tag.string = inner_content
                return str(original_tag)

            content = re.sub(pattern, replace, content, flags=re.DOTALL)

        return content
