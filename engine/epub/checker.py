"""Resolve the local EPUBCheck executable for source and output validation."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from engine.epub.validation import EpubChecker

_TOOLS = Path(__file__).resolve().parent.parent.parent / ".tools" / "epubcheck"


def checker_command(command: str | None = None) -> EpubChecker:
    configured = command or os.environ.get("EPUBCHECK_COMMAND")
    if configured:
        return EpubChecker(shlex.split(configured))
    java = sorted(_TOOLS.glob("jdk*/Contents/Home/bin/java"))
    jars = sorted(_TOOLS.glob("epubcheck*/epubcheck.jar"))
    if java and jars:
        pinned = _TOOLS / "epubcheck-5.3.0" / "epubcheck.jar"
        return EpubChecker((str(java[-1]), "-jar", str(pinned if pinned.is_file() else jars[-1])))
    return EpubChecker()


def checker_for_source(source: Path, command: str | None = None) -> EpubChecker:
    _ = source
    return checker_command(command)


__all__ = ["checker_command", "checker_for_source"]
