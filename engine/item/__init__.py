# Phase 1 infrastructure
from .tree import TreeNode, parse_html, find_by_xpath, encode_entities, decode_entities
from .xpath import is_valid_xpath, parse_xpath
from .token import estimate_tokens, MAX_TOKEN_LIMIT

# Phase 2 chunking
from .chunker import chunk_html, chunk_tree, add_context_to_chunks, ChunkState

# Legacy exports
from .chunker import count_tokens
from .merger import Merger
from .precode import PreCodeExtractor

__all__ = [
    # Phase 1
    "TreeNode",
    "parse_html",
    "find_by_xpath",
    "encode_entities",
    "decode_entities",
    "is_valid_xpath",
    "parse_xpath",
    "estimate_tokens",
    "MAX_TOKEN_LIMIT",
    # Phase 2
    "chunk_html",
    "chunk_tree",
    "add_context_to_chunks",
    "ChunkState",
    # Legacy
    "count_tokens",
    "Merger",
    "PreCodeExtractor",
]
