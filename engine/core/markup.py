import re


XMLISH_ROOT_RE = re.compile(r"^\s*(?:<\?xml\b[^>]*>\s*)?<([A-Za-z_][\w:.-]*)")


def prefers_xml_parser(markup: str) -> bool:
    """Return True when BeautifulSoup should parse this markup as XML."""
    match = XMLISH_ROOT_RE.search(markup or "")
    if not match:
        return False

    root = match.group(1).lower()
    if root in {"html"}:
        return False

    if root in {"ncx", "package", "container"}:
        return True

    normalized = (markup or "").lower()
    return "<navmap" in normalized or "<navpoint" in normalized


def get_markup_parser(markup: str) -> str:
    return "xml" if prefers_xml_parser(markup) else "html.parser"
