import re
import uuid
import tiktoken
from typing import List

from engine.schemas.chunk import Chunk


def count_tokens(text: str) -> int:
    """计算文本的token数"""
    try:
        tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")
    except KeyError:
        tokenizer = tiktoken.get_encoding("cl100k_base")
    return len(tokenizer.encode(text))


class HtmlChunker:
    """
    基于HTML结构的智能分块器

    设计原则：
    1. 只在完整HTML块边界分割
    2. 分割前分析标签对完整性
    3. 确保不会切断标签对
    4. 限制每个chunk的占位符数量，避免模型处理失败
    """

    # 允许作为分割点的块级标签
    ALLOWED_SPLIT_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "blockquote", "pre", "section",
        "article", "header", "footer", "nav", "aside",
        "main", "figure", "figcaption", "table", "tr"
    }

    # 禁止分割的标签（必须保持完整）
    FORBIDDEN_SPLIT_TAGS = {
        "navPoint", "navLabel", "content", "navMap",
        "pageList", "pageTarget", "spine", "itemref"
    }

    def __init__(self, token_limit: int = 1200, max_placeholders_per_chunk: int = 15):
        self.token_limit = token_limit
        self.max_placeholders_per_chunk = max_placeholders_per_chunk
        self.placeholder_pattern = re.compile(r'\[id\d+\]')
        self.closing_tag_pattern = re.compile(r'</([a-zA-Z][a-zA-Z0-9]*)\s*>')
        self.any_tag_pattern = re.compile(r'<[^>]+>')

    def chunk(
        self,
        text_with_placeholders: str,
        global_indices: List[int],
        placeholder_mgr,
        is_nav_file: bool = False
    ) -> List[Chunk]:
        """
        将带占位符的文本分块

        Args:
            text_with_placeholders: 带占位符的HTML文本
            global_indices: 文本中所有占位符的全局索引
            placeholder_mgr: PlaceholderManager实例
            is_nav_file: 是否是导航文件

        Returns:
            List[Chunk]: 分块后的列表
        """

        # Step 1: 找到所有占位符的位置
        placeholder_positions = self._find_placeholder_positions(
            text_with_placeholders
        )

        # Step 2: 找到安全分割点
        split_points = self._find_safe_split_points(
            text_with_placeholders,
            placeholder_positions,
            placeholder_mgr,
            is_nav_file
        )

        # Step 3: 根据分割点分割文本
        segments = self._split_at_points(text_with_placeholders, split_points)

        # Step 4: 合并segments为chunk（考虑token限制）
        chunks = self._merge_into_chunks(
            segments,
            global_indices,
            placeholder_mgr
        )

        return chunks

    def _find_placeholder_positions(self, text: str) -> List:
        """
        找到所有占位符的位置

        Returns:
            List of (start, end, placeholder, index)
            例如: [(0, 4, '[id0]', 0), (10, 15, '[id1]', 1), ...]
        """
        positions = []
        for match in self.placeholder_pattern.finditer(text):
            placeholder = match.group()
            index = int(placeholder[3:-1])
            positions.append((match.start(), match.end(), placeholder, index))
        return positions

    def _find_safe_split_points(
        self,
        text: str,
        placeholder_positions: List,
        placeholder_mgr,
        is_nav_file: bool
    ) -> List[int]:
        """
        找到安全分割位置

        分割点优先级：
        1. 块级结束标签后（如 </p>, </div>）
        2. 章节标题后（</h1>, </h2>, </h3>）
        3. 列表项后（</li>）
        """

        split_points = []

        for i, (start, end, placeholder, idx) in enumerate(placeholder_positions):
            tag_content = placeholder_mgr.tag_map.get(placeholder, "")

            # 跳过禁止分割的标签
            if is_nav_file and tag_content.startswith('<'):
                tag_name = self._extract_tag_name(tag_content)
                if tag_name in self.FORBIDDEN_SPLIT_TAGS:
                    continue

            # 检查是否是块级结束标签 - 只要是块级结束标签就可以分割
            # 分割后 current_chunk 以 </p> 结尾是完整的，next_chunk 可以从文本或新标签开始
            if self._is_block_closing_tag(tag_content):
                priority = self._get_split_priority(tag_content)
                split_points.append((end, priority))

        # 按优先级排序，返回位置
        split_points.sort(key=lambda x: x[1], reverse=True)  # 高优先级优先
        return [pos for pos, _ in split_points]

    def _is_block_closing_tag(self, tag: str) -> bool:
        """检查是否是块级结束标签"""
        match = re.match(r'</([a-zA-Z][a-zA-Z0-9]*)\s*>', tag)
        if match:
            tag_name = match.group(1).lower()
            return tag_name in self.ALLOWED_SPLIT_TAGS
        return False

    def _is_block_opening_tag(self, tag: str) -> bool:
        """检查是否是块级开始标签"""
        match = re.match(r'<([a-zA-Z][a-zA-Z0-9]*)\b', tag)
        if match:
            tag_name = match.group(1).lower()
            return tag_name in self.ALLOWED_SPLIT_TAGS
        return False

    def _extract_tag_name(self, tag: str) -> str:
        """从标签中提取标签名"""
        match = re.match(r'</?([a-zA-Z][a-zA-Z0-9]*)\b', tag)
        if match:
            return match.group(1).lower()
        return ""

    def _get_split_priority(self, tag: str) -> int:
        """获取分割优先级"""
        match = re.match(r'</([a-zA-Z][a-zA-Z0-9]*)\s*>', tag)
        if match:
            tag_name = match.group(1).lower()
            if tag_name in {"h1", "h2", "h3"}:
                return 100  # 最高优先级
            if tag_name == "p":
                return 50
            if tag_name in {"li", "div"}:
                return 30
        return 10

    def _split_at_points(
        self,
        text: str,
        split_points: List[int]
    ) -> List[str]:
        """在分割点分割文本"""
        if not split_points:
            return [text]

        all_points = sorted(set([0] + split_points + [len(text)]))
        segments = []
        for i in range(len(all_points) - 1):
            segment = text[all_points[i]:all_points[i + 1]]
            if segment:
                segments.append(segment)
        return segments

    def _merge_into_chunks(
        self,
        segments: List[str],
        global_indices: List[int],
        placeholder_mgr
    ) -> List[Chunk]:
        """将segments合并为考虑token限制和占位符数量的chunk"""

        chunks = []
        current_chunk_parts: list[str] = []
        current_indices: list[int] = []

        for segment in segments:
            # 统计这个segment中的占位符数量
            segment_placeholder_count = len(self.placeholder_pattern.findall(segment))

            # 计算如果加入这个segment，占位符数量是否超限
            new_indices = []
            for ph in self.placeholder_pattern.findall(segment):
                idx = int(ph[3:-1])
                if idx not in current_indices:
                    new_indices.append(idx)

            # 计算如果加入这个segment，token数是否超限
            test_parts = current_chunk_parts + [segment]
            test_text = ''.join(test_parts)
            test_tokens = count_tokens(test_text)

            can_append = (
                test_tokens <= self.token_limit and
                len(current_indices) + len(new_indices) <= self.max_placeholders_per_chunk
            )

            if can_append:
                # 可以加入当前chunk
                current_chunk_parts.append(segment)
                for ph in self.placeholder_pattern.findall(segment):
                    idx = int(ph[3:-1])
                    if idx not in current_indices:
                        current_indices.append(idx)
            elif not current_chunk_parts and segment_placeholder_count > self.max_placeholders_per_chunk:
                # 第一个segment本身就超限，需要强制按占位符数量分割
                ph_positions = [(m.start(), m.end(), m.group()) for m in self.placeholder_pattern.finditer(segment)]
                for i in range(0, len(ph_positions), self.max_placeholders_per_chunk):
                    sub_positions = ph_positions[i:i + self.max_placeholders_per_chunk]
                    if not sub_positions:
                        continue
                    start, end = sub_positions[0][0], sub_positions[-1][1]
                    sub_segment = segment[start:end]
                    chunks.append(self._create_chunk(
                        [sub_segment],
                        [int(p[2][3:-1]) for p in sub_positions],
                        placeholder_mgr
                    ))
            else:
                # 保存当前chunk，开始新的
                if current_chunk_parts:
                    chunks.append(self._create_chunk(
                        current_chunk_parts,
                        current_indices,
                        placeholder_mgr
                    ))
                current_chunk_parts = [segment]
                current_indices = []
                for ph in self.placeholder_pattern.findall(segment):
                    idx = int(ph[3:-1])
                    if idx not in current_indices:
                        current_indices.append(idx)

        # 保存最后一个chunk
        if current_chunk_parts:
            chunks.append(self._create_chunk(
                current_chunk_parts,
                current_indices,
                placeholder_mgr
            ))

        return chunks

    def _create_chunk(
        self,
        parts: List[str],
        indices: List[int],
        placeholder_mgr
    ) -> Chunk:
        """创建一个Chunk对象"""
        text = ''.join(parts).strip('\n')
        return Chunk(
            name=str(uuid.uuid4())[:8],
            original=text,
            translated=None,
            local_tag_map=placeholder_mgr.get_local_tag_map(sorted(indices)),
            tokens=count_tokens(text)
        )

    # ========== 新增方法：按 HTML 标签分割 ==========

    # 安全分割点：块级结束标签
    SAFE_BLOCK_END_TAGS = [
        '</p>', '</div>', '</h1>', '</h2>', '</h3>',
        '</h4>', '</h5>', '</h6>', '</blockquote>',
        '</section>', '</article>', '</header>', '</footer>',
        '</main>', '</aside>', '</figure>', '</figcaption>'
    ]

    # 容器级结束标签（万不得已时分割）
    CONTAINER_END_TAGS = [
        '</table>', '</thead>', '</tbody>', '</tfoot>',
        '</ul>', '</ol>'
    ]

    # 导航文件安全分割点
    NAV_SAFE_BLOCK_END_TAGS = [
        '</navPoint>', '</p>', '</div>'
    ]

    # 禁止分割的标签（会破坏结构）
    FORBIDDEN_SPLIT_TAGS_INTERNAL = [
        '</tr>', '</td>', '</th>', '</li>'
    ]

    # 安全分割点：块级结束标签
    SAFE_BLOCK_END_TAGS = [
        '</p>', '</div>', '</h1>', '</h2>', '</h3>',
        '</h4>', '</h5>', '</h6>', '</blockquote>',
        '</section>', '</article>', '</header>', '</footer>',
        '</main>', '</aside>', '</figure>', '</figcaption>'
    ]

    def chunk_by_html_tags(
        self,
        html: str,
        token_limit: int = 1200,
        is_nav_file: bool = False
    ) -> List[str]:
        """
        按块级 HTML 标签分割

        参考原版 Chunker 的简洁思路：
        1. 二分查找找到 token 限制内最大的字符位置
        2. 在这个范围内找最佳分割点
        3. 循环处理直到完成

        Args:
            html: 原始 HTML 文本
            token_limit: token 限制
            is_nav_file: 是否是导航文件（toc.ncx / nav.xhtml）

        Returns:
            List[str]: 分割后的原始 HTML 片段列表
        """
        if is_nav_file:
            return self._chunk_nav_file(html, token_limit)
        else:
            return self._chunk_normal_html(html, token_limit)

    def _chunk_normal_html(self, html: str, token_limit: int) -> List[str]:
        """普通 HTML 分割策略"""
        chunks = []
        pos = 0
        n = len(html)

        while pos < n:
            # 1. 二分查找：找到 token 限制内最大的字符位置
            low, high = 1, n - pos
            char_limit_end = pos
            while low <= high:
                mid = (low + high) // 2
                if count_tokens(html[pos:pos + mid]) <= token_limit:
                    char_limit_end = pos + mid
                    low = mid + 1
                else:
                    high = mid - 1

            # 2. 在 char_limit_end 范围内找最佳分割点
            split_at = -1

            # 优先找允许的块级结束标签
            for m in self.closing_tag_pattern.finditer(html, pos, char_limit_end):
                tag_name = m.group(1).lower()
                if tag_name in self.ALLOWED_SPLIT_TAGS:
                    split_at = m.end()

            # 3. 兜底：如果没找到块级结束标签，使用硬切（避免切成极小的 chunk）
            if split_at <= pos:
                split_at = self._hard_split_single(html, pos, token_limit)

            # 4. 极端情况：如果硬切也失败
            if split_at <= pos:
                split_at = char_limit_end
                # 检查是否切断了标签
                last_open = html.rfind("<", pos, split_at)
                last_close = html.rfind(">", pos, split_at)
                if last_open > last_close:
                    split_at = last_open
                if split_at <= pos:
                    split_at = max(pos + 1, char_limit_end)

            # 5. 生成 Chunk
            chunk_text = html[pos:split_at]
            if chunk_text.strip():
                chunks.append(chunk_text)
            pos = split_at

        # 后处理：合并太小的 chunk
        chunks = self._merge_small_chunks(chunks, token_limit)

        return [c for c in chunks if c.strip()]

    def _chunk_nav_file(self, html: str, token_limit: int) -> List[str]:
        """
        导航文件分割策略 - 尽可能填满到 token_limit

        核心逻辑：贪婪累积多个 navPoint，直到达到 token_limit
        """
        chunks = []
        pos = 0
        n = len(html)

        # nav 文件只允许在 navPoint 边界分割
        pattern = re.compile(r'</navPoint>', re.IGNORECASE)

        while pos < n:
            # 找到所有 navPoint 边界
            all_boundaries = [(m.end(), m.group()) for m in pattern.finditer(html, pos)]

            if not all_boundaries:
                # 没有 navPoint 了，使用普通 HTML 分割逻辑处理剩余内容
                remaining = html[pos:]
                if remaining.strip():
                    # 递归使用普通 HTML 分割处理剩余内容
                    sub_chunks = self._chunk_normal_html(remaining, token_limit)
                    chunks.extend(sub_chunks)
                break

            # 贪婪累积：尽可能填充多个 navPoint 直到 token_limit
            last_valid_boundary = pos

            for boundary_pos, _ in all_boundaries:
                # 检查从 pos 到这个边界的内容是否超限
                segment = html[pos:boundary_pos]
                segment_tokens = count_tokens(segment)

                if segment_tokens <= token_limit:
                    # 这个边界可以放入，更新 last_valid_boundary
                    last_valid_boundary = boundary_pos
                else:
                    # 超限了，使用 last_valid_boundary 作为分割点
                    break

            # 如果所有边界都能放入，取最后一个
            if last_valid_boundary == pos:
                # 第一个边界就超限（极端情况：单个 navPoint 就超过 token_limit）
                # 使用 _chunk_normal_html 的兜底逻辑
                last_valid_boundary = self._find_split_point_normal(html, pos, token_limit)

            chunk_text = html[pos:last_valid_boundary]
            if chunk_text.strip():
                chunks.append(chunk_text)
            pos = last_valid_boundary

        # 后处理：合并太小的 chunk
        chunks = self._merge_small_chunks(chunks, token_limit)

        return [c for c in chunks if c.strip()]

    def _merge_small_chunks(self, chunks: List[str], token_limit: int, min_ratio: float = 0.3) -> List[str]:
        """
        合并太小的 chunk 到相邻 chunk

        优先合并到上一个 chunk（保持结构），如果 chunk 在开头则合并到下一个。

        Args:
            chunks: 分割后的 chunks
            token_limit: token 限制
            min_ratio: 小于 token_limit * min_ratio 的 chunk 需要合并

        Returns:
            合并后的 chunks
        """
        if len(chunks) < 2:
            return chunks

        min_size = token_limit * min_ratio
        i = 0
        while i < len(chunks):
            chunk_tokens = count_tokens(chunks[i])
            if chunk_tokens < min_size:
                # 太小，需要合并
                if i == 0:
                    # 是第一个 chunk，合并到下一个
                    if i + 1 < len(chunks):
                        chunks[i + 1] = chunks[i] + chunks[i + 1]
                        chunks.pop(i)
                        # i 不变，继续检查新的 chunks[i]
                    else:
                        i += 1
                else:
                    # 合并到上一个
                    chunks[i - 1] += chunks[i]
                    chunks.pop(i)
                    i -= 1  # 回退，继续检查前一个
            else:
                i += 1

        return chunks

    def _find_split_point_normal(self, html: str, pos: int, token_limit: int) -> int:
        """普通 HTML 分割的兜底逻辑：在 token_limit 内找最佳分割点"""
        n = len(html)

        # 二分查找：找到 token 限制内最大的字符位置
        low, high = 1, n - pos
        char_limit_end = pos
        while low <= high:
            mid = (low + high) // 2
            if count_tokens(html[pos:pos + mid]) <= token_limit:
                char_limit_end = pos + mid
                low = mid + 1
            else:
                high = mid - 1

        # 找最后一个允许的块级结束标签
        split_at = -1
        for m in self.closing_tag_pattern.finditer(html, pos, char_limit_end):
            tag_name = m.group(1).lower()
            if tag_name in self.ALLOWED_SPLIT_TAGS:
                split_at = m.end()

        # 兜底：找任意完整标签结尾
        if split_at <= pos:
            for m in self.any_tag_pattern.finditer(html, pos, char_limit_end):
                split_at = m.end()

        # 极端情况
        if split_at <= pos:
            split_at = char_limit_end
            last_open = html.rfind("<", pos, split_at)
            last_close = html.rfind(">", pos, split_at)
            if last_open > last_close:
                split_at = last_open
            if split_at <= pos:
                split_at = max(pos + 1, char_limit_end)

        return split_at

    def _is_inside_container(self, html: str, pos: int) -> bool:
        """
        检查位置是否在容器标签（table/ul/ol）内部

        Args:
            html: HTML文本
            pos: 要检查的位置（分割点）

        Returns:
            bool: 如果位置在容器内部返回True，否则返回False
        """
        before, after = html[:pos], html[pos:]
        for container in ['table', 'ul', 'ol']:
            opens = list(re.finditer(f'<{container}[^>]*>', before))
            if opens:
                # 检查是否有对应的结束标签在 after 中
                if re.search(f'</{container}>', after):
                    return True
        return False

    def _find_block_end_positions(self, html: str, tags: List[str]) -> set:
        """
        找到所有给定标签的结束位置

        Returns:
            set: 标签结束位置集合
        """
        positions = set()
        for tag in tags:
            for match in re.finditer(re.escape(tag), html):
                end_pos = match.end()
                # 跳过在容器标签内部的分割点
                if not self._is_inside_container(html, end_pos):
                    positions.add(end_pos)
        return positions

    def _split_at_sentence_boundary(self, text: str, token_limit: int) -> List[str]:
        """
        在句子边界分割（保底方案）

        中英文句子结束符：. ! ? 。！？后接大写字母/汉字/HTML标签
        """
        # 中英文句子结束符模式
        sentence_pattern = r'([.!?。！？])\s+(?=[A-Z<\u4e00-\u9fa5])'
        matches = list(re.finditer(sentence_pattern, text))

        if not matches:
            # 找不到句子边界，按 token_limit 硬切
            return self._hard_split(text, token_limit)

        # 句子边界位置列表（每个句子从上一个边界或0到当前边界）
        boundaries = [0] + [m.end() for m in matches] + [len(text)]

        result = []
        i = 0

        while i < len(boundaries) - 1:
            chunk_start = boundaries[i]

            # 二分查找：从 chunk_start 开始，能容纳多少个句子
            left, right = i + 1, len(boundaries)
            while left < right:
                mid = (left + right) // 2
                chunk_end = boundaries[mid]
                chunk = text[chunk_start:chunk_end]
                if count_tokens(chunk) <= token_limit:
                    left = mid + 1
                else:
                    right = mid

            best_boundary = left - 1
            if best_boundary <= i:
                # 当前句子本身就超限，用硬切
                chunk_end = boundaries[i + 1]
                chunk = text[chunk_start:chunk_end]
                result.extend(self._hard_split(chunk, token_limit))
                i += 1
            else:
                result.append(text[chunk_start:boundaries[best_boundary]])
                i = best_boundary

        # 处理剩余
        if i < len(boundaries) - 1:
            remaining = text[boundaries[i]:]
            if remaining.strip():
                if count_tokens(remaining) <= token_limit:
                    result.append(remaining)
                else:
                    result.extend(self._hard_split(remaining, token_limit))

        return result if result else self._hard_split(text, token_limit)

    def _hard_split_single(self, html: str, pos: int, token_limit: int) -> int:
        """
        硬切单个 chunk：从 pos 开始，找到 token_limit 内的最大字符位置

        Returns:
            int: 分割点位置
        """
        n = len(html)
        low, high = pos + 1, n
        best_end = pos

        while low <= high:
            mid = (low + high) // 2
            if count_tokens(html[pos:mid]) <= token_limit:
                best_end = mid
                low = mid + 1
            else:
                high = mid - 1

        # 确保 best_end 有前进
        if best_end <= pos:
            best_end = min(pos + 1, n)

        return best_end

    def _hard_split(self, text: str, token_limit: int) -> List[str]:
        """
        按 token_limit 硬切（极端保底方案）

        使用二分查找优化，从前往后尽量在 token 限制内填充更多内容
        """
        if count_tokens(text) <= token_limit:
            return [text]

        result = []
        current_pos = 0
        text_len = len(text)

        while current_pos < text_len:
            # 二分查找当前 chunk 的结束位置
            left, right = current_pos + 1, text_len + 1
            while left < right:
                mid = (left + right) // 2
                chunk = text[current_pos:mid]
                tokens = count_tokens(chunk)
                if tokens <= token_limit:
                    left = mid + 1
                else:
                    right = mid

            best_end = left - 1

            # 如果 best_end 没有前进，强制前进一个字符
            if best_end <= current_pos:
                best_end = min(current_pos + 1, text_len)

            result.append(text[current_pos:best_end])
            current_pos = best_end

        return result
