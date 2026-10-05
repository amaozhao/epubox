"""T00 owns the complete inventory; later task files stay explicitly deferred."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXED = {"__init__.py", "pyproject.toml", ".gitignore", ".env.example", "uv.lock"}


def test_all_baseline_files_have_an_owner_and_current_stage_files_conform() -> None:
    inventory = ROOT / "docs/files.md"
    rows = []
    for line in inventory.read_text().splitlines():
        if not line.startswith("| "):
            continue
        columns = [part.strip() for part in line.split("|")[1:-1]]
        if len(columns) == 5 and columns[2].isdigit():
            rows.append(columns)
    assert len(rows) == 120
    assert len({row[0] for row in rows}) == len(rows)
    for old, destination, _, owner, state in rows:
        assert owner in {f"T{number:02}" for number in range(21)}, old
        if owner not in {"T00", "T01", "T02"}:
            continue
        assert state != "待实施", old
        path = ROOT / destination.split("；", 1)[0]
        assert path.is_file(), path
        if path.name not in FIXED:
            assert path.stem.isalpha() and path.stem.isascii(), path
        assert len(path.read_bytes().splitlines()) <= 1000, path


def test_current_stage_new_files_and_documents_conform() -> None:
    files = [
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "engine/item/budget.py",
        ROOT / "tests/engine/item/budget.py",
        ROOT / "tests/engine/core/config.py",
        ROOT / "tests/engine/schemas/bridge.py",
        ROOT / "tests/engine/schemas/files.py",
        *sorted((ROOT / "engine/schemas").glob("*.py")),
    ]
    for path in files:
        if path.name not in FIXED:
            assert path.stem.isalpha() and path.stem.isascii(), path
        assert len(path.read_bytes().splitlines()) <= 1000, path
