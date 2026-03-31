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

    def __init__(self, token_limit: int = 3000, max_placeholders_per_chunk: int = 30):
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

            # 检查是否是块级结束标签
            if self._is_block_closing_tag(tag_content):
                # 检查下一个是否是块级开始标签
                if i + 1 < len(placeholder_positions):
                    next_placeholder = placeholder_positions[i + 1][2]
                    next_tag = placeholder_mgr.tag_map.get(next_placeholder, "")
                    if self._is_block_opening_tag(next_tag):
                        # 找到一个安全分割点
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

            # 检查两个条件：token限制 AND 占位符数量限制
            # 或者当前chunk为空（强制接受第一个segment）
            if (test_tokens <= self.token_limit and
                len(current_indices) + len(new_indices) <= self.max_placeholders_per_chunk) or not current_chunk_parts:
                # 可以加入
                current_chunk_parts.append(segment)
                # 收集这个segment中的占位符索引
                for ph in self.placeholder_pattern.findall(segment):
                    idx = int(ph[3:-1])
                    if idx not in current_indices:
                        current_indices.append(idx)
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
                # 收集这个segment中的占位符索引
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
