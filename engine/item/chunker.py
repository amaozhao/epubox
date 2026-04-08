import re
import uuid
import tiktoken
from typing import List, NamedTuple

from bs4 import BeautifulSoup

from engine.item.xpath import get_xpath
from engine.schemas.chunk import Chunk


def count_tokens(text: str) -> int:
    """计算文本的token数"""
    try:
        tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")
    except KeyError:
        tokenizer = tiktoken.get_encoding("cl100k_base")
    return len(tokenizer.encode(text))


class Block(NamedTuple):
    html: str       # 元素的 HTML 字符串
    tokens: int     # token 数估算
    xpath: str      # 元素在 DOM 中的路径


class DomChunker:
    """
    基于 DOM 结构的智能分块器

    设计原则：
    1. 以完整 DOM 元素为最小切割单位，保证标签完整闭合
    2. 贪心合并多个元素到 token_limit 上限
    3. 不可翻译元素（纯占位符、纯图片等）跳过，不进入 chunk
    4. 记录每个元素的 xpath，用于翻译后恢复
    """

    # 不可翻译的元素（跳过，不进入 chunk）
    SKIP_TAGS = {"img", "svg", "math", "video", "audio", "canvas", "iframe"}

    # 不可拆分的容器（整体作为一个块，不递归拆分子元素）
    ATOMIC_TAGS = {"table", "ul", "ol", "dl", "figure", "nav"}

    def __init__(self, token_limit: int = 2000):
        self.token_limit = token_limit

    def chunk(self, html: str, is_nav_file: bool = False) -> List[Chunk]:
        """
        对 HTML 进行 DOM 级别分块

        Args:
            html: PreCodeExtractor 处理后的 HTML
            is_nav_file: 是否是导航文件（toc.ncx）

        Returns:
            chunks 列表
        """
        soup = BeautifulSoup(html, 'html.parser')

        # 1. 找到内容容器
        if is_nav_file:
            container = soup.find('navMap') or soup
        else:
            container = soup.find('body') or soup

        # 2. 收集可翻译的块元素
        blocks = self._collect_blocks(container)

        # 对非导航文件，额外收集 <head><title>
        if not is_nav_file:
            title_blocks = self._collect_title_block(soup)
            blocks = title_blocks + blocks

        # 3. 贪心合并
        chunks = self._greedy_merge(blocks)

        return chunks

    def _collect_title_block(self, soup) -> List[Block]:
        """收集 <head><title> 作为可翻译块"""
        head = soup.find('head')
        if not head:
            return []
        title = head.find('title')
        if not title or not title.get_text(strip=True):
            return []
        title_html = str(title)
        return [Block(
            html=title_html,
            tokens=count_tokens(title_html),
            xpath=get_xpath(title),
        )]

    def _collect_blocks(self, container) -> List[Block]:
        """
        收集容器的直接子元素作为块

        对于超过 token_limit 的单个元素：
        - 如果是 ATOMIC_TAGS（table/ul/ol），保持完整不拆分
        - 否则递归到子元素级别
        """
        blocks = []

        for child in container.children:
            child_html = str(child).strip()
            if not child_html:
                continue

            # 跳过不可翻译元素
            if self._should_skip(child):
                continue

            child_tokens = count_tokens(child_html)
            xpath = get_xpath(child)

            if child_tokens <= self.token_limit:
                blocks.append(Block(html=child_html, tokens=child_tokens, xpath=xpath))
            elif hasattr(child, 'name') and child.name in self.ATOMIC_TAGS:
                # 不可拆分容器：整体作为一个块（可能超限）
                blocks.append(Block(html=child_html, tokens=child_tokens, xpath=xpath))
            else:
                # 超限元素：递归到子元素
                blocks.extend(self._collect_blocks(child))

        return blocks

    def _greedy_merge(self, blocks: List[Block]) -> List[Chunk]:
        """贪心合并：将多个块打包到一个 chunk，直到接近 token_limit"""
        chunks = []
        buffer_htmls = []
        buffer_xpaths = []
        buffer_tokens = 0

        for block in blocks:
            if buffer_tokens + block.tokens > self.token_limit and buffer_htmls:
                chunks.append(self._create_chunk(buffer_htmls, buffer_xpaths, buffer_tokens))
                buffer_htmls = []
                buffer_xpaths = []
                buffer_tokens = 0

            buffer_htmls.append(block.html)
            buffer_xpaths.append(block.xpath)
            buffer_tokens += block.tokens

        if buffer_htmls:
            chunks.append(self._create_chunk(buffer_htmls, buffer_xpaths, buffer_tokens))

        return chunks

    def _create_chunk(self, htmls: List[str], xpaths: List[str], tokens: int) -> Chunk:
        """将多个 HTML 片段组合为一个 Chunk"""
        return Chunk(
            name=uuid.uuid4().hex[:8],
            original="\n".join(htmls),
            tokens=tokens,
            xpaths=xpaths,
        )

    def _should_skip(self, element) -> bool:
        """判断元素是否不需要翻译"""
        if not hasattr(element, 'name') or not element.name:
            # NavigableString — 裸文本节点无法生成有意义的 xpath，跳过
            return True

        if element.name in self.SKIP_TAGS:
            return True

        # 检查元素是否有实际文本内容
        text_content = element.get_text(strip=True)
        clean_text = re.sub(r'\[(PRE|CODE|STYLE):\d+\]', '', text_content)
        return not clean_text.strip()

