"""Small original EPUB fixtures; these are synthetic, not reader acceptance evidence."""

from html import escape
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile


def make_epub(path: Path, documents: dict[str, str] | None = None, *, version: str = "3.0") -> Path:
    documents = documents or {
        "chapter1.xhtml": '<h1 id="title">Reliable systems</h1><p>Keep the original data.</p>'
        "<p>Use <code>finally</code> to release the <em>resource</em>.</p>",
        "chapter2.xhtml": '<h1 id="title">Recovery</h1><p>Record failures and continue independent work.</p>',
    }
    names = list(documents)
    links = "".join(f'<li><a href="{escape(name)}">Chapter {i + 1}</a></li>' for i, name in enumerate(names))
    nav = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">'
    nav += '<head><title>Contents</title></head><body><nav epub:type="toc" id="toc"><h1>Contents</h1>'
    nav += f"<ol>{links}</ol></nav></body></html>"
    navpoints = "".join(
        f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>Chapter {i + 1}</text></navLabel>'
        f'<content src="{escape(name)}"/></navPoint>'
        for i, name in enumerate(names)
    )
    ncx = '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
    ncx += '<head><meta name="dtb:uid" content="urn:uuid:11111111-1111-4111-8111-111111111111"/>'
    ncx += '<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/>'
    ncx += '<meta name="dtb:maxPageNumber" content="0"/></head><docTitle><text>Reliable systems</text></docTitle>'
    ncx += f"<navMap>{navpoints}</navMap></ncx>"
    manifest = "".join(
        f'<item id="c{i}" href="{escape(name)}" media-type="application/xhtml+xml"/>' for i, name in enumerate(names)
    )
    manifest += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    if version.startswith("3"):
        manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(names)))
    modified = '<meta property="dcterms:modified">2026-09-28T00:00:00Z</meta>' if version.startswith("3") else ""
    opf = f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="uid">'
    opf += '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">'
    opf += "urn:uuid:11111111-1111-4111-8111-111111111111</dc:identifier><dc:title>Reliable systems</dc:title>"
    opf += f"<dc:language>en</dc:language>{modified}</metadata><manifest>{manifest}</manifest>"
    opf += f'<spine toc="ncx">{spine}</spine></package>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/toc.ncx", ncx)
        if version.startswith("3"):
            archive.writestr("OEBPS/nav.xhtml", nav)
        for name, body in documents.items():
            markup = (
                body
                if body.lstrip().startswith(("<?xml", "<html"))
                else (
                    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
                    'xml:lang="en"><head><title>Chapter</title></head><body>' + body + "</body></html>"
                )
            )
            archive.writestr("OEBPS/" + name, markup)
    return path
