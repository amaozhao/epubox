"""
HTML processor module.
Handles HTML content processing and transformation.
"""

import copy
import html
import re
from typing import Dict, List, Tuple, Union

from bs4 import BeautifulSoup, NavigableString, PageElement, Tag

# 不需要翻译的标签集合
SKIP_TAGS = {
    # 脚本和样式
    "script",
    "style",
    # 代码相关
    "code",
    # "pre",
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

    def replace_skip_tags_recursive(self, node: Tag) -> None:
        """
        递归替换HTML中的不可翻译标签为占位符.

        Args:
            node: 当前HTML节点
        """
        if not node or isinstance(node, NavigableString):
            return

        # 遍历当前节点的子节点
        if node.name == "head":
            # 特别处理 head 标签
            for child in list(node.children):
                if isinstance(child, Tag):
                    if child.name in {"meta", "link"}:  # 只替换 meta 和 link 标签
                        placeholder = self.create_placeholder(str(child))
                        child.replace_with(placeholder)
                    else:
                        # 递归处理其他标签（比如 title）
                        self.replace_skip_tags_recursive(child)
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

    def _group_nodes(self, node: Tag) -> List[List[PageElement]]:
        """
        智能分组节点，将相邻的可翻译节点组合在一起。

        Args:
            node: 当前HTML节点

        Returns:
            List[List[PageElement]]: 分组后的节点列表
        """
        groups = []
        current_group = []
        current_content = ""

        for child in node.children:
            # 跳过空白文本节点
            if isinstance(child, NavigableString) and not child.strip():
                continue

            # 检查节点是否需要翻译
            content = str(child)
            if not self._needs_translation(content):
                continue

            # 检查当前组加上新内容是否超过限制
            test_content = current_content + content if current_content else content
            tokens = self.translator._count_tokens(test_content)
            if tokens <= self.translator.limit_value:
                current_group.append(child)
                current_content = test_content
            else:
                if current_group:
                    groups.append(current_group)
                current_group = [child]
                current_content = content

        # 添加最后一个组
        if current_group:
            groups.append(current_group)

        return groups

    async def _translate_group(self, nodes: List[Union[Tag, NavigableString]]) -> None:
        """翻译节点组"""
        if not nodes:
            return

        # 1. 处理每个节点
        processed_contents = []
        all_inline_tags = {}
        need_recursive = False

        for node in nodes:
            if isinstance(node, NavigableString):
                processed_contents.append(str(node))
            else:
                # 检查节点是否太大
                content = str(node)
                tokens = self.translator._count_tokens(content)
                if tokens > self.translator.limit_value:
                    # 如果节点太大，标记需要递归处理
                    need_recursive = True
                    break
                # 创建新的 soup 对象处理当前节点
                node_soup = BeautifulSoup(str(node), "lxml")
                content, inline_tags = self._handle_inline_tags(str(node), node_soup)
                all_inline_tags.update(inline_tags)
                processed_contents.append(content)

        # 如果需要递归处理
        if need_recursive:
            for node in nodes:
                if isinstance(node, Tag):
                    await self.process_node(node)
            return

        # 2. 合并处理后的内容
        merged_content = "".join(processed_contents)

        # 3. 翻译处理后的内容
        translated_content = await self.translator.translate(
            merged_content,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
        )

        # 4. 还原内联标签
        restored_content = self._restore_inline_tags(
            translated_content, all_inline_tags
        )

        # 5. 更新原始节点
        if len(nodes) == 1:
            nodes[0].clear()  # type: ignore
            nodes[0].append(BeautifulSoup(restored_content, "lxml"))
        else:
            # 如果有多个节点，用第一个节点替换所有内容
            nodes[0].replace_with(BeautifulSoup(restored_content, "lxml"))
            for node in nodes[1:]:
                node.decompose()  # type: ignore

    async def process_node(self, node: PageElement) -> None:
        """
        处理HTML节点，检查长度并调用翻译接口替换内容.

        Args:
            node: 当前HTML节点
        """
        if isinstance(node, NavigableString):
            if self._needs_translation(str(node)):
                translated_text = await self.translator.translate(
                    str(node),
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                )
                translated_text = self._clean_translation_result(translated_text)

                # 还原占位符内容
                restored_text = await self.restore_content(translated_text)

                # 解析还原后的内容
                node.replace_with(BeautifulSoup(restored_text, "lxml"))
            return

        if not isinstance(node, Tag):
            return

        # 如果是 SKIP_TAGS，则跳过
        if node.name in SKIP_TAGS:
            return

        # 特别处理 head 标签
        if node.name == "head":
            # 保留 head 标签的属性
            attrs = node.attrs
            # 创建新的 head 标签
            new_head = BeautifulSoup(features="lxml").new_tag("head")
            # 复制原始属性
            for key, value in attrs.items():
                new_head[key] = value
            # 替换原始节点
            node.replace_with(new_head)
            return

        # 获取节点的文本内容
        content = str(node)

        if not content.strip():
            return

        # 计算token数量
        tokens = self.translator._count_tokens(content)
        if tokens <= self.translator.limit_value:
            # token 数量合适，直接翻译
            translated_text = await self.translator.translate(
                content,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            translated_text = self._clean_translation_result(translated_text)

            # 还原占位符内容
            restored_text = await self.restore_content(translated_text)

            # 解析还原后的内容
            node.clear()
            node.append(BeautifulSoup(restored_text, "lxml"))
            return

        # token 超限，使用分组翻译
        groups = self._group_nodes(node)
        for group in groups:
            await self._translate_group(group)  # type: ignore

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

    def _handle_inline_tags(
        self, content: str, soup: BeautifulSoup
    ) -> Tuple[str, Dict[int, Tag]]:
        """处理内联标签，返回处理后的内容和标签映射"""
        inline_tags = {}  # 存储 marker -> tag 的映射
        counter = 0

        # 处理所有内联标签
        for tag in soup.find_all(self.INLINE_TAGS):
            # 生成开始和结束标记
            start_marker = f"‹{counter}›"
            end_marker = f"‹/{counter}›"

            # 保存标签信息
            inline_tags[counter] = tag

            # 替换标签为标记，但保留内容
            tag.insert_before(start_marker)
            tag.insert_after(end_marker)
            tag.unwrap()  # 移除标签但保留内容

            counter += 1

        return str(soup), inline_tags

    def _restore_inline_tags(self, content: str, inline_tags: Dict[int, Tag]) -> str:
        """还原内联标签"""
        # 按标记号从大到小还原，避免嵌套标记的干扰
        for i in sorted(inline_tags.keys(), reverse=True):
            start_marker = f"‹{i}›"
            end_marker = f"‹/{i}›"
            tag = inline_tags[i]

            # 构建正则表达式匹配标记及其内容
            pattern = f"{re.escape(start_marker)}(.*?){re.escape(end_marker)}"

            # 还原标签
            def replace(match):
                inner_content = match.group(1)
                new_tag = copy.copy(tag)  # 复制原标签保留属性
                new_tag.string = inner_content
                return str(new_tag)

            content = re.sub(pattern, replace, content, flags=re.DOTALL)

        return content

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
        await self.process_node(root)

        # 还原占位符内容
        translated_html = str(root)
        translated_html = await self.restore_content(translated_html)

        # 根据不同类型的文档处理结果
        if body and body == root:
            # 如果是 body 节点，需要把内容放回原始的 HTML 结构中
            body.replace_with(BeautifulSoup(translated_html, parser).body)  # type: ignore
            translated_html = str(soup)
        elif root.name in ["ncx", "package"]:
            # 如果是 ncx 或 package 文档，保留原始结构
            new_root = BeautifulSoup(translated_html, parser).find(root.name)
            if new_root:
                root.replace_with(new_root)
                translated_html = str(soup)

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
