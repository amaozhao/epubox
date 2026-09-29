import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.epub.checker import checker_for_source
from engine.epub.validation import EpubCheckResult


class StubChecker:
    def __init__(self, result: EpubCheckResult) -> None:
        self.result = result
        self.command = result.command
        self.checked: list[Path] = []

    def check(self, source: Path) -> EpubCheckResult:
        self.checked.append(source)
        return self.result


def make_epub(
    path: Path,
    *,
    nav: str | None = None,
    nav_href: str = "navigation.xhtml",
    opf_path: str = "OEBPS/package.opf",
    nav_compression: int = zipfile.ZIP_STORED,
) -> None:
    container = f"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="{opf_path}"/></rootfiles>
</container>"""
    package = f"""<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <manifest><item id="nav" href="{nav_href}" media-type="application/xhtml+xml" properties="nav"/></manifest>
</package>"""
    nav = (
        nav
        if nav is not None
        else '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n<nav aria-labelledby="toc">\n</nav></body></html>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/package.opf", package)
        archive.writestr("OEBPS/navigation.xhtml", nav, compress_type=nav_compression)


def nav_error(extra: str = "") -> str:
    return (
        "ERROR(RSC-005): /tmp/book.epub/OEBPS/navigation.xhtml(2,28): "
        'Error while parsing file: attribute "aria-labelledby" not allowed here' + extra
    )


def test_default_checker_is_retained_when_it_passes(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"unused")
    primary = StubChecker(EpubCheckResult(("/tools/epubcheck-5.4.0/epubcheck.jar",), 0))
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source) is primary
    assert primary.checked == [source]
    fallback.assert_not_called()


def test_known_54_navigation_error_uses_passing_53(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    make_epub(source)
    primary = StubChecker(EpubCheckResult(("/tools/epubcheck-5.4.0/epubcheck.jar",), 1, errors=(nav_error(),)))
    fallback = StubChecker(EpubCheckResult(("5.3",), 0))
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker", return_value=fallback),
    ):
        assert checker_for_source(source) is fallback
    assert primary.checked == [source]
    assert fallback.checked == [source]


def test_mixed_navigation_aria_label_errors_use_passing_53(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    nav = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n'
        '<nav aria-label="contents"/>\n'
        '<nav aria-labelledby="landmarks"/>\n'
        '<nav aria-label="pages"/></body></html>'
    )
    make_epub(source, nav=nav)
    errors = tuple(
        f"ERROR(RSC-005): /tmp/book.epub/OEBPS/navigation.xhtml({line},28): "
        f'Error while parsing file: attribute "{attribute}" not allowed here'
        for line, attribute in ((2, "aria-label"), (3, "aria-labelledby"), (4, "aria-label"))
    )
    primary = StubChecker(EpubCheckResult(("/tools/epubcheck-5.4.0/epubcheck.jar",), 1, errors=errors))
    fallback = StubChecker(EpubCheckResult(("5.3",), 0))
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker", return_value=fallback),
    ):
        assert checker_for_source(source) is fallback
    assert primary.checked == [source]
    assert fallback.checked == [source]


def test_non_navigation_aria_error_does_not_use_fallback(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    make_epub(
        source,
        nav='<html xmlns="http://www.w3.org/1999/xhtml"><body>\n<p aria-label="ordinary">Text</p></body></html>',
    )
    error = (
        "ERROR(RSC-005): /tmp/book.epub/OEBPS/navigation.xhtml(2,28): "
        'Error while parsing file: attribute "aria-label" not allowed here'
    )
    primary = StubChecker(EpubCheckResult(("/tools/epubcheck-5.4.0/epubcheck.jar",), 1, errors=(error,)))
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source) is primary
    fallback.assert_not_called()


def test_mixed_errors_do_not_fall_back(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    make_epub(source)
    primary = StubChecker(
        EpubCheckResult(
            ("/tools/epubcheck-5.4.0/epubcheck.jar",),
            1,
            errors=(nav_error(), "ERROR(OPF-001): broken package"),
        )
    )
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source) is primary
    fallback.assert_not_called()


@pytest.mark.parametrize("case", ["oversized", "high_ratio", "path_escape", "encoded_escape", "bad_xml"])
def test_unsafe_or_malformed_source_never_uses_fallback(tmp_path: Path, case: str) -> None:
    source = tmp_path / "book.epub"
    nav = '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n<nav aria-labelledby="toc">\n</nav></body></html>'
    if case == "oversized":
        make_epub(source, nav=nav + " " * (8 * 1024 * 1024))
    elif case == "high_ratio":
        make_epub(source, nav=nav + " " * 10_000_000, nav_compression=zipfile.ZIP_DEFLATED)
    elif case == "path_escape":
        make_epub(source, opf_path="../OEBPS/package.opf")
    elif case == "encoded_escape":
        make_epub(source, nav_href="%2e%2e/%2e%2e/outside.xhtml")
    else:
        make_epub(source, nav='<html>\n<nav aria-labelledby="toc">')
    primary = StubChecker(EpubCheckResult(("/tools/epubcheck-5.4.0/epubcheck.jar",), 1, errors=(nav_error(),)))
    with (
        patch("engine.epub.checker.checker_command", return_value=primary),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source) is primary
    fallback.assert_not_called()


def test_explicit_checker_is_never_probed_or_replaced(tmp_path: Path) -> None:
    source = tmp_path / "book.epub"
    explicit = StubChecker(EpubCheckResult(("custom",), 1, errors=(nav_error(),)))
    with (
        patch("engine.epub.checker.checker_command", return_value=explicit),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source, "custom checker") is explicit
    assert explicit.checked == []
    fallback.assert_not_called()


def test_environment_checker_is_never_probed_or_replaced(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "book.epub"
    configured = StubChecker(EpubCheckResult(("custom",), 1, errors=(nav_error(),)))
    monkeypatch.setenv("EPUBCHECK_COMMAND", "custom checker")
    with (
        patch("engine.epub.checker.checker_command", return_value=configured),
        patch("engine.epub.checker._portable_checker") as fallback,
    ):
        assert checker_for_source(source) is configured
    assert configured.checked == []
    fallback.assert_not_called()
