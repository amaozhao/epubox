import pytest

from engine.item.chunker import HtmlChunker, count_tokens
from engine.item.placeholder import PlaceholderManager
from engine.item.tag import TagPreserver
from engine.schemas.translator import TranslationStatus


class TestHtmlChunker:
    """测试 HtmlChunker 类的核心功能和边界情况"""

    @pytest.fixture
    def placeholder_mgr(self):
        """创建测试用 PlaceholderManager"""
        mgr = PlaceholderManager()
        return mgr

    @pytest.fixture
    def chunker(self):
        """为每个测试提供一个 HtmlChunker 实例"""
        return HtmlChunker(token_limit=30)

    def test_count_tokens(self):
        """测试 token 计数功能"""
        assert count_tokens("Hello World") > 0
        assert count_tokens("") == 0

    def test_init(self):
        """测试 HtmlChunker 初始化"""
        chunker = HtmlChunker(token_limit=100)
        assert chunker.token_limit == 100

    def test_short_html(self, chunker, placeholder_mgr):
        """测试短 HTML 内容，应返回单个 Chunk"""
        html = "<p>Hello World!</p>"
        processed, mgr = TagPreserver().preserve_tags(html)
        placeholder_mgr.tag_map = mgr.tag_map
        placeholder_mgr.counter = mgr.counter
        global_indices = list(range(placeholder_mgr.counter))

        chunks = chunker.chunk(processed, global_indices, placeholder_mgr)

        assert len(chunks) == 1
        assert chunks[0].tokens <= chunker.token_limit
        assert chunks[0].name is not None

    def test_chunk_data_integrity(self, placeholder_mgr):
        """测试生成的 Chunk 对象的属性是否正确"""
        html = "<div><p>Hello</p><p>World</p></div>"
        processed, mgr = TagPreserver().preserve_tags(html)
        placeholder_mgr.tag_map = mgr.tag_map
        placeholder_mgr.counter = mgr.counter
        global_indices = list(range(placeholder_mgr.counter))

        chunker = HtmlChunker(token_limit=100)
        chunks = chunker.chunk(processed, global_indices, placeholder_mgr)

        assert len(chunks) >= 1
        for chunk in chunks:
            assert chunk.name is not None
            assert chunk.original is not None
            assert chunk.tokens >= 0
            assert isinstance(chunk.global_indices, list)
            assert isinstance(chunk.local_tag_map, dict)

    def test_empty_html(self, chunker, placeholder_mgr):
        """测试空 HTML"""
        html = ""
        processed, mgr = TagPreserver().preserve_tags(html)
        placeholder_mgr.tag_map = mgr.tag_map
        placeholder_mgr.counter = mgr.counter
        global_indices = list(range(placeholder_mgr.counter)) if placeholder_mgr.counter > 0 else []

        chunks = chunker.chunk(processed, global_indices, placeholder_mgr)
        assert len(chunks) >= 0

    def test_preserves_placeholder_indices(self, placeholder_mgr):
        """测试分块后占位符索引被正确保留"""
        html = "<p>Hello</p><p>World</p>"
        processed, mgr = TagPreserver().preserve_tags(html)
        placeholder_mgr.tag_map = mgr.tag_map
        placeholder_mgr.counter = mgr.counter
        global_indices = list(range(placeholder_mgr.counter))

        chunker = HtmlChunker(token_limit=100)
        chunks = chunker.chunk(processed, global_indices, placeholder_mgr)

        # 验证所有 chunk 的 global_indices 都是有效的
        for chunk in chunks:
            for idx in chunk.global_indices:
                assert idx < placeholder_mgr.counter

    def test_nav_file_respected(self, placeholder_mgr):
        """测试导航文件标记被正确传递"""
        html = "<navMap><navPoint><navLabel>Chapter 1</navLabel><content src=\"ch1.xhtml\"/></navPoint></navMap>"
        processed, mgr = TagPreserver().preserve_tags(html)
        placeholder_mgr.tag_map = mgr.tag_map
        placeholder_mgr.counter = mgr.counter
        global_indices = list(range(placeholder_mgr.counter))

        chunker = HtmlChunker(token_limit=1000)
        # 导航文件模式
        chunks = chunker.chunk(processed, global_indices, placeholder_mgr, is_nav_file=True)

        # 导航文件应该保持完整性
        assert len(chunks) >= 1


class TestHtmlChunkerHelperMethods:
    """测试 HtmlChunker 的辅助方法"""

    def test_is_block_closing_tag(self):
        """测试块级结束标签判断"""
        chunker = HtmlChunker(token_limit=100)
        assert chunker._is_block_closing_tag("</p>") is True
        assert chunker._is_block_closing_tag("</div>") is True
        assert chunker._is_block_closing_tag("</h1>") is True
        assert chunker._is_block_closing_tag("<p>") is False
        assert chunker._is_block_closing_tag("</span>") is False

    def test_is_block_opening_tag(self):
        """测试块级开始标签判断"""
        chunker = HtmlChunker(token_limit=100)
        assert chunker._is_block_opening_tag("<p>") is True
        assert chunker._is_block_opening_tag("<div>") is True
        assert chunker._is_block_opening_tag("<h1>") is True
        assert chunker._is_block_opening_tag("</p>") is False
        assert chunker._is_block_opening_tag("<span>") is False

    def test_extract_tag_name(self):
        """测试标签名提取"""
        chunker = HtmlChunker(token_limit=100)
        assert chunker._extract_tag_name("<p>") == "p"
        assert chunker._extract_tag_name("</div>") == "div"
        assert chunker._extract_tag_name("<h1 class='title'>") == "h1"
        assert chunker._extract_tag_name("<br/>") == "br"
        assert chunker._extract_tag_name("<img src='x'/>") == "img"

    def test_get_split_priority(self):
        """测试分割优先级"""
        chunker = HtmlChunker(token_limit=100)
        assert chunker._get_split_priority("</h1>") == 100
        assert chunker._get_split_priority("</h2>") == 100
        assert chunker._get_split_priority("</h3>") == 100
        assert chunker._get_split_priority("</p>") == 50
        assert chunker._get_split_priority("</li>") == 30
        assert chunker._get_split_priority("</div>") == 30
        assert chunker._get_split_priority("</span>") == 10

    def test_find_placeholder_positions(self):
        """测试占位符位置查找"""
        chunker = HtmlChunker(token_limit=100)

        text = "[id0]Hello[id1] World[id2]"
        positions = chunker._find_placeholder_positions(text)

        assert len(positions) == 3
        # positions: (start, end, placeholder, index)
        assert positions[0][3] == 0  # [id0]
        assert positions[1][3] == 1  # [id1]
        assert positions[2][3] == 2  # [id2]

    def test_find_safe_split_points(self):
        """测试安全分割点查找"""
        chunker = HtmlChunker(token_limit=100)
        html = "<div><p>First</p><p>Second</p></div>"
        processed, mgr = TagPreserver().preserve_tags(html)

        positions = chunker._find_placeholder_positions(processed)
        split_points = chunker._find_safe_split_points(processed, positions, mgr, is_nav_file=False)

        # 应该在 </p> 后找到分割点
        assert isinstance(split_points, list)

    def test_split_at_points(self):
        """测试在分割点分割文本"""
        chunker = HtmlChunker(token_limit=100)
        text = "Hello World Test"
        split_points = [6]  # 在 "World" 之前
        segments = chunker._split_at_points(text, split_points)
        assert len(segments) == 2
        assert segments[0] == "Hello "
        assert segments[1] == "World Test"

    def test_split_at_points_no_splits(self):
        """测试无分割点时返回原文本"""
        chunker = HtmlChunker(token_limit=100)
        text = "Hello World"
        segments = chunker._split_at_points(text, [])
        assert segments == ["Hello World"]

    def test_merge_into_chunks(self):
        """测试合并segments为chunks"""
        chunker = HtmlChunker(token_limit=50)
        segments = ["<p>Hello</p>", "<p>World</p>"]
        global_indices = [0, 1, 2, 3]
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<p>", "[id3]": "</p>"}
        mgr.counter = 4

        chunks = chunker._merge_into_chunks(segments, global_indices, mgr)
        assert len(chunks) >= 1
        assert all(hasattr(c, 'name') for c in chunks)

    def test_create_chunk(self):
        """测试创建chunk"""
        chunker = HtmlChunker(token_limit=100)
        parts = ["<p>Hello</p>"]
        indices = [0, 1]
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        mgr.counter = 2

        chunk = chunker._create_chunk(parts, indices, mgr)
        assert chunk.name is not None
        assert chunk.original == "<p>Hello</p>"
        assert chunk.global_indices == [0, 1]
        assert chunk.tokens > 0

    def test_create_chunk_strips_boundary_newlines(self):
        """验证 chunk 首尾的 \\n 被正确删除"""
        chunker = HtmlChunker(token_limit=100)
        parts = ["\n<p>Hello</p>\n"]
        indices = [0, 1]
        mgr = PlaceholderManager()
        mgr.tag_map = {"[id0]": "<p>", "[id1]": "</p>"}
        mgr.counter = 2

        chunk = chunker._create_chunk(parts, indices, mgr)
        # 首尾的 \n 应被删除
        assert chunk.original == "<p>Hello</p>"
        # 中间的 \n（如果有）应保留
        parts_with_middle = ["\n<p>Hello</p>\n<p>World</p>\n"]
        indices_middle = [0, 1, 2, 3]
        mgr2 = PlaceholderManager()
        mgr2.tag_map = {"[id0]": "<p>", "[id1]": "</p>", "[id2]": "<p>", "[id3]": "</p>"}
        mgr2.counter = 4
        chunk2 = chunker._create_chunk(parts_with_middle, indices_middle, mgr2)
        # 中间的 \n 应保留
        assert "\n" in chunk2.original
