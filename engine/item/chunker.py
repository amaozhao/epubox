import re
import uuid
import tiktoken
from typing import List, Tuple

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

    def __init__(self, token_limit: int = 2000, max_placeholders_per_chunk: int = 15):
        self.token_limit = token_limit
        self.max_placeholders_per_chunk = max_placeholders_per_chunk
        self.placeholder_pattern = re.compile(r'\[id\d+\]')

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
        allowed_tags = self.ALLOWED_SPLIT_TAGS if not is_nav_file else set()

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
        current_chunk_parts = []
        current_indices = []

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
            global_indices=sorted(indices),
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

    def chunk_by_html_tags(
        self,
        html: str,
        token_limit: int = 1500,
        is_nav_file: bool = False
    ) -> List[str]:
        """
        按块级 HTML 标签分割

        新流程：先分割 HTML，再做占位符替换

        Args:
            html: 原始 HTML 文本（可能带 [PRE:N] 占位符）
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
        """
        普通 HTML 分割策略

        分割点优先级：
        1. 安全块级结束标签（</p>, </div> 等）
        2. 句子边界（当 Level 1 无法满足 token 限制时）
        3. 容器级结束标签（</table>, </ul> 等，作为 fallback）
        """
        # Step 1: 找到所有块级结束标签位置
        split_positions = self._find_block_end_positions(html, self.SAFE_BLOCK_END_TAGS)

        # Step 2: 构建分割结果
        all_points = sorted([0] + list(split_positions) + [len(html)])
        result = []

        for i in range(len(all_points) - 1):
            chunk_text = html[all_points[i]:all_points[i + 1]]

            if count_tokens(chunk_text) <= token_limit:
                result.append(chunk_text)
            else:
                # 超长：在句子边界再分割
                sub_chunks = self._split_at_sentence_boundary(chunk_text, token_limit)
                result.extend(sub_chunks)

        return [c for c in result if c.strip()]

    def _chunk_nav_file(self, html: str, token_limit: int) -> List[str]:
        """
        导航文件分割策略

        按 </navPoint> 分割，保持 navPoint 完整
        """
        # 找到所有 </navPoint> 位置
        navpoint_pattern = r'</navPoint>'
        navpoint_positions = [m.end() for m in re.finditer(navpoint_pattern, html)]

        if not navpoint_positions:
            # 没有 navPoint，整个文件作为一个 chunk
            return [html] if html.strip() else []

        chunks = []
        current_start = 0

        for pos in navpoint_positions:
            chunk_text = html[current_start:pos]

            if count_tokens(chunk_text) <= token_limit:
                chunks.append(chunk_text)
                current_start = pos
            else:
                # 超长，在句子边界再分割
                sub_chunks = self._split_at_sentence_boundary(chunk_text, token_limit)
                chunks.extend(sub_chunks)
                current_start = pos

        # 处理最后一段
        if current_start < len(html):
            remaining = html[current_start:]
            # 去掉空白后检查是否只是闭合标签（如 </navMap>）
            stripped = remaining.strip()
            if stripped and not stripped.startswith('</'):
                # 不是只有闭合标签，正常处理
                if count_tokens(remaining) <= token_limit:
                    chunks.append(remaining)
                else:
                    chunks.extend(self._split_at_sentence_boundary(remaining, token_limit))
            elif stripped.startswith('</') and chunks:
                # 只有闭合标签（如 </navMap>），合并到最后一个 chunk
                chunks[-1] = chunks[-1] + remaining

        return [c for c in chunks if c.strip()]

    def _find_block_end_positions(self, html: str, tags: List[str]) -> set:
        """
        找到所有给定标签的结束位置

        Returns:
            set: 标签结束位置集合
        """
        positions = set()
        for tag in tags:
            for match in re.finditer(re.escape(tag), html):
                positions.add(match.end())
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

        result = []
        current_pos = 0

        for match in matches:
            boundary = match.end()
            sentence = text[current_pos:boundary]

            if count_tokens(sentence) <= token_limit:
                result.append(sentence)
                current_pos = boundary
            else:
                # 这个句子超限
                if result:
                    result.append(sentence)
                    current_pos = boundary
                else:
                    # 第一个句子就超限，硬切
                    sub_chunks = self._hard_split(sentence, token_limit)
                    result.extend(sub_chunks[:-1])
                    current_pos = boundary

        # 处理剩余
        remaining = text[current_pos:]
        if remaining:
            if count_tokens(remaining) <= token_limit:
                result.append(remaining)
            else:
                result.extend(self._hard_split(remaining, token_limit))

        return result

    def _hard_split(self, text: str, token_limit: int) -> List[str]:
        """
        按 token_limit 硬切（极端保底方案）

        从前往后，尽量在 token 限制内填充更多内容
        """
        if count_tokens(text) <= token_limit:
            return [text]

        result = []
        current_pos = 0
        text_len = len(text)

        while current_pos < text_len:
            # 尝试往后扩展到 token_limit
            best_end = current_pos

            for end_pos in range(current_pos + 1, text_len + 1):
                chunk = text[current_pos:end_pos]
                tokens = count_tokens(chunk)

                if tokens <= token_limit:
                    best_end = end_pos
                elif tokens > token_limit:
                    break

            # 如果 best_end 没有前进，强制前进一个字符
            if best_end == current_pos:
                best_end = min(current_pos + 1, text_len)

            result.append(text[current_pos:best_end])
            current_pos = best_end

        return result
