"""Resolve the local EPUBCheck executable for source and output validation."""

from __future__ import annotations

import os
import re
import shlex
import zipfile
from pathlib import Path

from engine.epub.validation import (
    EpubChecker,
    ZipLimits,
    _container_rootfiles,
    _manifest_items,
    _parse_xml_bytes,
    _validate_zip,
)

_TOOLS = Path(__file__).resolve().parent.parent.parent / ".tools" / "epubcheck"


def checker_command(command: str | None = None) -> EpubChecker:
    configured = command or os.environ.get("EPUBCHECK_COMMAND")
    if configured:
        return EpubChecker(shlex.split(configured))
    java = sorted(_TOOLS.glob("jdk*/Contents/Home/bin/java"))
    jars = sorted(_TOOLS.glob("epubcheck*/epubcheck.jar"))
    if java and jars:
        return EpubChecker((str(java[-1]), "-jar", str(jars[-1])))
    return EpubChecker()


def _portable_checker(version: str) -> EpubChecker | None:
    java = sorted(_TOOLS.glob("jdk*/Contents/Home/bin/java"))
    jar = _TOOLS / f"epubcheck-{version}" / "epubcheck.jar"
    return EpubChecker((str(java[-1]), "-jar", str(jar))) if java and jar.is_file() else None


def _known_epubcheck_54_nav_errors(source: Path, errors: tuple[str, ...]) -> bool:
    if not errors:
        return False
    try:
        with zipfile.ZipFile(source) as archive:
            entries = _validate_zip(archive, ZipLimits())

            def read_xml(name: str) -> bytes:
                if entries[name] > 8 * 1024 * 1024:
                    raise ValueError("navigation check resource exceeds the safe probe size")
                return archive.read(name)

            rootfiles = _container_rootfiles(
                _parse_xml_bytes(read_xml("META-INF/container.xml"), "META-INF/container.xml")
            )
            if len(rootfiles) != 1:
                return False
            opf_path = rootfiles[0]
            package = _parse_xml_bytes(read_xml(opf_path), opf_path)
            if package.attrib.get("version") != "3.0":
                return False
            nav_path = next(
                (item.path for item in _manifest_items(package, opf_path, entries) if "nav" in item.properties),
                None,
            )
            if not nav_path:
                return False
            nav_bytes = read_xml(nav_path)
            _parse_xml_bytes(nav_bytes, nav_path)
            nav_lines = nav_bytes.decode("utf-8").splitlines()
    except (KeyError, UnicodeDecodeError, ValueError, zipfile.BadZipFile):
        return False

    location = re.compile(rf"/{re.escape(nav_path)}\((\d+),\d+\)")
    for error in errors:
        match = location.search(error)
        attribute = re.search(r'attribute "(aria-label|aria-labelledby)" not allowed here', error)
        if "ERROR(RSC-005):" not in error or attribute is None or match is None:
            return False
        line_number = int(match.group(1))
        if not 1 <= line_number <= len(nav_lines) or not re.search(
            rf"<nav\b[^>]*\b{re.escape(attribute.group(1))}\s*=", nav_lines[line_number - 1]
        ):
            return False
    return True


def checker_for_source(source: Path, command: str | None = None) -> EpubChecker:
    checker = checker_command(command)
    if command or os.environ.get("EPUBCHECK_COMMAND"):
        return checker
    if not any(Path(part).parent.name == "epubcheck-5.4.0" for part in checker.command):
        return checker
    result = checker.check(source)
    if result.passed or result.fatals or not _known_epubcheck_54_nav_errors(source, result.errors):
        return checker
    fallback = _portable_checker("5.3.0")
    return fallback if fallback is not None and fallback.check(source).passed else checker


__all__ = ["checker_command", "checker_for_source"]
