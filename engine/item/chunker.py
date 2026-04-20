import re
import uuid
from functools import lru_cache
from typing import Any, List, NamedTuple

import tiktoken

from bs4 import BeautifulSoup, NavigableString
from bs4.element import ProcessingInstruction

from engine.item.xpath import get_xpath
from engine.schemas.chunk import Chunk, NavTextTarget


@lru_cache(maxsize=1)
def _get_tokenizer() -> Any | None:
    """优先使用 tiktoken；不可用时回退到本地近似估算。"""
    try:
        return tiktoken.encoding_for_model("gpt-3.5-turbo")
    except Exception:
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None


def count_tokens(text: str) -> int:
    """计算文本的 token 数。"""
    tokenizer = _get_tokenizer()
    if tokenizer is None:
        # Keep chunk sizing deterministic even when the tokenizer assets
        # cannot be fetched in sandboxed or offline environments.
        return max(1, len(re.findall(r"\w+|[^\w\s]", text)))
    return len(tokenizer.encode(text))


class Block(NamedTuple):
    html: str  # 元素的 HTML 字符串
    tokens: int  # token 数估算
    xpath: str  # 元素在 DOM 中的路径
    secondary_placeholders: int  # PRE/CODE/STYLE 占位符数量


class NavTextUnit(NamedTuple):
    marker: str
    text: str
    tokens: int
    target: NavTextTarget


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
    SECONDARY_PLACEHOLDER_RE = re.compile(r"\[(PRE|CODE|STYLE):\d+\]")
    DEFAULT_SECONDARY_PLACEHOLDER_LIMIT = 12
    DEFAULT_NAV_UNIT_LIMIT = 48

    # 不可拆分的容器（整体作为一个块，不递归拆分子元素）
    ATOMIC_TAGS = {"figure", "nav"}

    def __init__(
        self,
        token_limit: int = 2000,
        secondary_placeholder_limit: int = DEFAULT_SECONDARY_PLACEHOLDER_LIMIT,
        nav_unit_limit: int = DEFAULT_NAV_UNIT_LIMIT,
    ):
        self.token_limit = token_limit
        self.secondary_placeholder_limit = secondary_placeholder_limit
        self.nav_unit_limit = nav_unit_limit

    def chunk(self, html: str, is_nav_file: bool = False) -> List[Chunk]:
        """
        对 HTML 进行 DOM 级别分块

        Args:
            html: PreCodeExtractor 处理后的 HTML
            is_nav_file: 是否是导航文件（toc.ncx）

        Returns:
            chunks 列表
        """
        soup = BeautifulSoup(html, "html.parser")

        if is_nav_file:
            nav_chunks = self._chunk_nav_text(soup)
            if nav_chunks:
                return nav_chunks
            return []

        # 1. 找到内容容器
        container = soup.find("body") or soup

        # 2. 收集可翻译的块元素 / 内嵌目录导航块
        units = self._collect_blocks(container)

        # 对非导航文件，额外收集 <head><title>
        title_blocks = self._collect_title_block(soup)
        units = title_blocks + units

        # 3. 贪心合并
        chunks = self._greedy_merge(units)

        return chunks

    def _chunk_nav_text(self, soup) -> List[Chunk]:
        """导航文件走文本节点级分块，避免大块 nav HTML 超限。"""
        containers = self._nav_containers(soup)
        units = self._collect_nav_text_units(containers)
        if not units:
            return []
        return self._pack_nav_units(units)

    def _nav_containers(self, soup) -> List[BeautifulSoup]:
        nav_map = soup.find("navmap") or soup.find("navMap")
        if nav_map:
            return [nav_map]

        nav_elements = soup.find_all("nav")
        if nav_elements:
            return nav_elements

        body = soup.find("body")
        return [body or soup]

    def _collect_nav_text_units(self, containers) -> List[NavTextUnit]:
        units: List[NavTextUnit] = []

        for container in containers:
            for node in container.descendants:
                if not isinstance(node, NavigableString):
                    continue
                if isinstance(node, ProcessingInstruction):
                    continue

                text = str(node).strip()
                if not text:
                    continue

                parent = node.parent
                if not getattr(parent, "name", None):
                    continue
                if parent.name == "[document]":
                    continue

                if parent.name in self.SKIP_TAGS or parent.name in {"script", "style"}:
                    continue

                clean_text = self.SECONDARY_PLACEHOLDER_RE.sub("", text)
                if not clean_text.strip():
                    continue

                text_index = self._get_nav_text_index(node)
                if text_index < 0:
                    continue

                marker = f"[NAVTXT:{len(units)}]"
                target = NavTextTarget(
                    marker=marker,
                    xpath=get_xpath(parent),
                    text_index=text_index,
                    original_text=text,
                )
                units.append(
                    NavTextUnit(
                        marker=marker,
                        text=text,
                        tokens=count_tokens(f"{marker} {text}"),
                        target=target,
                    )
                )

        return units

    def _get_nav_text_index(self, node: NavigableString) -> int:
        parent = node.parent
        index = -1
        for child in parent.contents:
            if not isinstance(child, NavigableString):
                continue
            child_text = str(child).strip()
            if not child_text:
                continue
            clean_text = self.SECONDARY_PLACEHOLDER_RE.sub("", child_text)
            if not clean_text.strip():
                continue
            index += 1
            if child is node:
                return index
        return -1

    def _pack_nav_units(self, units: List[NavTextUnit]) -> List[Chunk]:
        chunks: List[Chunk] = []
        buffer_lines: List[str] = []
        buffer_targets: List[NavTextTarget] = []
        buffer_tokens = 0

        for unit in units:
            exceeds_token_limit = buffer_tokens + unit.tokens > self.token_limit
            exceeds_unit_limit = len(buffer_targets) >= self.nav_unit_limit
            if buffer_lines and (exceeds_token_limit or exceeds_unit_limit):
                chunks.append(self._create_nav_chunk(buffer_lines, buffer_targets, buffer_tokens))
                buffer_lines = []
                buffer_targets = []
                buffer_tokens = 0

            buffer_lines.append(f"{unit.marker} {unit.text}")
            buffer_targets.append(unit.target)
            buffer_tokens += unit.tokens

        if buffer_lines:
            chunks.append(self._create_nav_chunk(buffer_lines, buffer_targets, buffer_tokens))

        return chunks

    def _create_nav_chunk(self, lines: List[str], targets: List[NavTextTarget], tokens: int) -> Chunk:
        return Chunk(
            name=uuid.uuid4().hex[:8],
            original="\n".join(lines),
            translated=None,
            tokens=tokens,
            chunk_mode="nav_text",
            nav_targets=targets,
            xpaths=[],
        )

    def _collect_title_block(self, soup) -> List[Block]:
        """收集 <head><title> 作为可翻译块"""
        head = soup.find("head")
        if not head:
            return []
        title = head.find("title")
        if not title or not title.get_text(strip=True):
            return []
        title_html = str(title)
        return [
            Block(
                html=title_html,
                tokens=count_tokens(title_html),
                xpath=get_xpath(title),
                secondary_placeholders=self._count_secondary_placeholders(title_html),
            )
        ]

    def _collect_blocks(self, container) -> List[Block | Chunk]:
        """
        收集容器的直接子元素作为块

        对于超过 token_limit 的单个元素：
        - 如果是 ATOMIC_TAGS（table/ul/ol），保持完整不拆分
        - 否则递归到子元素级别
        """
        blocks: List[Block | Chunk] = []

        for child in container.children:
            child_html = str(child).strip()
            if not child_html:
                continue

            # 跳过不可翻译元素
            if self._should_skip(child):
                continue

            if self._is_embedded_toc_nav(child):
                blocks.extend(self._chunk_embedded_nav(child))
                continue

            child_tokens = count_tokens(child_html)
            child_placeholder_count = self._count_secondary_placeholders(child_html)
            xpath = get_xpath(child)

            if child_tokens <= self.token_limit and child_placeholder_count <= self.secondary_placeholder_limit:
                blocks.append(
                    Block(
                        html=child_html,
                        tokens=child_tokens,
                        xpath=xpath,
                        secondary_placeholders=child_placeholder_count,
                    )
                )
            elif hasattr(child, "name") and child.name in self.ATOMIC_TAGS:
                # 不可拆分容器：整体作为一个块（可能超限）
                blocks.append(
                    Block(
                        html=child_html,
                        tokens=child_tokens,
                        xpath=xpath,
                        secondary_placeholders=child_placeholder_count,
                    )
                )
            else:
                # 超限元素：递归到子元素
                child_blocks = self._collect_blocks(child)
                if child_blocks:
                    blocks.extend(child_blocks)
                else:
                    # 叶子元素没有可继续细分的子块时，保留原元素，避免内容丢失。
                    blocks.append(
                        Block(
                            html=child_html,
                            tokens=child_tokens,
                            xpath=xpath,
                            secondary_placeholders=child_placeholder_count,
                        )
                    )

        return blocks

    def _greedy_merge(self, blocks: List[Block | Chunk]) -> List[Chunk]:
        """贪心合并：将多个块打包到一个 chunk，直到接近 token_limit"""
        chunks = []
        buffer_htmls: List[str] = []
        buffer_xpaths: List[str] = []
        buffer_tokens = 0
        buffer_placeholders = 0

        def flush_buffer() -> None:
            nonlocal buffer_htmls, buffer_xpaths, buffer_tokens, buffer_placeholders
            if not buffer_htmls:
                return
            chunks.append(self._create_chunk(buffer_htmls, buffer_xpaths, buffer_tokens))
            buffer_htmls = []
            buffer_xpaths = []
            buffer_tokens = 0
            buffer_placeholders = 0

        for block in blocks:
            if isinstance(block, Chunk):
                flush_buffer()
                chunks.append(block)
                continue

            exceeds_token_limit = buffer_tokens + block.tokens > self.token_limit
            exceeds_placeholder_limit = (
                buffer_placeholders + block.secondary_placeholders > self.secondary_placeholder_limit
            )
            if (exceeds_token_limit or exceeds_placeholder_limit) and buffer_htmls:
                flush_buffer()

            buffer_htmls.append(block.html)
            buffer_xpaths.append(block.xpath)
            buffer_tokens += block.tokens
            buffer_placeholders += block.secondary_placeholders

        flush_buffer()

        return chunks

    def _create_chunk(self, htmls: List[str], xpaths: List[str], tokens: int) -> Chunk:
        """将多个 HTML 片段组合为一个 Chunk"""
        return Chunk(
            name=uuid.uuid4().hex[:8],
            original="\n".join(htmls),
            translated=None,
            tokens=tokens,
            xpaths=xpaths,
        )

    def _should_skip(self, element) -> bool:
        """判断元素是否不需要翻译"""
        if not hasattr(element, "name") or not element.name:
            # NavigableString — 裸文本节点无法生成有意义的 xpath，跳过
            return True

        if element.name in self.SKIP_TAGS:
            return True

        # 检查元素是否有实际文本内容
        text_content = element.get_text(strip=True)
        clean_text = self.SECONDARY_PLACEHOLDER_RE.sub("", text_content)
        return not clean_text.strip()

    def _is_embedded_toc_nav(self, element) -> bool:
        """识别普通文档中内嵌的目录导航块，走 nav_text 模式。"""
        if not getattr(element, "name", None) == "nav":
            return False

        classes = {cls.lower() for cls in element.get("class", []) if isinstance(cls, str)}
        if "toc" in classes:
            return True

        for attr in ("epub:type", "type", "role", "id"):
            value = element.get(attr)
            if not value:
                continue
            if isinstance(value, list):
                tokens = [str(v).lower() for v in value]
            else:
                tokens = [str(value).lower()]
            if any("toc" in token for token in tokens):
                return True

        return False

    def _chunk_embedded_nav(self, element) -> List[Chunk]:
        units = self._collect_nav_text_units([element])
        if not units:
            return []
        return self._pack_nav_units(units)

    def _count_secondary_placeholders(self, html: str) -> int:
        return len(self.SECONDARY_PLACEHOLDER_RE.findall(html))
