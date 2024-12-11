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

    async def process_node(self, node: PageElement) -> None:
        """
        递归处理HTML节点，检查长度并调用翻译接口替换内容.

        Args:
            node: 当前HTML节点
        """
        if not node:
            return

        # 1. 如果是 SKIP_TAGS 中的标签，直接替换为占位符并返回
        if isinstance(node, Tag) and node.name in SKIP_TAGS:
            placeholder = self.create_placeholder(str(node))
            node.replace_with(placeholder)
            return

        # 2. 如果是纯文本节点且有实际内容，则翻译
        if isinstance(node, NavigableString):
            text = str(node).strip()
            # 检查是否为空白字符或只包含占位符
            text_without_placeholders = re.sub(r"†\d+†", "", text)
            if (
                text_without_placeholders.strip()
            ):  # 只有当文本非空且不只是占位符时才翻译
                async with HTMLProcessor._translation_semaphore:
                    translated_text = await self.translator.translate(
                        str(node),
                        source_lang=self.source_lang,
                        target_lang=self.target_lang,
                    )
                node.replace_with(translated_text)
            return

        # 3. 对于其他节点（HTML标签），检查是否包含实际文本内容
        content_text = "".join(node.stripped_strings)
        # 移除所有占位符后检查是否还有实际内容
        content_without_placeholders = re.sub(r"†\d+†", "", content_text)
        if not content_without_placeholders.strip():
            # 如果移除占位符后没有实际文本，保持原样
            return

        # 4. 如果节点内容较小且包含文本，检查是否需要整体翻译
        content = str(node)
        # 获取 token 数
        token_count = self.translator._count_tokens(content)
        if token_count <= self.max_chunk_size and len(content_without_placeholders) > 0:
            # 获取节点的所有直接文本内容（不包括子节点的文本）
            direct_text = "".join(
                str(child)
                for child in node.children
                if isinstance(child, NavigableString) and str(child).strip()
            )

            # 检查直接文本是否只包含占位符
            direct_text_without_placeholders = re.sub(r"†\d+†", "", direct_text)
            if (
                direct_text_without_placeholders.strip()
            ):  # 如果节点自身包含非占位符文本，整体翻译
                async with HTMLProcessor._translation_semaphore:
                    translated_text = await self.translator.translate(
                        content,
                        source_lang=self.source_lang,
                        target_lang=self.target_lang,
                    )
                node.replace_with(BeautifulSoup(translated_text, "html.parser"))
                return

        # 5. 如果节点较大或没有直接文本内容，递归处理子节点
        for child in list(node.children):
            await self.process_node(child)

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
