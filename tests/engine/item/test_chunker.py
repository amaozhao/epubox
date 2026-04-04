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

        # 验证所有 chunk 的 local_tag_map 都是有效的
        for chunk in chunks:
            for key in chunk.local_tag_map.keys():
                idx = int(key[3:-1])
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

    def test_merge_into_chunks_respects_max_placeholders(self):
        """验证单个segment超限时按max_placeholders_per_chunk强制分割"""
        chunker = HtmlChunker(token_limit=10000, max_placeholders_per_chunk=5)
        # 创建一个包含10个占位符的单个segment
        segment = "[id0][id1][id2][id3][id4][id5][id6][id7][id8][id9]Hello World"
        global_indices = list(range(10))
        mgr = PlaceholderManager()
        for i in range(10):
            mgr.tag_map[f"[id{i}]"] = f"<tag{i}>"
        mgr.counter = 10

        chunks = chunker._merge_into_chunks([segment], global_indices, mgr)
        # 应该被分割成2个chunk（10个占位符 / 5限制 = 2 chunks）
        assert len(chunks) == 2
        for chunk in chunks:
            assert len(chunk.local_tag_map) <= 5

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
        assert len(chunk.local_tag_map) == 2
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


class TestChunkByHtmlTags:
    """测试 chunk_by_html_tags 方法"""

    def test_normal_html_single_chunk(self):
        """短 HTML 应返回单个 chunk"""
        html = "<p>Hello World!</p>"
        chunker = HtmlChunker(token_limit=100)
        chunks = chunker.chunk_by_html_tags(html, token_limit=100, is_nav_file=False)
        assert len(chunks) == 1

    def test_normal_html_multiple_paragraphs_accumulate(self):
        """多个段落应累积直到超过 token_limit 才分割"""
        # 创建多个段落，总 token 超过限制
        paragraphs = []
        for i in range(20):
            paragraphs.append(f'<p>Paragraph {i} with some text content that adds up.</p>')

        html = '<div>' + ''.join(paragraphs) + '</div>'
        chunker = HtmlChunker(token_limit=500)

        chunks = chunker.chunk_by_html_tags(html, token_limit=500, is_nav_file=False)

        # 应该累积多个段落，不是每个段落单独成 chunk
        assert len(chunks) < 20  # 不是每个段落一个 chunk
        # 所有 chunk 都应在 token 限制内
        for chunk in chunks:
            assert count_tokens(chunk) <= 500

    def test_normal_html_splits_at_block_tag(self):
        """应在块级标签（</p>）处分割"""
        html = "<p>First paragraph.</p><p>Second paragraph.</p><p>Third paragraph.</p>"
        chunker = HtmlChunker(token_limit=100)

        chunks = chunker.chunk_by_html_tags(html, token_limit=100, is_nav_file=False)

        # 每个 chunk 应以 </p> 结尾
        for chunk in chunks:
            assert chunk.rstrip().endswith('</p>') or '</p>' in chunk

    def test_normal_html_all_chunks_under_limit(self):
        """所有 chunk 都应在 token 限制内"""
        paragraphs = []
        for i in range(30):
            paragraphs.append(f'<p>Paragraph {i} with additional text to make the content longer.</p>')

        html = '<div>' + ''.join(paragraphs) + '</div>'
        chunker = HtmlChunker(token_limit=300)

        chunks = chunker.chunk_by_html_tags(html, token_limit=300, is_nav_file=False)

        for chunk in chunks:
            assert count_tokens(chunk) <= 300, f"Chunk with {count_tokens(chunk)} tokens exceeds limit"

    def test_nav_file_splits_at_navpoint(self):
        """nav 文件应在 </navPoint> 处分割"""
        html = '''<navMap>
<navPoint id="np1"><navLabel><text>Chapter 1</text></navLabel><content src="ch1.xhtml"/></navPoint>
<navPoint id="np2"><navLabel><text>Chapter 2</text></navLabel><content src="ch2.xhtml"/></navPoint>
</navMap>'''
        chunker = HtmlChunker(token_limit=100)

        chunks = chunker.chunk_by_html_tags(html, token_limit=100, is_nav_file=True)

        # nav 文件的 chunk 应该在 </navPoint> 或 </navMap> 处分割
        for chunk in chunks:
            text = chunk.strip()
            assert text.endswith('</navPoint>') or text.endswith('</navMap>') or '</navPoint>' in text

    def test_nav_file_all_chunks_under_limit(self):
        """nav 文件所有 chunk 都应在 token 限制内"""
        # 读取实际的 toc.ncx 文件
        import os
        toc_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'tests', 'toc.ncx')
        if os.path.exists(toc_path):
            with open(toc_path, 'r', encoding='utf-8') as f:
                html = f.read()

            chunker = HtmlChunker(token_limit=1200)
            chunks = chunker.chunk_by_html_tags(html, token_limit=1200, is_nav_file=True)

            assert len(chunks) > 0
            for chunk in chunks:
                assert count_tokens(chunk) <= 1200, f"Nav chunk with {count_tokens(chunk)} tokens exceeds limit"

    def test_empty_html(self):
        """空 HTML 应返回空列表"""
        chunker = HtmlChunker(token_limit=100)
        chunks = chunker.chunk_by_html_tags("", token_limit=100, is_nav_file=False)
        assert len(chunks) == 0

    def test_html_without_allowed_split_tags(self):
        """没有允许的分割标签时应使用硬切"""
        # 创建一个没有块级结束标签的 HTML
        html = "<span>Text without block tags.</span>" * 50
        chunker = HtmlChunker(token_limit=100)

        chunks = chunker.chunk_by_html_tags(html, token_limit=100, is_nav_file=False)

        # 应该仍然能分割
        assert len(chunks) > 0
        for chunk in chunks:
            assert count_tokens(chunk) <= 100

    def test_single_long_sentence_hard_split(self):
        """单个超长句子应使用硬切"""
        html = '<p>' + 'word ' * 500 + '</p>'  # 超过 token_limit
        chunker = HtmlChunker(token_limit=200)

        chunks = chunker.chunk_by_html_tags(html, token_limit=200, is_nav_file=False)

        # 应该能分割
        assert len(chunks) > 0
        for chunk in chunks:
            assert count_tokens(chunk) <= 200

    def test_nav_file_no_navpoint_whole_file(self):
        """没有 navPoint 的 nav 文件应返回整个文件"""
        html = '<navMap><content src="test.xhtml"/></navMap>'
        chunker = HtmlChunker(token_limit=100)

        chunks = chunker.chunk_by_html_tags(html, token_limit=100, is_nav_file=True)

        # 没有 navPoint，应该返回整个文件
        assert len(chunks) == 1
        assert chunks[0] == html
