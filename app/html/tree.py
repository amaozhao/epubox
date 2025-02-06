import asyncio
import html
from bs4 import BeautifulSoup, Tag, NavigableString, Comment
from typing import Optional


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
            "RAG": "检索增强生成"
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
    def __init__(self, node_type: str, content: str, token_count: int, parent: Optional['TreeNode'] = None):
        self.node_type: str = node_type  # 节点类型：leaf 或 non-leaf
        self.content: str = content  # 节点内容
        self.token_count: int = token_count  # token 数量
        self.parent: Optional[TreeNode] = parent  # 父节点
        self.children: list[TreeNode] = []  # 子节点列表
        self.translated: Optional[str] = None  # 翻译后的内容，仅叶节点使用
        
    def add_child(self, child: 'TreeNode'):
        """添加子节点"""
        self.children.append(child)
        child.parent = self


class TreeProcessor:
    def __init__(self, translator: TranslatorProvider, source_lang: str = "en", target_lang: str = "zh"):
        """初始化树处理器。

        Args:
            translator: 翻译器对象
            source_lang: 源语言，默认为英语
            target_lang: 目标语言，默认为中文
        """
        self.translator = translator
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.skip_tags = {'script', 'style', 'code', 'pre', 'link'}  # 跳过这些标签的内容
        self.placeholder_counter = 0  # 占位符计数器
        self.root = None
        self.soup = None
        self.placeholders = {}

    async def process(self, content: str, parser='html.parser') -> None:
        """处理HTML内容。
        
        Args:
            content: HTML内容字符串
        """
        print("原始内容：", content[:200])  # 输出原始内容
        self.root = TreeNode(node_type='root', content=content, token_count=0)
        self.soup = BeautifulSoup(content, parser)
        self.replace_skip_tags_recursive(self.soup)
        
        # 根据解析器类型找到根标签
        body = self.soup.find('body')
        root_tag = body if body else self.soup
        
        # 递归处理所有节点
        await self._traverse(root_tag, self.root)
        
        # 翻译所有叶节点
        await self._translate_nodes(self.root)
        
        # 输出处理后的内容
        result = self.restore_html(self.root, parser)
        print("处理后内容：", result[:200])

    async def _traverse(self, node, parent: Optional[TreeNode] = None) -> None:
        """递归遍历 HTML 节点，合并和处理文本节点。"""
        if isinstance(node, str):
            return

        # 收集并合并当前节点下的所有文本
        merged_nodes = self._collect_mergeable_nodes(node)
        
        if merged_nodes:
            for text, tokens in merged_nodes:
                leaf = TreeNode(
                    node_type='leaf',
                    content=text,
                    token_count=tokens,
                    parent=parent
                )
                parent.add_child(leaf)
        else:
            # 如果没有文本节点，继续处理子节点
            for child in node.children:
                if isinstance(child, Tag):
                    child_node = TreeNode(
                        node_type='non-leaf',
                        content=str(child),
                        token_count=len(str(child)),
                        parent=parent
                    )
                    parent.add_child(child_node)
                    await self._traverse(child, child_node)

    def _collect_mergeable_nodes(self, node) -> list[tuple[str, int]]:
        """收集并合并文本节点，确保最大化合并同时不超过token限制。
        
        Args:
            node: 当前节点
            
        Returns:
            list of (text, tokens) tuples，每个tuple的tokens都不超过limit_value
        """
        # 先收集所有文本节点
        all_text_nodes = []
        
        def collect_text(node) -> None:
            if isinstance(node, NavigableString) and not isinstance(node, Comment):
                text = str(node).strip()
                if text:
                    # 先计算单个节点的 token 数
                    tokens = self.translator._count_tokens(text)
                    # 获取父节点的标签名和属性
                    parent_tag = node.parent
                    if parent_tag and isinstance(parent_tag, Tag):
                        # 创建一个新的标签包含文本
                        new_tag = self.soup.new_tag(parent_tag.name, **parent_tag.attrs)
                        new_tag.string = text
                        all_text_nodes.append((str(new_tag), tokens))
                    else:
                        all_text_nodes.append((text, tokens))
            elif isinstance(node, Tag):
                if node.name in self.skip_tags:
                    # 如果是需要跳过的标签，保存整个标签
                    all_text_nodes.append((str(node), 0))
                else:
                    # 否则递归处理子节点
                    for child in node.children:
                        collect_text(child)
        
        collect_text(node)
        
        if not all_text_nodes:
            return []
            
        result = []
        current_texts = []
        current_tokens = 0
        
        for text, tokens in all_text_nodes:
            # 如果是需要跳过的标签，直接添加
            if any(f"<{tag}" in text for tag in self.skip_tags):
                if current_texts:
                    # 保存当前累积的节点
                    merged_text = ' '.join(current_texts)
                    result.append((merged_text, current_tokens))
                    # 重置状态
                    current_texts = []
                    current_tokens = 0
                # 添加标签内容
                result.append((text, 0))
                continue
            
            # 先检查合并后是否会超过限制
            test_tokens = self.translator._count_tokens(' '.join(current_texts + [text]))
            
            if test_tokens > self.translator.limit_value:
                if current_texts:
                    # 保存当前累积的节点
                    merged_text = ' '.join(current_texts)
                    result.append((merged_text, current_tokens))
                    # 重置状态
                    current_texts = [text]
                    current_tokens = tokens
            else:
                # 可以安全合并
                current_texts.append(text)
                current_tokens = test_tokens
        
        # 处理剩余节点
        if current_texts:
            merged_text = ' '.join(current_texts)
            final_tokens = self.translator._count_tokens(merged_text)
            result.append((merged_text, final_tokens))
        
        return result

    async def _translate_nodes(self, node: TreeNode) -> None:
        """递归翻译所有叶节点。"""
        if not node:
            return
            
        if node.node_type == 'leaf':
            print(f"\n节点类型: {node.node_type}")
            print(f"原始内容: {node.content}")
            translated = await self.translator.translate(
                node.content,
                source_lang=self.source_lang,
                target_lang=self.target_lang
            )
            translated = self._clean_translation_result(translated)
            node.translated = translated
            print(f"翻译内容: {node.translated}")
            print("-" * 50)
        else:
            for child in node.children:
                await self._translate_nodes(child)

    def replace_skip_tags_recursive(self, node: Tag) -> None:
        """
        递归替换HTML中的不可翻译标签为占位符.

        Args:
            node: 当前处理的节点
        """
        for child in list(node.children):
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
        placeholder = f"{{{self.placeholder_counter}}}"
        self.placeholders[placeholder] = str(node)
        self.placeholder_counter += 1
        return placeholder

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

    def restore_html(self, node: TreeNode, parser='html.parser') -> str:
        """根据树结构恢复HTML。
        
        Args:
            node: 树节点
            parser: HTML解析器类型
            
        Returns:
            恢复后的HTML内容
        """
        if not node:
            return ""
        
        # 如果是根节点，直接使用原始内容作为模板
        if node.node_type == 'root':
            soup = BeautifulSoup(node.content, parser)
            root_tag = soup.find('body') if soup.find('body') else soup
            
            # 递归更新每个标签的文本内容
            self._update_node_content(root_tag, node, parser)
            return html.unescape(str(soup))
            
        # 处理叶节点
        if node.node_type == 'leaf':
            content = node.translated if node.translated else node.content
            return self.restore_content(content)
            
        # 处理非叶节点
        soup = BeautifulSoup(node.content, parser)
        top_tag = soup.find()  # 获取第一个标签
        if not top_tag:
            return node.content
            
        # 递归更新标签内容
        self._update_node_content(top_tag, node, parser)
        return str(soup)

    def _update_node_content(self, tag: Tag, node: TreeNode, parser='html.parser') -> None:
        """递归更新标签的内容.
        
        Args:
            tag: BeautifulSoup标签
            node: TreeNode节点
        """
        
        # 如果节点有子节点，递归处理
        if node.children:
            # 保存原始属性
            original_attrs = dict(tag.attrs) if hasattr(tag, 'attrs') else {}
            
            # 清空标签内容，准备重新填充
            tag.clear()
            
            # 恢复原始属性
            tag.attrs.update(original_attrs)
            
            # 处理每个子节点
            for child_node in node.children:
                if child_node.node_type == 'leaf':
                    # 处理叶节点（文本内容）
                    content = child_node.translated if child_node.translated else child_node.content
                    tag.append(NavigableString(self.restore_content(content)))
                else:
                    # 处理非叶节点（标签）
                    child_soup = BeautifulSoup(child_node.content, parser)
                    child_tag = child_soup.find()
                    if child_tag:
                        self._update_node_content(child_tag, child_node, parser)
                        tag.append(child_tag)

    def restore_content(self, content: str) -> str:
        """
        还原占位符内容，保留原始的 HTML 标签。
        
        Args:
            content: 包含占位符的内容
            
        Returns:
            str: 还原后的内容
        """
        # 还原占位符，保留 HTML 标签
        for placeholder, original_content in self.placeholders.items():
            # 如果原始内容是 HTML 标签，直接替换
            if original_content.startswith('<') and original_content.endswith('>'):
                content = content.replace(placeholder, original_content)
            else:
                # 对于非 HTML 标签的内容，保持原样
                content = content.replace(placeholder, original_content)
        return content

if __name__ == '__main__':
    async def main():
        with open("/Users/amaozhao/workspace/epubox/app/html/B21025_FM.xhtml", "r") as f:
            content = f.read()
        processor = TreeProcessor(translator=TranslatorProvider(limit_value=1000), source_lang='en', target_lang='zh')
        await processor.process(content)
        print(processor.restore_html(processor.root))
        
    asyncio.run(main())
