"""
Phase 1.1: HTML tree representation for EPUB translation.

Provides a TreeNode dataclass and utilities for parsing, serializing,
and navigating HTML documents.
"""

from dataclasses import dataclass, field
from html.entities import name2codepoint
from html.parser import HTMLParser
from typing import Optional
import re


def encode_entities(text: str) -> str:
    """Encode characters that are special in HTML (& < > \" )."""
    result = []
    for ch in text:
        if ch == "&":
            result.append("&amp;")
        elif ch == "<":
            result.append("&lt;")
        elif ch == ">":
            result.append("&gt;")
        elif ch == '"':
            result.append("&quot;")
        else:
            result.append(ch)
    return "".join(result)


def decode_entities(text: str) -> str:
    """Decode named HTML entities (e.g. &amp; &lt;)."""
    result = []
    i = 0
    while i < len(text):
        if text[i] == "&":
            # Scan for semicolon-terminated entity
            semicolon = text.find(";", i)
            if semicolon != -1:
                entity = text[i + 1 : semicolon]
                if entity in name2codepoint:
                    result.append(chr(name2codepoint[entity]))
                    i = semicolon + 1
                    continue
                elif entity.startswith("#") and entity[1:].isdigit():
                    result.append(chr(int(entity[1:])))
                    i = semicolon + 1
                    continue
        result.append(text[i])
        i += 1
    return "".join(result)


# -----------------------------------------------------------------------------
# Self-closing tags
# -----------------------------------------------------------------------------

SELF_CLOSING_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
})


# -----------------------------------------------------------------------------
# TreeNode
# -----------------------------------------------------------------------------

@dataclass
class TreeNode:
    """
    Represents a node in an HTML document tree.

    Fields:
        tag: element tag name (e.g. "p", "div"). Empty string for text nodes.
        attributes: ordered dict-like mapping of attribute names to values.
        children: list of child TreeNode objects.
        parent: reference to the parent TreeNode (None for root).
        text: text content. Only non-empty for text nodes (tag == "").
    """

    tag: str
    attributes: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: Optional["TreeNode"] = None
    text: str = ""

    # -------------------------------------------------------------------------
    # Node-type helpers
    # -------------------------------------------------------------------------

    def is_text_node(self) -> bool:
        """Return True for text nodes (no tag, just text content)."""
        return self.tag == "" and self.text != ""

    def is_element_node(self) -> bool:
        """Return True for element nodes (non-empty tag)."""
        return self.tag != ""

    def is_self_closing(self) -> bool:
        """Return True if this element is self-closing (e.g. <br>)."""
        return self.tag.lower() in SELF_CLOSING_TAGS

    # -------------------------------------------------------------------------
    # Traversal
    # -------------------------------------------------------------------------

    def _collect_text_nodes(self) -> list["TreeNode"]:
        """Return all descendant text nodes in document order."""
        results: list[TreeNode] = []
        stack = list(self.children)
        while stack:
            node = stack.pop(0)
            if node.is_text_node():
                results.append(node)
            stack = node.children + stack
        return results

    # -------------------------------------------------------------------------
    # Serialisation
    # -------------------------------------------------------------------------

    def to_html(self) -> str:
        """Recursively serialize this node and its children to an HTML string."""
        if self.is_text_node():
            return encode_entities(self.text)

        parts = []
        # Include XML declaration and DOCTYPE for root node
        if self.tag == "div" and "_xml_declaration" in self.attributes:
            parts.append(self.attributes["_xml_declaration"] + "\n")
        if self.tag == "div" and "_doctype" in self.attributes:
            parts.append(self.attributes["_doctype"] + "\n")

        parts.append(f"<{self.tag}")
        for k, v in self.attributes.items():
            if k.startswith("_"):
                continue  # Skip special attributes
            parts.append(f' {k}="{encode_entities(v)}"')
        if self.is_self_closing() and not self.children:
            parts.append(" />")
            return "".join(parts)
        parts.append(">")

        for child in self.children:
            parts.append(child.to_html())

        parts.append(f"</{self.tag}>")
        return "".join(parts)


# -----------------------------------------------------------------------------
# html.parser integration
# -----------------------------------------------------------------------------

# Tags that are void per spec (never have closing tags)
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
})


class _TreeBuilder(HTMLParser):
    """
    Builds a TreeNode tree from raw HTML using Python's html.parser.

    Uses a div wrapper so a single root is always returned.
    """

    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True (default): C layer decodes entities
        self.root = TreeNode(tag="div", attributes={}, children=[], parent=None, text="")
        self.stack: list[TreeNode] = [self.root]
        self._open_tags: list[str] = []
        self._xml_declaration: str = ""
        self._doctype: str = ""

    @property
    def _current(self) -> TreeNode:
        return self.stack[-1]

    def _push(self, node: TreeNode) -> None:
        node.parent = self._current
        self._current.children.append(node)
        if node.is_element_node() and not node.is_self_closing():
            self.stack.append(node)
            self._open_tags.append(node.tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        node = TreeNode(
            tag=tag.lower(),
            attributes=attr_dict,
            children=[],
            parent=None,
            text="",
        )
        if tag.lower() in _VOID_TAGS:
            # Void elements are always leaf nodes
            self._push(node)
        else:
            self._push(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._open_tags:
            return
        if tag != self._open_tags[-1]:
            # Mismatched close tag: ignore and let the parser recover
            return
        self.stack.pop()
        self._open_tags.pop()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        # The C layer has already decoded entities/charrefs when convert_charrefs=True.
        node = TreeNode(
            tag="",
            attributes={},
            children=[],
            parent=None,
            text=data,
        )
        node.parent = self._current
        self._current.children.append(node)

    def handle_decl(self, decl: str) -> None:
        """Handle DOCTYPE declarations and other declarations."""
        decl = decl.strip()
        if decl.lower().startswith("doctype"):
            self._doctype = f"<!{decl}>"

    def handle_pi(self, data: str) -> None:
        """Handle processing instructions (e.g., XML declaration)."""
        data = data.strip()
        if data.lower().startswith("xml "):
            # data contains "xml version=\"1.0\" encoding=\"UTF-8\"?",
            # we need to add <? and ?> to form the complete declaration
            self._xml_declaration = f"<?{data}>"


def parse_html(html: str) -> TreeNode:
    """
    Parse a raw HTML string into a TreeNode tree.

    A synthetic <div> root is always returned. Text nodes are represented
    as TreeNode instances with tag="" and non-empty text.
    The root node may have special attributes '_xml_declaration' and '_doctype'
    if those were present in the original HTML.
    """
    builder = _TreeBuilder()
    builder.feed(html)
    # Store XML declaration and DOCTYPE in the root node's attributes
    if builder._xml_declaration:
        builder.root.attributes["_xml_declaration"] = builder._xml_declaration
    if builder._doctype:
        builder.root.attributes["_doctype"] = builder._doctype
    return builder.root


# -----------------------------------------------------------------------------
# XPath navigation (absolute paths only)
# -----------------------------------------------------------------------------

_XPATH_STEP = re.compile(r"/([a-zA-Z][a-zA-Z0-9]*)(?:\[(\d+)\])?")


def find_by_xpath(root: TreeNode, xpath: str) -> Optional[TreeNode]:
    """
    Find a node by an absolute XPath-like path.

    Supported syntax:
        /tag/tag/tag          – first child matching each tag
        /tag[2]              – second child (1-based)

    Relative paths (starting with //) are NOT supported.
    Returns None if the path does not resolve.
    """
    if not xpath.startswith("/") or xpath.startswith("//"):
        return None

    parts = _XPATH_STEP.findall(xpath)
    if not parts:
        return None

    current: list[TreeNode] = [root]
    for tag, index_str in parts:
        tag = tag.lower()
        index: Optional[int] = int(index_str) if index_str else None
        next_current: list[TreeNode] = []
        for node in current:
            for child in node.children:
                if child.is_element_node() and child.tag == tag:
                    next_current.append(child)
        if not next_current:
            return None
        if index is not None:
            # 1-based index
            idx = index - 1
            if 0 <= idx < len(next_current):
                current = [next_current[idx]]
            else:
                return None
        else:
            current = next_current

    return current[0] if current else None
