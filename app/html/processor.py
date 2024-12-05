"""
HTML processor module.
Handles HTML content processing and transformation.
"""

import re
from typing import Dict, List

from bs4 import BeautifulSoup, NavigableString, Tag

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
    "a",
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


class HTMLContentProcessor:
    """Main class for processing HTML content."""

    def __init__(self, max_chunk_size: int = 4500):
        """
        初始化HTML处理器.

        Args:
            max_chunk_size: 单个翻译块的最大大小，默认4500字符
        """
        self.max_chunk_size = max_chunk_size
        self.placeholder_counter = 0
        self.placeholders: Dict[str, str] = {}

    def _create_placeholder(self, content: str) -> str:
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

    def _process_content(self, node: Tag, tasks: List[Dict]) -> None:
        """
        递归处理HTML内容，生成翻译任务.
        如果节点内容长度（包括HTML标签）小于限制，整个节点作为一个任务；
        否则递归处理其子节点.

        Args:
            node: BeautifulSoup节点
            tasks: 翻译任务列表
        """
        if not node or isinstance(node, NavigableString):
            return

        # 获取完整的HTML内容（包括标签）
        content = str(node)

        # 如果是空节点，跳过
        if not node.get_text().strip():
            return

        # 如果内容在长度限制内，或者没有子节点，作为一个任务
        if len(content) <= self.max_chunk_size or not any(
            isinstance(child, Tag) for child in node.children
        ):
            tasks.append({"content": content, "node": node})
        # 否则递归处理子节点
        else:
            for child in node.children:
                if isinstance(child, Tag):
                    self._process_content(child, tasks)

    async def process_html(self, html_content: str) -> List[Dict]:
        """
        处理HTML内容，生成翻译任务列表.

        Args:
            html_content: HTML内容字符串

        Returns:
            List[Dict]: 翻译任务列表，每个任务包含content和node
        """
        # 重置状态
        self.placeholder_counter = 0
        self.placeholders.clear()

        # 处理空内容
        if not html_content.strip():
            return []

        # 解析HTML
        soup = BeautifulSoup(html_content, "html.parser")
        root = soup.find()

        # 如果没有HTML标签，将纯文本作为一个任务
        if not root:
            tasks = [{"content": html_content, "node": soup}]
            return tasks

        try:
            # 第一阶段：替换所有skip标签
            while True:
                skip_tag = soup.find(SKIP_TAGS)
                if not skip_tag:
                    break
                placeholder = self._create_placeholder(str(skip_tag))
                skip_tag.replace_with(placeholder)

            # 第二阶段：处理剩余内容，生成翻译任务
            tasks = []
            self._process_content(soup, tasks)
            return tasks

        except Exception as e:
            # 只处理预期的异常
            if isinstance(e, (ValueError, AttributeError)):
                raise ValueError(f"Invalid HTML structure: {str(e)}")
            raise  # 其他异常直接抛出

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
