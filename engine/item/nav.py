"""
Phase 2.2: EPUB Navigation File Parser for Translation.

Handles toc.ncx (NCX format) and nav.xhtml (XHTML nav format) files.
"""

import re
from typing import Dict, List

import xml.etree.ElementTree as ET


# XML namespaces used in EPUB nav files
# Note: NCX namespace has trailing slash, XHTML namespace does not
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XHTML_NS = "http://www.w3.org/1999/xhtml"


def is_nav_file(html: str) -> bool:
    """
    Check if HTML content is an EPUB navigation file.

    Supports:
    - NCX format (toc.ncx): root element is <ncx>
    - XHTML nav format (nav.xhtml): contains <nav> with <ol><li> structure
      (either as root element or as html>body>nav descendant)

    Args:
        html: Raw HTML/XML content string

    Returns:
        True if this is a recognized EPUB nav file, False otherwise
    """
    stripped = html.strip()
    if not stripped:
        return False

    # NCX format detection via regex (works even without closing tag)
    # Match <ncx ...> or <ncx> (with optional xmlns attribute)
    if re.search(r'<ncx(?:\s[^>]*)?>', stripped, re.IGNORECASE):
        return True

    # XHTML nav format detection via regex first
    # Look for <nav ...> with required ol>li structure
    if "<nav" not in stripped.lower():
        return False

    # Try XML parsing for XHTML nav format
    try:
        root = ET.fromstring(stripped)
        nav_elem = None

        if root.tag.endswith("nav") or root.tag == f"{{{XHTML_NS}}}nav":
            # nav is the root element (standalone nav.xhtml)
            nav_elem = root
        else:
            # nav is a descendant (html>body>nav structure)
            nav_elem = _find_nav_element(root)

        if nav_elem is not None:
            return _has_nav_structure(nav_elem)
    except ET.ParseError:
        pass

    return False


def _find_nav_element(root: ET.Element) -> ET.Element | None:
    """Find the first <nav> element in the tree."""
    # Check root's direct children first
    for child in root:
        if _is_nav_tag(child):
            return child
    # Recurse into children
    for child in root:
        result = _find_nav_element(child)
        if result is not None:
            return result
    return None


def _is_nav_tag(elem: ET.Element) -> bool:
    """Check if element is a nav element (with or without namespace)."""
    return (
        elem.tag.endswith("nav")
        or elem.tag == f"{{{XHTML_NS}}}nav"
        or elem.tag == f"{{{XHTML_NS}}}html:nav"
    )


def _has_nav_structure(nav_elem: ET.Element) -> bool:
    """Check if nav element contains the required ol>li structure."""
    # Find ol element
    ol_elem = _find_child_by_local_name(nav_elem, "ol")
    if ol_elem is None:
        return False

    # Find li element inside ol
    li_elem = _find_child_by_local_name(ol_elem, "li")
    return li_elem is not None


def _find_child_by_local_name(parent: ET.Element, local_name: str) -> ET.Element | None:
    """Find a direct child element by its local name (ignoring namespace)."""
    for child in parent:
        if child.tag.endswith(f":{local_name}") or child.tag == local_name:
            return child
        # Also check for namespace-prefixed tags
        if child.tag == f"{{{XHTML_NS}}}{local_name}":
            return child
    return None


def extract_nav_points(html: str) -> List[Dict]:
    """
    Extract all navigable text points from a nav file.

    Supports both NCX and XHTML nav formats.

    Args:
        html: Raw HTML/XML content string

    Returns:
        List of dicts with keys:
        - text: the navigable text content
        - index: sequential index of this text node
        - tag: 'text' for NCX <text> elements, 'a' for XHTML <a> elements
    """
    stripped = html.strip()
    if not stripped:
        return []

    try:
        root = ET.fromstring(stripped)

        # NCX format
        if root.tag.endswith("ncx") or root.tag == f"{{{NCX_NS}}}ncx":
            return _extract_ncx_nav_points(root)

        # XHTML nav format
        nav_elem = None
        if root.tag.endswith("nav") or root.tag == f"{{{XHTML_NS}}}nav":
            nav_elem = root
        else:
            nav_elem = _find_nav_element(root)

        if nav_elem is not None:
            return _extract_xhtml_nav_points(nav_elem)

    except ET.ParseError:
        pass

    return []


def _extract_ncx_nav_points(root: ET.Element) -> List[Dict]:
    """Extract text from NCX <navMap>/<navPoint>/<navLabel>/<text> elements."""
    results: List[Dict] = []
    nav_map = _find_ncx_element(root, "navMap")

    if nav_map is None:
        return []

    # Collect all <text> elements in document order
    _collect_ncx_text_nodes(nav_map, results)
    return results


def _find_ncx_element(parent: ET.Element, tag: str) -> ET.Element | None:
    """Find a child element by local name, checking both with and without NCX namespace."""
    for child in parent:
        if child.tag == tag or child.tag == f"{{{NCX_NS}}}{tag}":
            return child
    return None


def _collect_ncx_text_nodes(elem: ET.Element, results: List[Dict]) -> None:
    """Recursively collect <text> nodes in document order."""
    for child in elem:
        if child.tag == "text" or child.tag == f"{{{NCX_NS}}}text":
            text_content = (child.text or "").strip()
            if text_content:
                results.append({
                    "text": text_content,
                    "index": len(results),
                    "tag": "text",
                })
        else:
            _collect_ncx_text_nodes(child, results)


def _extract_xhtml_nav_points(nav_elem: ET.Element) -> List[Dict]:
    """Extract text from XHTML nav <ol><li><a> elements."""
    results: List[Dict] = []
    ol_elem = _find_child_by_local_name(nav_elem, "ol")

    if ol_elem is not None:
        _collect_xhtml_nav_text_nodes(ol_elem, results)

    return results


def _collect_xhtml_nav_text_nodes(parent: ET.Element, results: List[Dict]) -> None:
    """
    Recursively collect <a> text nodes from nav list structure.

    Processes children in document order, collecting <a> text and
    recursing into nested <ol> lists.
    """
    for child in parent:
        tag_lower = child.tag.lower() if child.tag else ""

        if tag_lower == "a" or child.tag == f"{{{XHTML_NS}}}a":
            text_content = (child.text or "").strip()
            if text_content:
                results.append({
                    "text": text_content,
                    "index": len(results),
                    "tag": "a",
                })

        # Recurse into nested <ol> for nested navigation
        if tag_lower == "ol" or child.tag == f"{{{XHTML_NS}}}ol":
            _collect_xhtml_nav_text_nodes(child, results)

        # Recurse into <li> to handle nested structure
        if tag_lower == "li" or child.tag == f"{{{XHTML_NS}}}li":
            _collect_xhtml_nav_text_nodes(child, results)


def preserve_content_attrs(html: str) -> Dict[int, str]:
    """
    Preserve all navigable text content nodes from a nav file.

    Returns a mapping of text node index to original text content.
    This is the inverse operation used before calling rebuild_nav().

    Args:
        html: Raw HTML/XML content string

    Returns:
        Dict mapping text node index (int) to original text (str)
    """
    points = extract_nav_points(html)
    return {p["index"]: p["text"] for p in points}


def rebuild_nav(html: str, translations: Dict[int, str]) -> str:
    """
    Rebuild nav file HTML with translated text content.

    Replaces original text nodes with their translations while
    preserving all HTML/XML structure and attributes.

    Args:
        html: Original HTML/XML content string
        translations: Dict mapping text node index to translated text

    Returns:
        HTML with translated content, preserving original structure
    """
    stripped = html.strip()
    if not stripped:
        return html

    try:
        root = ET.fromstring(stripped)

        if root.tag.endswith("ncx") or root.tag == f"{{{NCX_NS}}}ncx":
            return _rebuild_ncx_nav(root, translations, stripped)
        else:
            nav_elem = None
            if root.tag.endswith("nav") or root.tag == f"{{{XHTML_NS}}}nav":
                nav_elem = root
            else:
                nav_elem = _find_nav_element(root)

            if nav_elem is not None:
                return _rebuild_xhtml_nav(root, nav_elem, translations)

    except ET.ParseError:
        pass

    # Fallback: return original if parsing fails
    return html


def _rebuild_ncx_nav(root: ET.Element, translations: Dict[int, str], original: str) -> str:
    """Rebuild NCX nav file with translations applied to <text> elements."""
    nav_map = _find_ncx_element(root, "navMap")
    if nav_map is None:
        return ET.tostring(root, encoding="unicode")

    current_index = 0

    def replace_text_nodes(elem: ET.Element) -> None:
        nonlocal current_index
        for child in list(elem):
            if child.tag == "text" or child.tag == f"{{{NCX_NS}}}text":
                if current_index in translations:
                    child.text = translations[current_index]
                current_index += 1
            else:
                replace_text_nodes(child)

    replace_text_nodes(nav_map)

    # Serialize with XML declaration to preserve NCX format
    xml_str = ET.tostring(root, encoding="unicode")
    # Strip namespace prefixes that ET adds (ns0:, ns1:, ns2:, etc.)
    xml_str = re.sub(r'\bns\d+:', '', xml_str)
    # Fix self-closing tags: " />" -> "/>"
    xml_str = re.sub(r'\s+/>', '/>', xml_str)
    # Fix namespace declaration: xmlns:ns0="..." -> xmlns="..."
    xml_str = re.sub(r'xmlns:ns\d+="([^"]+)"', r'xmlns="\1"', xml_str)
    # Add XML declaration if original had it
    if original.startswith("<?xml"):
        # Extract declaration from original
        decl_match = re.match(r'<\?xml[^?]+\?>', original)
        if decl_match:
            xml_str = decl_match.group() + "\n" + xml_str
    return xml_str


def _rebuild_xhtml_nav(root: ET.Element, nav_elem: ET.Element, translations: Dict[int, str]) -> str:
    """Rebuild XHTML nav file with translations applied to <a> elements."""
    current_index = 0

    def replace_a_text_nodes(parent: ET.Element) -> None:
        nonlocal current_index
        for child in list(parent):
            tag_lower = child.tag.lower() if child.tag else ""

            if tag_lower == "a" or child.tag == f"{{{XHTML_NS}}}a":
                if current_index in translations:
                    child.text = translations[current_index]
                current_index += 1

            if tag_lower == "ol" or child.tag == f"{{{XHTML_NS}}}ol":
                replace_a_text_nodes(child)

            if tag_lower == "li" or child.tag == f"{{{XHTML_NS}}}li":
                replace_a_text_nodes(child)

    replace_a_text_nodes(nav_elem)
    xml_str = ET.tostring(root, encoding="unicode")
    # Strip namespace prefixes that ET adds (ns0:, ns1:, ns2:, etc.)
    xml_str = re.sub(r'\bns\d+:', '', xml_str)
    # Also strip the html: prefix used by ET for XHTML namespace
    xml_str = re.sub(r'\bhtml:', '', xml_str)
    return xml_str


class NavParser:
    """
    EPUB Navigation File Parser.

    Provides methods for detecting, extracting, and rebuilding
    EPUB navigation files (toc.ncx and nav.xhtml).
    """

    def is_nav_file(self, html: str) -> bool:
        """Check if content is an EPUB nav file."""
        return is_nav_file(html)

    def extract_nav_points(self, html: str) -> List[Dict]:
        """Extract navigable text points from nav file."""
        return extract_nav_points(html)

    def preserve_content_attrs(self, html: str) -> Dict[int, str]:
        """Preserve navigable text content nodes."""
        return preserve_content_attrs(html)

    def rebuild_nav(self, html: str, translations: Dict[int, str]) -> str:
        """Rebuild nav file with translated content."""
        return rebuild_nav(html, translations)
