"""
Phase 2.1: Tree-based HTML chunking for EPUB translation.

Provides ChunkState dataclass and three public functions:
- chunk_html(html, token_limit) -> list[ChunkState]
- chunk_tree(root, token_limit) -> list[ChunkState]
- add_context_to_chunks(chunks) -> list[ChunkState]

Core algorithm:
- CONTAINER tags: entire element as 1 chunk (never split)
- LEAF tags: greedy accumulation approaching token_limit
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import tiktoken

from engine.item.token import MAX_TOKEN_LIMIT
from engine.item.tree import TreeNode, parse_html
from engine.schemas.translator import TranslationStatus


# -----------------------------------------------------------------------------
# Tag classification
# -----------------------------------------------------------------------------

# Container tags: entire element becomes 1 chunk (never split)
CONTAINER_TAGS = frozenset({
    'nav', 'ol', 'ul', 'dl', 'div', 'section', 'article',
    'header', 'footer', 'main', 'aside', 'figure',
    'table', 'thead', 'tbody', 'tfoot', 'tr',
    # NCX file tags (lowercase for case-insensitive matching)
    'ncx', 'navmap', 'navpoint',
})

# Leaf tags: greedy accumulation to approach token_limit
_LEAF_TAGS = frozenset({
    'p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'span', 'em', 'strong', 'a', 'code', 'b', 'i', 'u',
    'small', 'sub', 'sup', 'mark', 'del', 'ins',
    'td', 'th', 'br', 'hr', 'img', 'input', 'area',
    'col', 'embed', 'link', 'meta', 'param', 'source', 'track', 'wbr',
})


def _is_leaf_tag(tag: str) -> bool:
    """Return True if tag is a leaf tag that can be accumulated."""
    return tag.lower() in _LEAF_TAGS


def _is_container_tag(tag: str) -> bool:
    """Return True if tag is a container tag (never split)."""
    return tag.lower() in CONTAINER_TAGS


# -----------------------------------------------------------------------------
# ChunkState
# -----------------------------------------------------------------------------

@dataclass
class ChunkState:
    """
    Represents a single translatable chunk extracted from an HTML tree.
    """

    xpath: str
    original: str = ""
    translated: Optional[str] = None
    status: TranslationStatus = TranslationStatus.PENDING
    tokens: int = 0
    needs_translation: bool = True  # 前缀/后缀 chunk 不需要翻译


# -----------------------------------------------------------------------------
# count_tokens
# -----------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    """Calculate token count for text using tiktoken."""
    if not text:
        return 0
    try:
        tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")
    except KeyError:
        tokenizer = tiktoken.get_encoding("cl100k_base")
    return len(tokenizer.encode(text))


# -----------------------------------------------------------------------------
# Helper
# -----------------------------------------------------------------------------

def _node_xpath(node: TreeNode) -> str:
    """Build an absolute XPath-like path for `node`."""
    parts: list[str] = []
    current: Optional[TreeNode] = node
    while current is not None:
        if current.parent is None:
            parts.append(f"/{current.tag}")
            break
        parent = current.parent
        siblings = [
            c for c in parent.children if c.is_element_node() and c.tag == current.tag
        ]
        idx = next((i for i, s in enumerate(siblings) if s is current), -1) + 1
        parts.append(f"/{current.tag}[{idx}]")
        current = parent
    parts.reverse()
    return "".join(parts)


# -----------------------------------------------------------------------------
# Core chunking algorithm
# -----------------------------------------------------------------------------

def chunk_leaves(nodes: list[TreeNode], token_limit: int) -> list[ChunkState]:
    """
    Greedily accumulate leaf nodes into chunks approaching token_limit.

    Each chunk must contain properly closed leaf tags.
    """
    result: list[ChunkState] = []
    current_nodes: list[TreeNode] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current_nodes, current_tokens
        if current_nodes:
            html = "".join(n.to_html() for n in current_nodes)
            result.append(ChunkState(
                xpath=_node_xpath(current_nodes[0]),
                original=html,
                tokens=count_tokens(html),
            ))
            current_nodes = []
            current_tokens = 0

    for node in nodes:
        node_html = node.to_html()
        node_tokens = count_tokens(node_html)

        if node_tokens <= token_limit:
            if current_tokens + node_tokens <= token_limit:
                current_nodes.append(node)
                current_tokens += node_tokens
            else:
                flush()
                current_nodes.append(node)
                current_tokens = node_tokens
        else:
            # Node itself exceeds limit - flush and add as single chunk
            flush()
            result.append(ChunkState(
                xpath=_node_xpath(node),
                original=node_html,
                tokens=node_tokens,
            ))

    flush()
    return result


def _find_body_children(node: TreeNode) -> list[TreeNode] | None:
    """Recursively find <body> and return its children. Returns None if no body found."""
    if node.tag.lower() == "body":
        return node.children

    for child in node.children:
        if child.is_element_node():
            result = _find_body_children(child)
            if result is not None:
                return result
    return None


def _build_prefix_chunk(root: TreeNode) -> tuple[ChunkState | None, list[TreeNode]]:
    """
    Build a prefix chunk from XML declaration, DOCTYPE, and opening tags.

    Returns (prefix_chunk, remaining_children) where remaining_children
    are the content nodes inside <body> (or root children if no body).
    """
    parts = []

    # XML declaration
    if "_xml_declaration" in root.attributes:
        parts.append(root.attributes["_xml_declaration"])

    # DOCTYPE
    if "_doctype" in root.attributes:
        parts.append(root.attributes["_doctype"])

    # Collect opening tags by traversing: html > head > body
    def collect_opening_tags(node: TreeNode, depth: int = 0) -> list[str]:
        """Collect opening tags until body is found. Returns list of tag strings."""
        if depth > 20:  # Safety limit
            return []

        result = []
        for child in node.children:
            if not child.is_element_node():
                continue

            tag = child.tag.lower()
            if tag == "html":
                # Get html opening tag
                child_html = child.to_html()
                gt_pos = child_html.find(">")
                if gt_pos > 0:
                    result.append(child_html[:gt_pos + 1])
                # Recurse into html to find head/body
                inner = collect_opening_tags(child, depth + 1)
                result.extend(inner)
            elif tag == "head":
                # Get full head element (opening + content + closing)
                child_html = child.to_html()
                result.append(child_html)
                # Continue to find body (don't recurse into head's children)
            elif tag == "body":
                # Get body opening tag
                child_html = child.to_html()
                gt_pos = child_html.find(">")
                if gt_pos > 0:
                    result.append(child_html[:gt_pos + 1])
                # Don't recurse further - we've found body
                return result

        return result

    opening_tags = collect_opening_tags(root)
    parts.extend(opening_tags)

    # Find body children for content
    body_children = _find_body_children(root)

    if not parts:
        # No prefix needed, return original children
        return None, root.children

    prefix_html = "\n".join(parts)
    prefix_chunk = ChunkState(
        xpath="prefix",
        original=prefix_html,
        tokens=count_tokens(prefix_html),
        needs_translation=False,
    )

    # If no body found, use root's element children as content
    # This handles NCX files and other XML formats without body
    content_children = body_children if body_children is not None else root.children

    return prefix_chunk, content_children


def _build_suffix_chunk() -> ChunkState:
    """
    Build a suffix chunk for </body></html>.

    Returns a ChunkState with needs_translation=False.
    """
    suffix_html = "</body>\n</html>"
    return ChunkState(
        xpath="suffix",
        original=suffix_html,
        tokens=count_tokens(suffix_html),
        needs_translation=False,
    )


def _chunk_container_children(node: TreeNode, token_limit: int) -> list[ChunkState]:
    """
    Recursively chunk container's children when container exceeds token_limit.

    This is a helper for chunk_tree - does NOT produce prefix/suffix chunks.
    """
    result: list[ChunkState] = []
    pending_leaves: list[TreeNode] = []

    def flush_leaves() -> None:
        nonlocal pending_leaves
        if pending_leaves:
            result.extend(chunk_leaves(pending_leaves, token_limit))
            pending_leaves = []

    for child in node.children:
        if not child.is_element_node():
            continue

        tag = child.tag.lower()

        if _is_container_tag(tag):
            flush_leaves()
            child_html = child.to_html()
            child_tokens = count_tokens(child_html)

            if child_tokens > token_limit:
                result.extend(_chunk_container_children(child, token_limit))
            else:
                result.append(ChunkState(
                    xpath=_node_xpath(child),
                    original=child_html,
                    tokens=child_tokens,
                ))
        else:
            pending_leaves.append(child)

    flush_leaves()
    return result


def chunk_tree(root: TreeNode, token_limit: int) -> list[ChunkState]:
    """
    Chunk a TreeNode tree with container/leaf tag classification.

    Algorithm:
    - CONTAINER tags (nav, ol, ul, div, etc.): entire element as 1 chunk
    - LEAF tags (p, li, h1-h6, span, etc.): greedy accumulation via chunk_leaves()
    - Prefix/suffix chunks for non-translatable parts (needs_translation=False)

    Args:
        root: a TreeNode tree (synthetic <div> from parse_html).
        token_limit: max estimated tokens per ChunkState.

    Returns:
        List of ChunkState objects, in document order.
    """
    if not root.children:
        return []

    result: list[ChunkState] = []

    # Build prefix chunk and get remaining children (excluding html/head/body wrappers)
    prefix_chunk, remaining_children = _build_prefix_chunk(root)
    if prefix_chunk:
        result.append(prefix_chunk)

    # Determine if we have a body element (for suffix)
    body_children = _find_body_children(root)
    has_body = body_children is not None

    pending_leaves: list[TreeNode] = []

    def flush_leaves() -> None:
        nonlocal pending_leaves
        if pending_leaves:
            result.extend(chunk_leaves(pending_leaves, token_limit))
            pending_leaves = []

    for child in remaining_children:
        if not child.is_element_node():
            continue

        tag = child.tag.lower()

        if _is_container_tag(tag):
            # Flush any pending leaves first
            flush_leaves()
            # Container tag: check if exceeds limit
            child_html = child.to_html()
            child_tokens = count_tokens(child_html)

            if child_tokens > token_limit:
                # Container exceeds limit: recursively chunk its children
                child_result = _chunk_container_children(child, token_limit)
                result.extend(child_result)
            else:
                # Container fits within limit: entire element as 1 chunk
                result.append(ChunkState(
                    xpath=_node_xpath(child),
                    original=child_html,
                    tokens=child_tokens,
                ))
        else:
            # Leaf tag: accumulate for greedy batching
            pending_leaves.append(child)

    # Flush remaining leaves
    flush_leaves()

    # Append suffix chunk only if we had a body (HTML files)
    if has_body:
        result.append(_build_suffix_chunk())

    # Merge small adjacent content chunks (greedy approach)
    result = _merge_small_adjacent_chunks(result, token_limit)

    return result


def _get_parent_xpath(xpath: str) -> str:
    """Extract parent xpath by removing the last segment."""
    last_slash = xpath.rfind("/")
    if last_slash <= 0:
        return ""
    return xpath[:last_slash]


def _is_container_chunk(chunk: ChunkState) -> bool:
    """Check if chunk is from a container tag (should not be merged)."""
    if chunk.xpath in ("prefix", "suffix"):
        return False
    # Extract tag from xpath like "/div/div[1]" -> "div"
    last_seg = chunk.xpath.rsplit("/", 1)[-1]
    # Remove index like "div[1]" -> "div"
    tag = last_seg.split("[")[0]
    return _is_container_tag(tag)


def _merge_small_adjacent_chunks(chunks: list[ChunkState], token_limit: int) -> list[ChunkState]:
    """
    Merge small adjacent content chunks into larger chunks up to token_limit.

    Only merges chunks that are content (not prefix/suffix).
    Only merges LEAF chunks (containers are kept intact).
    Only merges chunks from the SAME parent xpath.
    """
    if len(chunks) <= 2:
        return chunks  # Can't merge if too few chunks

    result: list[ChunkState] = []
    i = 0

    while i < len(chunks):
        chunk = chunks[i]

        # Skip prefix/suffix chunks
        if chunk.xpath in ("prefix", "suffix"):
            result.append(chunk)
            i += 1
            continue

        # Containers are never merged
        if _is_container_chunk(chunk):
            result.append(chunk)
            i += 1
            continue

        # For leaf chunks, try to merge with next adjacent leaf chunks
        if chunk.needs_translation:
            merged_html = chunk.original
            merged_tokens = chunk.tokens
            merged_xpath = chunk.xpath
            parent_xpath = _get_parent_xpath(chunk.xpath)
            j = i + 1

            # Greedily merge while under token_limit AND from same parent
            while j < len(chunks):
                next_chunk = chunks[j]
                if next_chunk.xpath in ("prefix", "suffix"):
                    break
                if not next_chunk.needs_translation:
                    break

                # Don't merge containers
                if _is_container_chunk(next_chunk):
                    break

                # Only merge if same parent xpath
                next_parent = _get_parent_xpath(next_chunk.xpath)
                if next_parent != parent_xpath:
                    break

                # Check if adding this chunk would stay under limit
                combined_tokens = merged_tokens + next_chunk.tokens
                if combined_tokens <= token_limit:
                    merged_html += next_chunk.original
                    merged_tokens = combined_tokens
                    j += 1
                else:
                    break

            if j > i + 1:
                # We merged something
                result.append(ChunkState(
                    xpath=merged_xpath,
                    original=merged_html,
                    tokens=merged_tokens,
                    needs_translation=True,
                ))
                i = j
            else:
                result.append(chunk)
                i += 1
        else:
            result.append(chunk)
            i += 1

    return result


def chunk_html(html: str, token_limit: int = MAX_TOKEN_LIMIT) -> list[ChunkState]:
    """
    Convenience wrapper: parse an HTML string then call chunk_tree.

    Args:
        html: raw HTML string (may be empty).
        token_limit: max tokens per chunk (default: MAX_TOKEN_LIMIT).

    Returns:
        List of ChunkState objects, in document order.
    """
    if not html or not html.strip():
        return []
    root = parse_html(html)
    return chunk_tree(root, token_limit)


def add_context_to_chunks(chunks: list[ChunkState]) -> list[ChunkState]:
    """
    Pass-through function for backward compatibility.
    Context is now handled directly by the LLM during translation.
    """
    return chunks
