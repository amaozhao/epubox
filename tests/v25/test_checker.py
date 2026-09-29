from pathlib import Path

import pytest

from engine.epub import checker as checker_module
from engine.epub.checker import checker_command, checker_for_source


def test_explicit_epubcheck_command_is_used_without_source_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"not opened by command selection")
    monkeypatch.setenv("EPUBCHECK_COMMAND", "java -jar /tmp/epubcheck.jar")

    assert checker_command().command == ("java", "-jar", "/tmp/epubcheck.jar")
    assert checker_for_source(source).command == ("java", "-jar", "/tmp/epubcheck.jar")


def test_default_uses_one_pinned_bundled_checker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EPUBCHECK_COMMAND", raising=False)
    java = tmp_path / "jdk-21" / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.touch()
    for version in ("5.3.0", "5.4.0"):
        jar = tmp_path / f"epubcheck-{version}" / "epubcheck.jar"
        jar.parent.mkdir()
        jar.touch()
    monkeypatch.setattr(checker_module, "_TOOLS", tmp_path)

    assert checker_command().command == (str(java), "-jar", str(tmp_path / "epubcheck-5.3.0" / "epubcheck.jar"))
