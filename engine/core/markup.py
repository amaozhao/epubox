import html.entities
import re
from functools import lru_cache
from xml.parsers import expat

from lxml import etree  # type: ignore[attr-defined]

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


class UnsafeMarkupError(ValueError):
    """Raised when EPUB markup cannot be parsed without external I/O."""


_TRUSTED_XHTML_DTDS = {
    "xhtml1-strict.dtd",
    "xhtml1-transitional.dtd",
    "xhtml1-frameset.dtd",
    "xhtml11.dtd",
}


@lru_cache(maxsize=1)
def _xhtml_entities_dtd() -> str:
    declarations = []
    for name, value in sorted(html.entities.html5.items()):
        name = name.removesuffix(";")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]+", name):
            continue
        codepoints = "".join(f"&#{ord(char)};" for char in value)
        declarations.append(f'<!ENTITY {name} "{codepoints}">')
    return "\n".join(declarations)


class _EpubResolver(etree.Resolver):
    def resolve(self, url: str, public_id: str | None, context):
        filename = (url or "").rsplit("/", 1)[-1].lower()
        if filename in _TRUSTED_XHTML_DTDS:
            return self.resolve_string(_xhtml_entities_dtd(), context)
        raise OSError(f"external entity is not allowed: {url or public_id}")


class _RootSeen(Exception):
    pass


def _validate_doctype(data: bytes | str) -> None:
    """Reject custom DTD declarations before lxml can expand them."""

    parser = expat.ParserCreate()
    if isinstance(data, str):
        source = data
    else:
        try:
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                source = data.decode("utf-16")
            elif data.startswith(b"\xef\xbb\xbf"):
                source = data.decode("utf-8-sig")
            elif data.startswith(b"<\x00"):
                source = data.decode("utf-16-le")
            elif data.startswith(b"\x00<"):
                source = data.decode("utf-16-be")
            else:
                source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsafeMarkupError(str(exc)) from exc

    def doctype(_name: str, system_id: str | None, _public_id: str | None, internal: bool) -> None:
        if internal:
            raise UnsafeMarkupError("internal DTD subsets are not allowed")
        if system_id and system_id.rsplit("/", 1)[-1].lower() not in _TRUSTED_XHTML_DTDS:
            raise UnsafeMarkupError(f"external entity is not allowed: {system_id}")

    def root(_name: str, _attributes: dict[str, str]) -> None:
        raise _RootSeen

    parser.StartDoctypeDeclHandler = doctype
    parser.StartElementHandler = root
    try:
        parser.Parse(source, True)
    except _RootSeen:
        return
    except expat.ExpatError as exc:
        raise UnsafeMarkupError(str(exc)) from exc


def _xml_parser(*, encoding: str | None = None) -> etree.XMLParser:
    parser = etree.XMLParser(
        encoding=encoding,
        load_dtd=True,
        no_network=True,
        resolve_entities=True,
        remove_blank_text=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
        recover=False,
        huge_tree=False,
    )
    parser.resolvers.add(_EpubResolver())
    return parser


def parse_xml_bytes(data: bytes) -> etree._ElementTree:
    """Strictly parse XML bytes while honoring their declaration or BOM."""

    _validate_doctype(data)
    try:
        root = etree.fromstring(data, _xml_parser())
    except (OSError, etree.XMLSyntaxError) as exc:
        raise UnsafeMarkupError(str(exc)) from exc
    return root.getroottree()


def parse_xml_safely(markup: str) -> etree._ElementTree:
    """Parse EPUB XML without network or arbitrary local-file entity access."""

    _validate_doctype(markup)
    try:
        root = etree.fromstring(markup.encode("utf-8"), _xml_parser(encoding="utf-8"))
    except (OSError, etree.XMLSyntaxError) as exc:
        raise UnsafeMarkupError(str(exc)) from exc
    return root.getroottree()


def serialize_xml(tree: etree._ElementTree, *, source_markup: str = "") -> str:
    """Serialize a parsed document while retaining its declaration and doctype."""

    has_declaration = source_markup.lstrip("\ufeff").startswith("<?xml")
    return etree.tostring(
        tree,
        encoding="utf-8",
        xml_declaration=has_declaration,
        pretty_print=False,
    ).decode("utf-8")


def qname_local_name(value: str) -> str:
    return etree.QName(value).localname.lower()


def element_path(element: etree._Element) -> tuple[int, ...]:
    """Return a stable path using element children only (comments/PIs do not count)."""

    path: list[int] = []
    current = element
    while current.getparent() is not None:
        parent = current.getparent()
        siblings = [child for child in parent if isinstance(child.tag, str)]
        path.append(siblings.index(current))
        current = parent
    return tuple(reversed(path))


def find_by_element_path(tree: etree._ElementTree, path: tuple[int, ...] | list[int]) -> etree._Element:
    current = tree.getroot()
    for index in path:
        children = [child for child in current if isinstance(child.tag, str)]
        try:
            current = children[index]
        except IndexError as exc:
            raise KeyError(tuple(path)) from exc
    return current
