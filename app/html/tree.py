import asyncio
import re
from typing import Optional

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from app.html.attr_processor import AttributeProcessor

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


class TranslatorProvider:
    def __init__(self, limit_value):
        self.limit_value = limit_value
        self.translations = {
            "Preface": "前言",
            "Who this book is for": "本书适合谁",
            "What this book covers": "本书内容",
            "Chapter": "章节",
            "FastAPI": "FastAPI",
            "Python": "Python",
            "API": "API",
            "APIs": "API",
            "RESTful": "RESTful",
            "SQL": "SQL",
            "NoSQL": "NoSQL",
            "MongoDB": "MongoDB",
            "Redis": "Redis",
            "WebSocket": "WebSocket",
            "WebSockets": "WebSocket",
            "OAuth2": "OAuth2",
            "JWT": "JWT",
            "ORM": "ORM",
            "CRUD": "增删改查",
            "LLM": "大语言模型",
            "RAG": "检索增强生成",
        }

    def _count_tokens(self, content: str) -> int:
        """计算文本的token数量，这里使用简单的空格分词。

        Args:
            content: 要计算的文本内容

        Returns:
            token数量
        """
        return len(content)

    async def translate(self, content: str, source_lang: str, target_lang: str) -> str:
        """翻译文本内容。

        Args:
            content: 要翻译的文本
            source_lang: 源语言
            target_lang: 目标语言

        Returns:
            翻译后的文本
        """
        tokens = self._count_tokens(content)

        # 保持特殊词汇的翻译一致性
        result = content
        for en, zh in self.translations.items():
            result = result.replace(en, zh)

        # 将剩余的英文转换为中文（这里用大写模拟翻译效果）
        if result == content:  # 如果没有特殊词汇匹配，才进行通用翻译
            result = content.upper()

        return result


class TreeNode:
    def __init__(
        self,
        node_type: str,
        content: str,
        token_count: int,
        parent: Optional["TreeNode"] = None,
    ):
        self.node_type: str = node_type  # 节点类型：leaf 或 non-leaf
        self.content: str = content  # 节点内容
        self.token_count: int = token_count  # token 数量
        self.parent: Optional[TreeNode] = parent  # 父节点
        self.children: list[TreeNode] = []  # 子节点列表
        self.translated: Optional[str] = None  # 翻译后的内容，仅叶节点使用

    def add_child(self, child: "TreeNode"):
        """添加子节点"""
        self.children.append(child)
        child.parent = self


class TreeProcessor:
    def __init__(
        self,
        translator: TranslatorProvider,
        source_lang: str = "en",
        target_lang: str = "zh",
    ):
        """初始化树处理器。

        Args:
            translator: 翻译器对象
            source_lang: 源语言，默认为英语
            target_lang: 目标语言，默认为中文
        """
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.skip_tags = {
            "script",
            "style",
            "code",
            "pre",
            "link",
        }  # 跳过这些标签的内容
        self.placeholder_counter = 0  # 占位符计数器
        self.root = None
        self.soup = None
        self.placeholders = {}  # 存储占位符到原始内容的映射，键是占位符字符串（如 {1}）
        self.attr_processor = AttributeProcessor()  # 属性处理器

    def reset_state(self) -> None:
        """重置处理器状态，清除所有占位符和计数器。"""
        print(f"重置状态：清除 {len(self.placeholders)} 个占位符")
        self.placeholder_counter = 0
        self.placeholders.clear()
        self.root = None
        self.soup = None
        self.attr_processor.reset()  # 重置属性处理器

    async def process(self, content: str, parser="html.parser") -> str:
        """处理HTML内容。"""
        self.reset_state()
        self.soup = BeautifulSoup(content, parser)

        body = self.soup.find("body")
        if not body:
            return content

        # ===== 解析阶段 =====
        # 1. 首先处理占位符替换
        self.replace_skip_tags_recursive(body)

        # 2. 压缩剩余标签的属性
        for tag in body.find_all(True):
            # 跳过已经被替换为占位符的标签
            if not (
                isinstance(tag.string, NavigableString)
                and re.match(r"\{\d+\}", str(tag.string))
            ):
                self._compress_tag_attrs(tag)

        # 3. 创建树结构
        self.root = TreeNode("root", str(self.soup), 0)

        # 4. 遍历和翻译
        await self._traverse(body, self.root)
        await self._translate_nodes(self.root)

        # ===== 恢复阶段 =====
        # 1. 重建基本HTML结构
        result = self.restore_html(self.root, parser)

        # 2. 解析重建后的HTML
        restored_soup = BeautifulSoup(result, parser)

        # 3. 解压缩所有属性
        for tag in restored_soup.find_all(True):
            # 跳过占位符节点
            if not (
                isinstance(tag.string, NavigableString)
                and re.match(r"\{\d+\}", str(tag.string))
            ):
                self._decompress_tag_attrs(tag)

        # 4. 最后还原占位符
        final_result = self.restore_content(str(restored_soup))

        return final_result

    def _compress_tag_attrs(self, tag: Tag) -> None:
        """压缩标签属性。"""
        if hasattr(tag, "attrs") and tag.attrs:
            tag.attrs = self.attr_processor.compress_attrs(tag.attrs)

    def _decompress_tag_attrs(self, tag: Tag) -> None:
        """解压缩标签属性。"""
        if hasattr(tag, "attrs") and tag.attrs:
            tag.attrs = self.attr_processor.decompress_attrs(tag.attrs)

    async def _traverse(self, node: Tag, parent: TreeNode) -> None:
        """
        递归遍历HTML节点，构建树结构
        """
        # 如果是注释节点，直接跳过
        if isinstance(node, Comment):
            return

        # 如果是文本节点，直接返回，因为我们只处理完整的标签
        if isinstance(node, NavigableString):
            return

        # 获取当前节点的完整HTML内容
        current_html = str(node)
        current_tokens = self.translator._count_tokens(current_html)

        # 如果当前节点的token数量已经超过限制，需要递归处理它的子节点
        if current_tokens > self.translator.limit_value:
            # 创建非叶子节点
            attrs = (
                "".join([f' {k}="{v}"' for k, v in node.attrs.items()])
                if hasattr(node, "attrs")
                else ""
            )
            current = TreeNode(
                node_type="non-leaf",
                content=f"<{node.name}{attrs}>",
                token_count=0,
                parent=parent,
            )
            parent.add_child(current)

            # 递归处理所有子节点
            for child in node.contents:
                await self._traverse(child, current)

            # 添加结束标签
            current.content += f"</{node.name}>"
            return

        # 尝试和前一个叶子节点合并
        if parent.children and parent.children[-1].node_type == "leaf":
            last_leaf = parent.children[-1]
            merged_html = last_leaf.content + current_html
            merged_tokens = self.translator._count_tokens(merged_html)

            if merged_tokens <= self.translator.limit_value:
                # 可以合并
                last_leaf.content = merged_html
                last_leaf.token_count = merged_tokens
                return

        # 不能合并，创建新的叶子节点
        leaf = TreeNode(
            node_type="leaf",
            content=current_html,
            token_count=current_tokens,
            parent=parent,
        )
        parent.add_child(leaf)

    async def _translate_nodes(self, node: TreeNode) -> None:
        """递归翻译所有叶节点。"""
        if not node:
            return

        if node.node_type == "leaf":
            print(f"\n节点类型: {node.node_type}")
            print(f"Token数量: {node.token_count}")
            translated = await self.translator.translate(
                node.content, source_lang=self.source_lang, target_lang=self.target_lang
            )
            translated = self._clean_translation_result(translated)
            node.translated = translated
            print(f"翻译内容: {node.translated[:200]}")
            print("-" * 50)
        else:
            for child in node.children:
                await self._translate_nodes(child)

    def _extract_content(self, html_str: str, parser="html.parser") -> str:
        """提取HTML字符串中的实际内容，去除外层标签。"""
        if not html_str:
            return ""
        soup = BeautifulSoup(html_str, parser)
        body = soup.find("body")
        if body:
            return "".join(str(child) for child in body.contents)
        return html_str

    def _merge_contents(self, tag: Tag, content: str, parser="html.parser") -> None:
        """安全地合并内容到标签中。"""
        if not content.strip():
            return
        new_soup = BeautifulSoup(content, parser)
        if new_soup.contents:
            tag.extend(new_soup.contents)

    def restore_html(self, node: TreeNode, parser="html.parser") -> str:
        """重建HTML内容。"""
        if not node:
            return ""

        # 处理叶子节点
        if node.node_type == "leaf":
            content = node.translated if node.translated else node.content
            # 在叶子节点级别就进行占位符还原
            return self.restore_content(content)

        # 收集子节点内容
        contents = []
        for child in node.children:
            content = self.restore_html(child, parser)
            if content.strip():
                contents.append(self._extract_content(content))

        merged_content = "".join(contents)

        # 处理根节点
        if node.node_type == "root":
            soup = self.soup
            if merged_content:
                body = soup.find("body")
                if body:
                    body.clear()
                    self._merge_contents(body, merged_content, parser)
            return str(soup)

        # 处理普通节点
        if not node.content:
            return merged_content

        # 处理有标签的节点
        if merged_content:
            soup = BeautifulSoup(node.content, parser)
            tag = soup.find()
            if tag:
                tag.clear()
                self._merge_contents(tag, merged_content, parser)
                return str(tag)

        return node.content

    def _update_node_content(self, tag: Tag, node: TreeNode) -> None:
        """更新标签内容。

        Args:
            tag: BeautifulSoup标签
            node: TreeNode节点
        """
        if not node.children:
            return

        # 保存原始属性
        original_attrs = dict(tag.attrs) if hasattr(tag, "attrs") else {}

        # 清空标签内容
        tag.clear()

        # 恢复属性
        tag.attrs.update(original_attrs)

        # 添加所有子节点的内容
        for child in node.children:
            content = self.restore_html(child, "html.parser")
            if content.strip():
                # 解析子节点的HTML内容
                child_soup = BeautifulSoup(content, "html.parser")
                # 将子节点的内容添加到父节点
                for element in child_soup.contents:
                    tag.append(element)

    def replace_skip_tags_recursive(self, node: Tag) -> None:
        """
        递归替换HTML中的不可翻译标签为占位符.

        Args:
            node: 当前处理的节点
        """
        for child in list(node.contents):
            if isinstance(child, Tag):
                if child.name in SKIP_TAGS:
                    placeholder = self._generate_placeholder(child)
                    child.replace_with(placeholder)
                else:
                    self.replace_skip_tags_recursive(child)

    def _generate_placeholder(self, node: Tag) -> str:
        """生成占位符，并存储占位符到原始内容的映射.

        Args:
            node: 需要替换的标签

        Returns:
            占位符字符串
        """
        self.placeholder_counter += 1
        placeholder = f"{{{self.placeholder_counter}}}"
        self.placeholders[placeholder] = str(node)
        print(f"生成占位符：{placeholder} -> {node.name} 标签")
        return placeholder

    def restore_content(self, content: str) -> str:
        """
        还原占位符内容，保留原始的 HTML 标签。

        Args:
            content: 包含占位符的内容

        Returns:
            str: 还原后的内容
        """
        if not content:
            return content

        # 使用正则表达式替换所有占位符
        def replace_placeholder(match):
            placeholder = match.group(0)  # 获取完整的占位符
            if placeholder not in self.placeholders:
                print(f"警告：找不到占位符 {placeholder} 的原始内容")
                return placeholder
            print(f"还原占位符：{placeholder}")
            return self.placeholders[placeholder]

        # 循环替换直到没有更多占位符
        prev_content = None
        result = content
        while prev_content != result:
            prev_content = result
            result = re.sub(r"\{(\d+)\}", replace_placeholder, result)

        remaining_placeholders = re.findall(r"\{(\d+)\}", result)
        if remaining_placeholders:
            print(
                f"警告：还有 {len(remaining_placeholders)} 个占位符未被还原：{remaining_placeholders}"
            )

        return result

    def _clean_translation_result(self, text: str) -> str:
        """清理翻译结果中的代码标记.

        Args:
            text: 翻译结果文本

        Returns:
            清理后的文本
        """
        if not text:
            return text

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


if __name__ == "__main__":

    async def main():
        with open(
            "/Users/amaozhao/workspace/epubox/app/html/B21025_FM.xhtml", "r"
        ) as f:
            content = f.read()
        processor = TreeProcessor(
            translator=TranslatorProvider(limit_value=4000),
            source_lang="en",
            target_lang="zh",
        )
        result = await processor.process(content)
        with open("/Users/amaozhao/workspace/epubox/app/html/restored.xhtml", "w") as f:
            f.write(result)

    asyncio.run(main())
