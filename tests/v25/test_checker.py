from pathlib import Path

import pytest

from engine.epub.checker import checker_command, checker_for_source


def test_explicit_epubcheck_command_is_used_without_source_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"not opened by command selection")
    monkeypatch.setenv("EPUBCHECK_COMMAND", "java -jar /tmp/epubcheck.jar")

    assert checker_command().command == ("java", "-jar", "/tmp/epubcheck.jar")
    assert checker_for_source(source).command == ("java", "-jar", "/tmp/epubcheck.jar")
