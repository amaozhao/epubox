import re
from typing import List
import tiktoken

from engine.schemas.chunk import Chunk


class Chunker:
    def __init__(self, limit: int = 3000, encoder: str = "gpt-3.5-turbo"):
        try:
            self.tokenizer = tiktoken.encoding_for_model(encoder)
        except KeyError:
            self.tokenizer = tiktoken.get_encoding("cl100k_base")

        self.limit = limit
        # 常规内容允许拆分的标签
        self.allowed_split_tags = {"p", "li", "ul", "div", "header", "section", "figure", "body", "html", "head", "br"}
        # EPUB 导航相关标签 - 禁止拆分，这些标签必须保持完整
        self.nav_tags = {"navPoint", "navLabel", "content", "navMap", "pageList", "pageTarget", "spine", "itemref"}
        self.any_tag_pattern = re.compile(r"<[^>]+>")
        self.closing_tag_pattern = re.compile(r"</([a-zA-Z][a-zA-Z0-9]*)\s*>")

    def get_token_count(self, content: str) -> int:
        return len(self.tokenizer.encode(content)) if content else 0

    def chunk(self, html: str, is_nav_file: bool = False) -> List[Chunk]:
        """
        将 HTML 内容拆分成多个块。

        Args:
            html: HTML 内容
            is_nav_file: 是否是 EPUB 导航文件（toc.ncx, nav.xhtml等）
        """
        chunks = []
        pos = 0
        n = len(html)
        cid = 0

        # 导航文件也使用统一的限制，保持结构完整性
        effective_limit = self.limit

        while pos < n:
            cid += 1
            # 1. 寻找 Token 限制下的最大字符截止位置
            low, high = 0, n - pos
            token_limit_char_end = pos
            while low <= high:
                mid = (low + high) // 2
                if self.get_token_count(html[pos : pos + mid]) <= effective_limit:
                    token_limit_char_end = pos + mid
                    low = mid + 1
                else:
                    high = mid - 1

            # 2. 在 Token 限制内寻找最佳拆分点
            split_at = -1

            # 尝试寻找允许的结束标签 (如 </p>, </div>)
            for m in self.closing_tag_pattern.finditer(html, pos, token_limit_char_end):
                tag_name = m.group(1).lower()
                # 跳过导航标签作为拆分点，保护 EPUB 目录结构
                if is_nav_file and hasattr(self, 'nav_tags') and tag_name in self.nav_tags:
                    continue
                if tag_name in self.allowed_split_tags:
                    split_at = m.end()

            # 3. 兜底策略：如果没找到允许的标签，寻找限制内的【任意】完整标签结尾
            if split_at <= pos:
                for m in self.any_tag_pattern.finditer(html, pos, token_limit_char_end):
                    split_at = m.end()

            # 4. 极端情况：如果连一个完整标签都没装下，或者根本没标签
            # 则必须强制在 token_limit_char_end 拆分，但要避开 < > 内部
            if split_at <= pos:
                split_at = token_limit_char_end
                # 检查是否切断了标签：如果在 split_at 之前有 '<' 但没有对应的 '>'
                last_open = html.rfind("<", pos, split_at)
                last_close = html.rfind(">", pos, split_at)
                if last_open > last_close:
                    # 我们处于标签中间，回退到 '<' 之前
                    split_at = last_open

                # 如果回退后 split_at == pos，说明单个标签就超了 limit
                # 这时只能硬切，否则会死循环。优先保证至少前进一点。
                if split_at <= pos:
                    split_at = max(pos + 1, token_limit_char_end)

            # 5. 生成 Chunk
            content = html[pos:split_at]
            if content:
                chunks.append(
                    Chunk(name=str(cid), original=content, translated=None, tokens=self.get_token_count(content))
                )

            pos = split_at

        return chunks
