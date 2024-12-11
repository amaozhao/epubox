"""
HTML processor module.
Handles HTML content processing and transformation.
"""

import asyncio
import html
import re
from typing import Dict, List

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

    # 类级别的信号量，限制并发翻译请求
    _translation_semaphore = asyncio.Semaphore(1)

    def __init__(
        self, translator, source_lang="en", target_lang="zh", max_chunk_size: int = 4500
    ):
        """
        初始化HTML处理器.

        Args:
            max_chunk_size: 单个翻译块的最大token数，默认4500 tokens
        """
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_chunk_size = max_chunk_size
        self.placeholder_counter = 0
        self.placeholders: Dict[str, str] = {}

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

    async def _handle_skip_tag(self, node: Tag) -> None:
        """处理需要跳过的标签。"""
        placeholder = self.create_placeholder(str(node))
        node.replace_with(placeholder)

    async def _handle_text_node(self, node: NavigableString) -> None:
        """处理纯文本节点。"""
        text = str(node).strip()
        text_without_placeholders = re.sub(r"†\d+†", "", text)
        if text_without_placeholders.strip():
            async with HTMLProcessor._translation_semaphore:
                translated_text = await self.translator.translate(
                    str(node),
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                )
            node.replace_with(translated_text)

    def _collect_child_info(self, node: Tag) -> List[Dict]:
        """收集节点的子节点信息。"""
        child_info = []
        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if not re.sub(r"†\d+†", "", text).strip():
                    continue
            child_info.append(
                {"node": child, "tokens": self.translator._count_tokens(str(child))}
            )
        return child_info

    async def _translate_node_directly(self, node: Tag, content: str) -> None:
        """直接翻译整个节点。"""
        async with HTMLProcessor._translation_semaphore:
            translated_text = await self.translator.translate(
                content,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            node.replace_with(BeautifulSoup(translated_text, "html.parser"))

    async def _process_child_groups(self, child_info: List[Dict]) -> None:
        """处理子节点分组。"""
        current_group = []
        current_tokens = 0

        for info in child_info:
            child_tokens = info["tokens"]

            # 如果当前子节点过大，需要递归处理
            if child_tokens > self.max_chunk_size:
                # 先处理之前的组
                if current_group:
                    await self._translate_group(current_group)
                    current_group = []
                    current_tokens = 0
                # 递归处理大节点
                await self.process_node(info["node"])
                continue

            # 尝试添加到当前组
            if current_tokens + child_tokens <= self.max_chunk_size:
                current_group.append(info["node"])
                current_tokens += child_tokens
            else:
                # 处理当前组
                if current_group:
                    await self._translate_group(current_group)
                # 开始新组
                current_group = [info["node"]]
                current_tokens = child_tokens

        # 处理最后一组
        if current_group:
            await self._translate_group(current_group)

    async def _translate_group(self, nodes: List[PageElement]) -> None:
        """翻译节点组。"""
        # 合并节点内容
        merged_content = "".join(str(node) for node in nodes)

        # 翻译
        async with HTMLProcessor._translation_semaphore:
            translated_content = await self.translator.translate(
                merged_content,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )

        # 解析翻译结果
        translated_soup = BeautifulSoup(translated_content, "html.parser")
        translated_nodes = list(translated_soup.children)

        # 替换原始节点
        if len(translated_nodes) == len(nodes):
            for original, translated in zip(nodes, translated_nodes):
                original.replace_with(translated)
        else:
            # 如果节点数量不匹配，整体替换
            nodes[0].parent.replace(nodes[0], translated_soup)
            for node in nodes[1:]:
                node.decompose()

    async def process_node(self, node: PageElement) -> None:
        """
        递归处理HTML节点，检查长度并调用翻译接口替换内容。

        Args:
            node: 当前HTML节点
        """
        if not node:
            return

        # 1. 处理跳过标签
        if isinstance(node, Tag) and node.name in SKIP_TAGS:
            await self._handle_skip_tag(node)
            return

        # 2. 处理文本节点
        if isinstance(node, NavigableString):
            await self._handle_text_node(node)
            return

        # 3. 检查当前节点的总token数
        content = str(node)
        total_tokens = self.translator._count_tokens(content)

        # 如果节点足够小，直接翻译
        if total_tokens <= self.max_chunk_size:
            await self._translate_node_directly(node, content)
            return

        # 4. 收集并处理子节点信息
        child_info = self._collect_child_info(node)
        await self._process_child_groups(child_info)

    async def process(self, html_content: str, parser="html.parser") -> str:
        """
        处理HTML内容，生成翻译任务列表.

        Args:
            html_content: HTML内容字符串

        Returns:
            List[Dict]: 翻译任务列表，每个任务包含content和node
        """
        self.cleanup()

        # 处理空内容
        if not html_content.strip():
            return html_content

        # 解析HTML
        soup = BeautifulSoup(html_content, parser)
        root = soup.find()

        # 如果没有HTML标签，将纯文本作为一个任务
        if not root:
            translated_text = await self.translator.translate(
                text=html_content,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
            return html.unescape(translated_text)

        # 第一阶段：替换所有skip标签
        self.replace_skip_tags_recursive(root)

        # 第二阶段：处理剩余内容，生成翻译结果
        await self.process_node(root)

        # 还原占位符内容
        translated_html = str(root)
        translated_html = self.restore_content(translated_html)

        # 解除HTML转义
        return html.unescape(translated_html)

    def restore_content(self, translated_text: str) -> str:
        """
        还原占位符内容.

        Args:
            translated_text: 翻译后的文本

        Returns:
            str: 还原占位符后的文本
        """
        # 清理可能的额外文本
        # 如果文本以翻译说明开头，尝试找到实际的翻译内容
        text_lines = translated_text.split("\n")
        cleaned_lines = []
        for line in text_lines:
            # 跳过可能的解释性文本
            if any(
                skip in line.lower()
                for skip in ["翻译:", "翻译：", "translation:", "译文:", "译文："]
            ):
                continue
            # 跳过空行
            if not line.strip():
                continue
            cleaned_lines.append(line)

        result = "\n".join(cleaned_lines)
        pattern = r"†(\d+)†"

        # 还原所有占位符
        for match in re.finditer(pattern, result):
            placeholder = match.group(0)
            if placeholder in self.placeholders:
                result = result.replace(placeholder, self.placeholders[placeholder])

        return result
