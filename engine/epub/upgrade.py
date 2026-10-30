from __future__ import annotations

import html.entities
import posixpath
import re
import zipfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

from engine.core.markup import element_path, parse_xml_bytes, qname_local_name
from engine.epub.metadata import (
    recover_empty_agents,
    remove_dangling_refinements,
    required_dc,
    sync_ncx_uid,
    unique_identifier,
)
from engine.epub.parsing import parse_resource
from engine.epub.ranges import ResourceIndex, index_resource
from engine.epub.validation import PackageInventory

_OPF = "http://www.idpf.org/2007/opf"
_DC = "http://purl.org/dc/elements/1.1/"
_XHTML = "http://www.w3.org/1999/xhtml"
_EPUB = "http://www.idpf.org/2007/ops"
_NCX = "http://www.daisy.org/z3986/2005/ncx/"
_XML = "http://www.w3.org/XML/1998/namespace"
_SVG = "http://www.w3.org/2000/svg"
_MATHML = "http://www.w3.org/1998/Math/MathML"
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_OPF_ELEMENTS = {
    "package",
    "metadata",
    "manifest",
    "item",
    "spine",
    "itemref",
    "guide",
    "reference",
    "bindings",
    "mediaType",
    "collection",
    "link",
    "meta",
}
_GUIDE_TYPES = {
    "acknowledgements": "acknowledgments",
    "bibliography": "bibliography",
    "colophon": "colophon",
    "copyright-page": "copyright-page",
    "cover": "cover",
    "dedication": "dedication",
    "epigraph": "epigraph",
    "foreword": "foreword",
    "glossary": "glossary",
    "index": "index",
    "loi": "loi",
    "lot": "lot",
    "preface": "preface",
    "text": "bodymatter",
    "title-page": "titlepage",
    "toc": "toc",
}


def upgrade_package(
    source: Path,
    inventory: PackageInventory,
    replacements: Mapping[str, bytes],
) -> dict[str, bytes]:
    """Return translated replacements plus the minimum deterministic EPUB 3.0 upgrade."""

    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())

        def read(path: str) -> bytes:
            if path in replacements:
                return replacements[path]
            return archive.read(path)

        result = {path: data for path, data in replacements.items() if path not in names or archive.read(path) != data}
        for path in inventory.documents:
            current = read(path)
            normalized = _normalize_xhtml(current)
            if normalized != current:
                result[path] = normalized

        raw_opf = read(inventory.opf_path)
        tree = parse_xml_bytes(raw_opf)
        package = tree.getroot()
        before = etree.tostring(tree, encoding="utf-8", pretty_print=False)
        namespaces_changed = _normalize_opf_namespaces(package)
        package.set("version", "3.0")
        metadata = _child(package, "metadata")
        _flatten_metadata(metadata)
        manifest = _child(package, "manifest")
        _metadata(package, metadata, source, inventory, archive)
        if inventory.ncx_path and inventory.ncx_path in names | set(replacements):
            current_ncx = read(inventory.ncx_path)
            synced_ncx = sync_ncx_uid(current_ncx, unique_identifier(package, metadata))
            if synced_ncx != current_ncx:
                result[inventory.ncx_path] = synced_ncx

        items = {item.get("id", ""): item for item in manifest if qname_local_name(item.tag) == "item"}
        cover_id = _cover(metadata, package)
        remove_dangling_refinements(package, metadata)
        _order_metadata(metadata)
        for item in items.values():
            path = _resolve(inventory.opf_path, item.get("href", ""))
            media_type = item.get("media-type", "")
            if media_type == "text/html":
                item.set("media-type", "application/xhtml+xml")
            for attribute in list(item.attrib):
                if qname_local_name(attribute) in {"fallback-style", "required-modules", "required-namespace"}:
                    item.attrib.pop(attribute)
            if path in names or path in replacements:
                actual = _content_properties(read(path), item.get("media-type", ""))
                if item.get("media-type") == "application/xhtml+xml":
                    # Converters may replace inline formulas/figures with images
                    # without removing their old foreign-content declarations.
                    properties = [
                        value
                        for value in item.get("properties", "").split()
                        if value not in {"mathml", "svg"} or value in actual
                    ]
                    if properties:
                        item.set("properties", " ".join(properties))
                    else:
                        item.attrib.pop("properties", None)
                for value in actual:
                    _add_property(item, value)
        cover_item = _cover_item(package, items, inventory.opf_path, cover_id, read)
        if cover_item is not None:
            _add_property(cover_item, "cover-image")

        nav_path = inventory.nav_path or next(
            (
                path
                for item in items.values()
                if item.get("media-type") in {"application/xhtml+xml", "text/html"}
                and (path := _resolve(inventory.opf_path, item.get("href", ""))) in names | set(replacements)
                and _has_toc(read(path))
            ),
            None,
        )
        if nav_path is None:
            nav_path = _available_nav(inventory.opf_path, names | set(replacements))
            nav_data = _navigation(package, manifest, inventory, nav_path, _language(metadata), read)
            result[nav_path] = nav_data
            nav_id = _available_id(set(items), "nav")
            item = etree.SubElement(manifest, f"{{{_OPF}}}item")
            item.set("id", nav_id)
            item.set("href", _relative(inventory.opf_path, nav_path))
            item.set("media-type", "application/xhtml+xml")
            item.set("properties", "nav")
        else:
            for item in items.values():
                if _resolve(inventory.opf_path, item.get("href", "")) == nav_path:
                    item.set("media-type", "application/xhtml+xml")
                    _add_property(item, "nav")
                    break

        # Navigation may expose auxiliary pages omitted from an EPUB 2 spine.
        # Register them without changing the original default reading order.
        nav_data = result[nav_path] if nav_path in result else read(nav_path)
        cover_path = _resolve(inventory.opf_path, cover_item.get("href", "")) if cover_item is not None else None
        for path in _auxiliary_covers(nav_data, nav_path, inventory):
            current = result[path] if path in result else read(path)
            repaired_cover = _repair_cover_resources(current, path, inventory, cover_path, names | set(result))
            if repaired_cover != current:
                result[path] = repaired_cover
        repaired_nav = _repair_cover_navigation(nav_data, nav_path, package, inventory)
        if repaired_nav != nav_data:
            result[nav_path] = repaired_nav
            nav_data = repaired_nav
        spine = _child(package, "spine")
        registered = {item.get("idref") for item in spine if qname_local_name(item.tag) == "itemref"}
        for item_id in navigation_spine_additions(inventory, nav_data, nav_path):
            if item_id not in registered:
                reference = etree.SubElement(spine, f"{{{_OPF}}}itemref")
                reference.set("idref", item_id)
                reference.set("linear", "no")
                registered.add(item_id)

        if namespaces_changed:
            etree.cleanup_namespaces(package, top_nsmap={None: _OPF, "dc": _DC})
        rendered_tree = etree.tostring(tree, encoding="utf-8", pretty_print=False)
        rendered = etree.tostring(
            tree,
            encoding="utf-8",
            xml_declaration=raw_opf.lstrip().startswith(b"<?xml"),
            pretty_print=False,
        )
        if rendered_tree != before:
            result[inventory.opf_path] = rendered
        return result


def _cover_landmarks(root: etree._Element) -> list[etree._Element]:
    return [
        link
        for link in root.iter(f"{{{_XHTML}}}a")
        if "cover" in link.get(f"{{{_EPUB}}}type", "").split()
        and any(
            parent.tag == f"{{{_XHTML}}}nav" and "landmarks" in parent.get(f"{{{_EPUB}}}type", "").split()
            for parent in link.iterancestors()
        )
    ]


def _auxiliary_covers(data: bytes, nav_path: str, inventory: PackageInventory) -> tuple[str, ...]:
    paths = {
        item.path
        for item in inventory.manifest
        if item.item_id not in inventory.spine and item.media_type in {"application/xhtml+xml", "text/html"}
    }
    targets: list[str] = []
    for link in _cover_landmarks(parse_xml_bytes(data).getroot()):
        href = link.get("href", "").strip()
        parsed = urlsplit(href)
        path = _resolve(nav_path, href) if parsed.path else nav_path
        if href and not parsed.scheme and not parsed.netloc and path in paths and path not in targets:
            targets.append(path)
    return tuple(targets)


def _repair_cover_resources(
    data: bytes, path: str, inventory: PackageInventory, cover_path: str | None, resources: set[str]
) -> bytes:
    """Repair dangling assets of a legacy cover only when the package identifies their replacements."""
    parsed = parse_resource(data, "application/xhtml+xml")
    if parsed.tree is None or parsed.diagnostics:
        return data
    images = list(parsed.tree.iter(f"{{{_XHTML}}}img"))
    changes: dict[tuple[tuple[int, ...], str], str] = {}
    for node in parsed.tree.iter(f"{{{_XHTML}}}link", f"{{{_XHTML}}}img"):
        image = node.tag == f"{{{_XHTML}}}img"
        attribute = "src" if image else "href"
        href = node.get(attribute, "").strip()
        url = urlsplit(href)
        if not href or url.scheme or url.netloc or _resolve(path, href) in resources:
            continue
        target = None
        if image and len(images) == 1 and cover_path in resources:
            target = cover_path
        elif not image and "stylesheet" in node.get("rel", "").lower().split():
            matches = [
                item.path
                for item in inventory.manifest
                if item.media_type == "text/css"
                and item.path in resources
                and PurePosixPath(item.path).name == PurePosixPath(unquote(url.path)).name
            ]
            if len(matches) == 1:
                target = matches[0]
        if target is not None:
            changes[element_path(node), attribute] = _relative(path, target)
    return _patch_navigation_attributes(data, index_resource(parsed), changes) if changes else data


def _patch_navigation_attributes(
    data: bytes, indexed: ResourceIndex, changes: Mapping[tuple[tuple[int, ...], str], str]
) -> bytes:
    patches = [
        (slot.source_span(0, len(slot.text)), changes[slot.path, slot.attribute_name])
        for slot in indexed.slots
        if slot.field == "attribute"
        and slot.attribute_name is not None
        and (slot.path, slot.attribute_name) in changes
        and slot.text
    ]
    for span, value in sorted(patches, key=lambda patch: patch[0].byte_start, reverse=True):
        replacement = html.escape(value, quote=True).encode(indexed.encoding)
        data = data[: span.byte_start] + replacement + data[span.byte_end :]
    return data


def _repair_cover_navigation(
    data: bytes, nav_path: str, package: etree._Element, inventory: PackageInventory
) -> bytes:
    """Prefer the package's spine cover over a stale auxiliary cover landmark."""
    spine_paths = {item.path for item in inventory.manifest if item.item_id in inventory.spine}
    content_paths = {
        item.path
        for item in inventory.manifest
        if item.media_type in {"application/xhtml+xml", "text/html", "image/svg+xml"}
    }
    guide = next((node for node in package if qname_local_name(node.tag) == "guide"), None)
    if guide is None:
        return data
    target = None
    for reference in guide:
        href = reference.get("href", "").strip()
        parsed = urlsplit(href)
        if (
            qname_local_name(reference.tag) == "reference"
            and reference.get("type", "").lower() == "cover"
            and href
            and not parsed.scheme
            and not parsed.netloc
            and _resolve(inventory.opf_path, href) in spine_paths & content_paths
        ):
            target = _relative(nav_path, _target(inventory.opf_path, href))
            break
    if target is None:
        return data
    parsed_nav = parse_resource(data, "application/xhtml+xml")
    if parsed_nav.tree is None or parsed_nav.diagnostics:
        return data
    changes: dict[tuple[tuple[int, ...], str], str] = {}
    for link in _cover_landmarks(parsed_nav.tree.getroot()):
        href = link.get("href", "").strip()
        parsed = urlsplit(href)
        path = _resolve(nav_path, href) if parsed.path else nav_path
        if href and not parsed.scheme and not parsed.netloc and path in content_paths - spine_paths:
            changes[element_path(link), "href"] = target
    return _patch_navigation_attributes(data, index_resource(parsed_nav), changes) if changes else data


def navigation_spine_additions(inventory: PackageInventory, nav_data: bytes, nav_path: str) -> tuple[str, ...]:
    """Return source content documents exposed by navigation but absent from its spine."""
    documents = {
        item.path: item.item_id
        for item in inventory.manifest
        if item.media_type in {"application/xhtml+xml", "text/html", "image/svg+xml"}
    }
    registered = set(inventory.spine)
    additions: list[str] = []
    root = parse_xml_bytes(nav_data).getroot()
    for link in root.iter(f"{{{_XHTML}}}a", f"{{{_XHTML}}}area"):
        href = link.get("href", "").strip()
        parsed = urlsplit(href)
        if not href or parsed.scheme or parsed.netloc:
            continue
        path = _resolve(nav_path, href) if parsed.path else nav_path
        item_id = documents.get(path)
        if item_id is not None and item_id not in registered:
            additions.append(item_id)
            registered.add(item_id)
    return tuple(additions)


def _normalize_opf_namespaces(root: etree._Element) -> bool:
    changed = False
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element)
        if name.namespace == _DC:
            normalized = f"{{{_DC}}}{name.localname.lower()}"
            if element.tag != normalized:
                element.tag = normalized
                changed = True
        elif name.localname in _OPF_ELEMENTS and name.namespace != _DC:
            normalized = f"{{{_OPF}}}{name.localname}"
            if element.tag != normalized:
                element.tag = normalized
                changed = True
    return changed


def _flatten_metadata(metadata: etree._Element) -> None:
    for wrapper in list(metadata):
        if qname_local_name(wrapper.tag) not in {"dc-metadata", "x-metadata"}:
            continue
        index = metadata.index(wrapper)
        for child in list(wrapper):
            wrapper.remove(child)
            metadata.insert(index, child)
            index += 1
        metadata.remove(wrapper)


def _normalize_xhtml(data: bytes) -> bytes:
    """Patch legacy prolog/root syntax without serializing protected content."""

    parsed = parse_resource(data, "application/xhtml+xml")
    if parsed.tree is None:
        return data
    tree = parsed.tree
    root = tree.getroot()
    indexed = index_resource(parsed)
    patches: list[tuple[int, int, bytes]] = []

    info = tree.docinfo
    if info.doctype and (info.public_id or info.system_url):
        start, end = _doctype_span(parsed.text)
        bom = len(data) - len(parsed.text.encode(indexed.encoding))
        byte_start = bom + len(parsed.text[:start].encode(indexed.encoding))
        byte_end = bom + len(parsed.text[:end].encode(indexed.encoding))
        patches.append((byte_start, byte_end, "<!DOCTYPE html>".encode(indexed.encoding)))

    attribute = f"{{{_EPUB}}}prefix"
    value = root.get(attribute)
    if value is not None:
        declarations = re.findall(r"([A-Za-z][\w.-]*):\s+(\S+)", value)
        used = {
            prefix
            for element in root.iter()
            for name, content in element.attrib.items()
            if qname_local_name(name) != "prefix"
            for prefix in re.findall(r"\b([A-Za-z][\w.-]*):[\w.-]+", content)
        }
        # XHTML epub:type uses epub:prefix, not the separate unnamespaced RDFa prefix.
        span = indexed.nodes[()].starttag
        starttag = data[span.start : span.end].decode(indexed.encoding)
        aliases = sorted((name for name, iri in root.nsmap.items() if name and iri == _EPUB), key=len, reverse=True)
        if aliases and not any(prefix in used for prefix, _ in declarations):
            names = "|".join(re.escape(name) for name in aliases)
            alias_match = re.search(rf"\s+(?:{names}):prefix\s*=\s*(['\"])(.*?)\1", starttag, re.DOTALL)
            if alias_match is not None:
                byte_start = span.start + len(starttag[: alias_match.start()].encode(indexed.encoding))
                byte_end = span.start + len(starttag[: alias_match.end()].encode(indexed.encoding))
                patches.append((byte_start, byte_end, b""))

    patches.extend(_empty_title_patches(data, indexed, root))
    patches.extend(_legacy_img_dimension_patches(data, indexed))
    for start, end, replacement in _entity_patches(data, indexed):
        if not any(start < patch_end and end > patch_start for patch_start, patch_end, _ in patches):
            patches.append((start, end, replacement))

    result = data
    for start, end, replacement in sorted(patches, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _empty_title_patches(
    data: bytes, indexed: ResourceIndex, root: etree._Element
) -> tuple[tuple[int, int, bytes], ...]:
    fallback = next(
        (
            text
            for element in root.iter()
            if isinstance(element.tag, str)
            and etree.QName(element).namespace == _XHTML
            and etree.QName(element).localname in {"h1", "h2", "h3"}
            and (text := re.sub(r"\s+", " ", "".join(element.itertext())).strip())
        ),
        "Untitled",
    )
    escaped = html.escape(fallback, quote=False).encode(indexed.encoding)
    patches: list[tuple[int, int, bytes]] = []
    for node in indexed.nodes.values():
        if node.qname != f"{{{_XHTML}}}title":
            continue
        content = data[node.content.start : node.content.end].decode(indexed.encoding)
        if content.strip():
            continue
        if not node.self_closing:
            patches.append((node.content.start, node.content.end, escaped))
            continue
        starttag = data[node.starttag.start : node.starttag.end].decode(indexed.encoding)
        match = re.match(r"<\s*([^\s/>]+)", starttag)
        if match is None:
            continue
        opening = re.sub(r"/\s*>$", ">", starttag)
        replacement = f"{opening}{html.escape(fallback, quote=False)}</{match.group(1)}>".encode(indexed.encoding)
        patches.append((node.full.start, node.full.end, replacement))
    return tuple(patches)


def _legacy_img_dimension_patches(data: bytes, indexed: ResourceIndex) -> tuple[tuple[int, int, bytes], ...]:
    patches: list[tuple[int, int, bytes]] = []
    pattern = re.compile(
        r"(?P<head>\s(?:height|width)\s*=\s*(?P<quote>['\"]))(?P<value>\d+)px(?P=quote)",
        re.IGNORECASE,
    )
    for node in indexed.nodes.values():
        if node.qname != f"{{{_XHTML}}}img":
            continue
        span = node.starttag
        starttag = data[span.start : span.end].decode(indexed.encoding)
        for match in pattern.finditer(starttag):
            byte_start = span.start + len(starttag[: match.start()].encode(indexed.encoding))
            byte_end = span.start + len(starttag[: match.end()].encode(indexed.encoding))
            replacement = f"{match.group('head')}{match.group('value')}{match.group('quote')}".encode(indexed.encoding)
            patches.append((byte_start, byte_end, replacement))
    return tuple(patches)


def _entity_patches(data: bytes, indexed: ResourceIndex) -> tuple[tuple[int, int, bytes], ...]:
    patches: list[tuple[int, int, bytes]] = []
    seen: set[tuple[int, int]] = set()
    for slot in indexed.slots:
        for character in slot.chars:
            key = (character.raw.start, character.raw.end)
            if key in seen:
                continue
            seen.add(key)
            raw = data[key[0] : key[1]].decode(indexed.encoding)
            match = re.fullmatch(r"&([A-Za-z][A-Za-z0-9]+);", raw)
            if match is None or match.group(1) in {"amp", "apos", "gt", "lt", "quot"}:
                continue
            value = html.entities.html5.get(match.group(1) + ";")
            if value is None:
                continue
            replacement = "".join(f"&#{ord(item)};" for item in value).encode(indexed.encoding)
            patches.append((key[0], key[1], replacement))
    return tuple(patches)


def _doctype_span(text: str) -> tuple[int, int]:
    start = text.lower().find("<!doctype")
    if start < 0:
        raise ValueError("parsed document type declaration is missing from source text")
    quote: str | None = None
    for index in range(start + 9, len(text)):
        character = text[index]
        if quote is not None:
            if character == quote:
                quote = None
        elif character in {'"', "'"}:
            quote = character
        elif character == ">":
            return start, index + 1
    raise ValueError("document type declaration is not closed")


def _child(parent: etree._Element, name: str) -> etree._Element:
    found = next((element for element in parent if qname_local_name(element.tag) == name), None)
    if found is not None:
        return found
    return etree.SubElement(parent, f"{{{_OPF}}}{name}")


def _metadata(
    package: etree._Element,
    metadata: etree._Element,
    source: Path,
    inventory: PackageInventory,
    archive: zipfile.ZipFile,
) -> None:
    identifiers = [item for item in metadata if qname_local_name(item.tag) == "identifier"]
    identifier = next((item for item in identifiers if (item.text or "").strip()), None)
    if identifier is None:
        identifier = etree.SubElement(metadata, f"{{{_DC}}}identifier")
        identifier.text = f"urn:sha256:{inventory.source_hash}"
    identifier_id = identifier.get("id") or _available_id(
        {item.get("id", "") for item in metadata.iter()}, "epubox-uid"
    )
    identifier.set("id", identifier_id)
    if package.get("unique-identifier") not in {item.get("id") for item in identifiers}:
        package.set("unique-identifier", identifier_id)
    required_dc(metadata, "title", source.stem)
    required_dc(metadata, "language", "und")
    recover_empty_agents(metadata)

    modified = [
        item
        for item in metadata
        if qname_local_name(item.tag) == "meta" and item.get("property") == "dcterms:modified"
    ]
    valid = next((item for item in modified if _valid_utc((item.text or "").strip())), None)
    for item in modified:
        if item is not valid:
            metadata.remove(item)
    if valid is None:
        valid = etree.SubElement(metadata, f"{{{_OPF}}}meta")
        valid.set("property", "dcterms:modified")
        valid.text = _zip_time(archive.getinfo(inventory.opf_path))

    for creator in [item for item in metadata if qname_local_name(item.tag) in {"creator", "contributor"}]:
        refinements: list[tuple[str, str, str | None]] = []
        for attribute in list(creator.attrib):
            name = qname_local_name(attribute)
            if name not in {"role", "file-as"}:
                continue
            value = creator.attrib.pop(attribute).strip()
            if value:
                refinements.append((name, value, "marc:relators" if name == "role" else None))
        if not refinements:
            continue
        creator_id = creator.get("id") or _available_id(
            {item.get("id", "") for item in metadata.iter()}, "epubox-agent"
        )
        creator.set("id", creator_id)
        for prop, value, scheme in refinements:
            meta = etree.SubElement(metadata, f"{{{_OPF}}}meta")
            meta.set("refines", f"#{creator_id}")
            meta.set("property", prop)
            if scheme:
                meta.set("scheme", scheme)
            meta.text = value

    for value in [item for item in metadata if qname_local_name(item.tag) == "identifier"]:
        schemes = [
            value.attrib.pop(attribute).strip()
            for attribute in list(value.attrib)
            if qname_local_name(attribute) == "scheme"
        ]
        if not any(schemes):
            continue
        value_id = value.get("id") or _available_id({item.get("id", "") for item in metadata.iter()}, "epubox-id")
        value.set("id", value_id)
        meta = etree.SubElement(metadata, f"{{{_OPF}}}meta")
        meta.set("refines", f"#{value_id}")
        meta.set("property", "identifier-type")
        meta.text = next(scheme for scheme in schemes if scheme)

    for value in [item for item in metadata if isinstance(item.tag, str) and etree.QName(item).namespace == _DC]:
        local_name = etree.QName(value).localname
        if local_name in {"date", "publisher"} and not len(value) and not (value.text or "").strip():
            identifier = value.get("id")
            if identifier is None or not any(node.get("refines") == f"#{identifier}" for node in package.iter()):
                metadata.remove(value)
                continue
        for attribute in list(value.attrib):
            name = etree.QName(attribute)
            if name.namespace != _OPF:
                continue
            content = value.attrib.pop(attribute).strip()
            if not content:
                continue
            value_id = value.get("id") or _available_id(
                {item.get("id", "") for item in metadata.iter()}, "epubox-meta"
            )
            value.set("id", value_id)
            meta = etree.SubElement(metadata, f"{{{_OPF}}}meta")
            meta.set("refines", f"#{value_id}")
            meta.set("property", f"epubox:{name.localname}")
            meta.text = content
            _declare_epubox(package)
        if local_name == "date":
            value.tag = f"{{{_OPF}}}meta"
            value.set("property", "dcterms:date")


def _valid_utc(value: str) -> bool:
    if not _UTC.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _cover(metadata: etree._Element, package: etree._Element) -> str | None:
    cover_id: str | None = None
    for item in list(metadata):
        if qname_local_name(item.tag) == "meta" and item.get("name", "").lower() == "cover":
            cover_id = (item.get("content") or "").strip() or cover_id
            metadata.remove(item)
        elif qname_local_name(item.tag) == "meta" and item.get("name"):
            original = item.get("name", "")
            value = item.get("content", "")
            item.attrib.clear()
            prefix, separator, _ = original.partition(":")
            iri = item.nsmap.get(prefix) if separator else None
            if iri:
                item.set("property", original)
                _declare_prefix(package, prefix, iri)
            else:
                name = re.sub(r"[^A-Za-z0-9_.-]", "-", original).strip("-") or "metadata"
                item.set("property", f"epubox:{name}")
                _declare_epubox(package)
            item.text = value
    return cover_id


def _order_metadata(metadata: etree._Element) -> None:
    children = list(metadata)
    dc = [item for item in children if isinstance(item.tag, str) and etree.QName(item).namespace == _DC]
    remaining = [item for item in children if item not in dc]
    ordered = [*dc, *remaining]
    if children == ordered:
        return
    for item in ordered:
        metadata.append(item)


def _cover_item(
    package: etree._Element,
    items: Mapping[str, etree._Element],
    opf_path: str,
    cover_id: str | None,
    read,
) -> etree._Element | None:
    candidates = [items[cover_id]] if cover_id is not None and cover_id in items else []
    guide = next((item for item in package if qname_local_name(item.tag) == "guide"), None)
    if guide is not None:
        references = [
            item
            for item in guide
            if qname_local_name(item.tag) == "reference" and item.get("type", "").lower() == "cover"
        ]
        paths = {_resolve(opf_path, item.get("href", "")) for item in references}
        candidates.extend(item for item in items.values() if _resolve(opf_path, item.get("href", "")) in paths)
    for item in candidates:
        if item.get("media-type", "").startswith("image/"):
            return item
        path = _resolve(opf_path, item.get("href", ""))
        if item.get("media-type") != "application/xhtml+xml":
            continue
        try:
            document = parse_xml_bytes(read(path)).getroot()
        except (KeyError, ValueError):
            continue
        source = next(
            (
                node.get("src")
                for node in document.iter()
                if isinstance(node.tag, str) and qname_local_name(node.tag) == "img" and node.get("src")
            ),
            None,
        )
        if source:
            image_path = _resolve(path, source)
            found = next(
                (
                    value
                    for value in items.values()
                    if value.get("media-type", "").startswith("image/")
                    and _resolve(opf_path, value.get("href", "")) == image_path
                ),
                None,
            )
            if found is not None:
                return found
    return None


def _declare_epubox(package: etree._Element) -> None:
    _declare_prefix(package, "epubox", "urn:epubox:metadata:")


def _declare_prefix(package: etree._Element, name: str, iri: str) -> None:
    prefixes = package.get("prefix", "").strip()
    if f"{name}:" not in prefixes.split():
        package.set("prefix", (prefixes + f" {name}: {iri}").strip())


def _language(metadata: etree._Element) -> str:
    return next(
        (
            (item.text or "").strip()
            for item in metadata
            if qname_local_name(item.tag) == "language" and (item.text or "").strip()
        ),
        "und",
    )


def _has_toc(data: bytes) -> bool:
    try:
        root = parse_xml_bytes(data).getroot()
    except ValueError:
        return False
    return any(
        qname_local_name(item.tag) == "nav"
        and "toc" in (item.get(f"{{{_EPUB}}}type") or item.get("epub:type") or "").split()
        for item in root.iter()
        if isinstance(item.tag, str)
    )


def _content_properties(data: bytes, media_type: str) -> tuple[str, ...]:
    if media_type != "application/xhtml+xml":
        return ()
    try:
        root = parse_xml_bytes(data).getroot()
    except ValueError:
        return ()
    properties: set[str] = set()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element)
        if name.namespace == _SVG or name.localname == "svg":
            properties.add("svg")
        if name.namespace == _MATHML or name.localname == "math":
            properties.add("mathml")
        if name.namespace == _EPUB and name.localname == "switch":
            properties.add("switch")
        if name.localname in {
            "button",
            "fieldset",
            "form",
            "input",
            "object",
            "output",
            "script",
            "select",
            "textarea",
        } or any(qname_local_name(attribute).startswith("on") for attribute in element.attrib):
            properties.add("scripted")
        if _remote_reference(element):
            properties.add("remote-resources")
        if name.localname in {"style", "script"} and re.search(r"(?:https?:)?//", element.text or ""):
            properties.add("remote-resources")
    return tuple(sorted(properties))


def _remote(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https"} or bool(parsed.netloc)


def _remote_reference(element: etree._Element) -> bool:
    name = qname_local_name(element.tag)
    attribute = {
        "audio": "src",
        "embed": "src",
        "iframe": "src",
        "img": "src",
        "input": "src",
        "object": "data",
        "script": "src",
        "source": "src",
        "track": "src",
        "video": "src",
    }.get(name)
    if attribute and _remote(element.get(attribute, "")):
        return True
    return name == "link" and "stylesheet" in element.get("rel", "").split() and _remote(element.get("href", ""))


def _add_property(item: etree._Element, value: str) -> None:
    values = item.get("properties", "").split()
    if value not in values:
        item.set("properties", " ".join((*values, value)))


def _available_nav(opf_path: str, names: set[str]) -> str:
    directory = posixpath.dirname(opf_path)
    for number in range(10_000):
        name = "nav.xhtml" if number == 0 else f"nav-{number}.xhtml"
        path = posixpath.join(directory, name) if directory else name
        if path not in names:
            return path
    raise ValueError("could not allocate EPUB navigation path")


def _available_id(values: set[str | None], preferred: str) -> str:
    if preferred not in values:
        return preferred
    for number in range(1, 10_000):
        candidate = f"{preferred}-{number}"
        if candidate not in values:
            return candidate
    raise ValueError("could not allocate EPUB package id")


def _navigation(
    package: etree._Element,
    manifest: etree._Element,
    inventory: PackageInventory,
    nav_path: str,
    language: str,
    read,
) -> bytes:
    chinese = language.lower().startswith("zh")
    contents, pages, locations = ("目录", "页码", "定位") if chinese else ("Contents", "Pages", "Landmarks")
    html = etree.Element(f"{{{_XHTML}}}html", nsmap={None: _XHTML, "epub": _EPUB})
    html.set("lang", language)
    html.set(f"{{{_XML}}}lang", language)
    head = etree.SubElement(html, f"{{{_XHTML}}}head")
    etree.SubElement(head, f"{{{_XHTML}}}title").text = contents
    body = etree.SubElement(html, f"{{{_XHTML}}}body")
    toc = etree.SubElement(body, f"{{{_XHTML}}}nav")
    toc.set(f"{{{_EPUB}}}type", "toc")
    toc.set("id", "toc")
    etree.SubElement(toc, f"{{{_XHTML}}}h1").text = contents
    ordered = etree.SubElement(toc, f"{{{_XHTML}}}ol")
    toc_entries = 0
    if inventory.ncx_path:
        ncx = parse_xml_bytes(read(inventory.ncx_path)).getroot()
        nav_map = next((item for item in ncx.iter() if qname_local_name(item.tag) == "navmap"), None)
        if nav_map is not None:
            for point in nav_map:
                if qname_local_name(point.tag) == "navpoint":
                    toc_entries += _navpoint(ordered, point, inventory.ncx_path, nav_path)
        page_list = next((item for item in ncx.iter() if qname_local_name(item.tag) == "pagelist"), None)
        if page_list is not None:
            page_nav = etree.SubElement(body, f"{{{_XHTML}}}nav")
            page_nav.set(f"{{{_EPUB}}}type", "page-list")
            etree.SubElement(page_nav, f"{{{_XHTML}}}h2").text = pages
            page_ol = etree.SubElement(page_nav, f"{{{_XHTML}}}ol")
            for target in page_list:
                if qname_local_name(target.tag) in {"pagetarget", "navtarget"}:
                    _navpoint(page_ol, target, inventory.ncx_path, nav_path)
    if not toc_entries:
        _spine_toc(ordered, manifest, inventory, nav_path, read)

    guide = next((item for item in package if qname_local_name(item.tag) == "guide"), None)
    if guide is not None and len(guide):
        references = [
            (reference, semantic)
            for reference in guide
            if qname_local_name(reference.tag) == "reference" and reference.get("href")
            if (semantic := _GUIDE_TYPES.get(reference.get("type", "").lower())) is not None
        ]
        if references:
            landmarks = etree.SubElement(body, f"{{{_XHTML}}}nav")
            landmarks.set(f"{{{_EPUB}}}type", "landmarks")
            etree.SubElement(landmarks, f"{{{_XHTML}}}h2").text = locations
            listing = etree.SubElement(landmarks, f"{{{_XHTML}}}ol")
            for reference, semantic in references:
                href = _target(inventory.opf_path, reference.get("href", ""))
                link = _link(
                    listing,
                    reference.get("title") or reference.get("type") or "Landmark",
                    _relative(nav_path, href),
                )
                link.set(f"{{{_EPUB}}}type", semantic)
    return etree.tostring(html.getroottree(), encoding="utf-8", xml_declaration=True, pretty_print=False)


def _spine_toc(
    ordered: etree._Element,
    manifest: etree._Element,
    inventory: PackageInventory,
    nav_path: str,
    read,
) -> None:
    by_id = {item.get("id", ""): item for item in manifest if qname_local_name(item.tag) == "item"}
    for item_id in inventory.spine:
        item = by_id.get(item_id)
        if item is None:
            continue
        path = _resolve(inventory.opf_path, item.get("href", ""))
        document = parse_xml_bytes(read(path)).getroot()
        heading = next(
            (
                node
                for node in document.iter()
                if qname_local_name(node.tag) == "h1" and "".join(node.itertext()).strip()
            ),
            None,
        )
        title = next(
            (
                node
                for node in document.iter()
                if qname_local_name(node.tag) == "title" and "".join(node.itertext()).strip()
            ),
            None,
        )
        label_node = heading if heading is not None else title
        label = "".join(label_node.itertext()).strip() if label_node is not None else PurePosixPath(path).stem
        href = _relative(nav_path, path) + (
            f"#{heading.get('id')}" if heading is not None and heading.get("id") else ""
        )
        _link(ordered, label, href)


def _navpoint(parent: etree._Element, point: etree._Element, source_path: str, nav_path: str) -> int:
    label_node = next((item for item in point.iter() if qname_local_name(item.tag) == "text"), None)
    content = next((item for item in point if qname_local_name(item.tag) == "content"), None)
    if content is None or not content.get("src"):
        return 0
    target = _target(source_path, content.get("src", ""))
    item = etree.SubElement(parent, f"{{{_XHTML}}}li")
    link = etree.SubElement(item, f"{{{_XHTML}}}a")
    link.set("href", _relative(nav_path, target))
    link.text = (
        "".join(label_node.itertext()).strip()
        if label_node is not None
        else PurePosixPath(target.partition("#")[0]).stem
    )
    children = [child for child in point if qname_local_name(child.tag) == "navpoint"]
    if children:
        nested = etree.SubElement(item, f"{{{_XHTML}}}ol")
        for child in children:
            _navpoint(nested, child, source_path, nav_path)
    return 1


def _link(parent: etree._Element, label: str, href: str) -> etree._Element:
    item = etree.SubElement(parent, f"{{{_XHTML}}}li")
    link = etree.SubElement(item, f"{{{_XHTML}}}a")
    link.set("href", href)
    link.text = label
    return link


def _resolve(base: str, href: str) -> str:
    path = unquote(urlsplit(href).path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path))


def _target(base: str, href: str) -> str:
    parsed = urlsplit(href)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parsed.path)))
    return path + ("#" + unquote(parsed.fragment) if parsed.fragment else "")


def _relative(base: str, target: str) -> str:
    path, marker, fragment = target.partition("#")
    relative = quote(posixpath.relpath(path, posixpath.dirname(base) or "."), safe="/!$&'()*+,;=:@")
    return relative + (marker + quote(fragment, safe="!$&'()*+,;=:@/?") if marker else "")


def _zip_time(info: zipfile.ZipInfo) -> str:
    year, month, day, hour, minute, second = info.date_time
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


__all__ = ["navigation_spine_additions", "upgrade_package"]
