"""
Phase 2.1: Tree-based HTML chunking for EPUB translation.

Provides ChunkState dataclass and three public functions:
- chunk_html(html, token_limit) -> list[ChunkState]
- chunk_tree(root, token_limit) -> list[ChunkState]
- add_context_to_chunks(chunks) -> list[ChunkState]

Core algorithm: recursive tree traversal + greedy merging.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import tiktoken

from engine.item.token import MAX_TOKEN_LIMIT
from engine.item.tree import TreeNode, parse_html
from engine.schemas.translator import TranslationStatus


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

def _chunk_subtree(node: TreeNode, token_limit: int) -> list[ChunkState]:
    """
    Recursively chunk a node's children into token-limited chunks.

    Returns list of ChunkStates in document order.
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

    for child in node.children:
        if not child.is_element_node():
            continue

        child_html = child.to_html()
        child_tokens = count_tokens(child_html)

        if child_tokens <= token_limit:
            # Child fits - try to add to current group
            if current_tokens + child_tokens <= token_limit:
                current_nodes.append(child)
                current_tokens += child_tokens
            else:
                # Doesn't fit - flush current and start new group with this child
                flush()
                current_nodes.append(child)
                current_tokens = child_tokens
        else:
            # Child exceeds limit - flush current, recursively chunk this child
            flush()
            sub_chunks = _chunk_subtree(child, token_limit)
            if sub_chunks:
                # 有子分块则使用
                result.extend(sub_chunks)
            else:
                # 没有子分块（如叶子节点），直接添加整个 child
                # 注意：存储实际 token 数，不裁剪（叶节点无法拆分）
                result.append(ChunkState(
                    xpath=_node_xpath(child),
                    original=child.to_html(),
                    tokens=child_tokens,
                ))

    flush()
    return result


def chunk_tree(root: TreeNode, token_limit: int) -> list[ChunkState]:
    """
    Chunk a TreeNode tree using recursive traversal and greedy merging.

    Algorithm (from refactoring doc):
    1. Start from root's direct element children
    2. For each child:
       - If it fits in current chunk, add it
       - If it doesn't fit, flush current chunk and start new one
       - If child itself exceeds token_limit, recursively chunk its children
    3. When chunk reaches token_limit, flush it and continue

    Args:
        root: a TreeNode tree (synthetic <div> from parse_html).
        token_limit: max estimated tokens per ChunkState.

    Returns:
        List of ChunkState objects, in document order.
    """
    if not root.children:
        return []
    return _chunk_subtree(root, token_limit)


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
