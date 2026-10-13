"""T00 owns the inventory; implemented task files must satisfy its constraints."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXED = {"__init__.py", "pyproject.toml", ".gitignore", ".env.example", "uv.lock"}
WORDS = frozenset(
    [
        "adversary",
        "alignment",
        "assembly",
        "association",
        "atomic",
        "atoms",
        "audit",
        "base",
        "bindings",
        "boundaries",
        "bridge",
        "budget",
        "candidates",
        "capacity",
        "cardinality",
        "ceiling",
        "checker",
        "citations",
        "cli",
        "code",
        "coherence",
        "command",
        "compact",
        "config",
        "constant",
        "constraints",
        "context",
        "contracts",
        "conversion",
        "custody",
        "diagnostics",
        "engine",
        "execution",
        "extractor",
        "factory",
        "failures",
        "feedback",
        "files",
        "fill",
        "fingerprint",
        "fitting",
        "frames",
        "freeze",
        "hashing",
        "history",
        "index",
        "inline",
        "inputs",
        "integration",
        "internal",
        "journal",
        "legacy",
        "lifecycle",
        "limit",
        "lists",
        "logger",
        "main",
        "manual",
        "markers",
        "markup",
        "members",
        "metadata",
        "minimum",
        "models",
        "navigation",
        "ncx",
        "orchestrator",
        "outcomes",
        "output",
        "overlay",
        "packing",
        "parallel",
        "parsing",
        "partition",
        "parts",
        "performance",
        "physical",
        "planner",
        "planning",
        "policy",
        "pool",
        "preflight",
        "preparation",
        "preparing",
        "profiling",
        "progress",
        "projection",
        "proofs",
        "protocol",
        "provider",
        "publication",
        "publish",
        "quality",
        "ranges",
        "ready",
        "recovery",
        "repair",
        "replace",
        "replay",
        "report",
        "request",
        "resolution",
        "response",
        "restarts",
        "resume",
        "retry",
        "review",
        "reviews",
        "revision",
        "run",
        "runner",
        "runtime",
        "scheduling",
        "session",
        "shards",
        "source",
        "sparse",
        "speed",
        "startup",
        "state",
        "storage",
        "store",
        "stream",
        "streaming",
        "structure",
        "styles",
        "support",
        "terms",
        "timing",
        "tokens",
        "translation",
        "upgrade",
        "utility",
        "validation",
        "verification",
        "views",
        "wire",
        "workflow",
        "working",
    ]
)


def _assert_name(path: Path) -> None:
    if path.name in FIXED:
        return
    if path.suffix == ".py" and "tests" in path.parts:
        matched = re.fullmatch(r"test_([a-z]+)\.py", path.name)
        assert matched is not None, path
        assert matched.group(1) in WORDS, path
        return
    assert path.stem in WORDS if path.suffix == ".py" else path.stem.isalpha() and path.stem.isascii(), path


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
        if owner not in {f"T{number:02}" for number in range(20)}:
            continue
        assert state != "待实施", old
        path = ROOT / destination.split("；", 1)[0]
        assert path.is_file(), path
        _assert_name(path)
        assert len(path.read_bytes().splitlines()) <= 1000, path


def test_current_stage_new_files_and_documents_conform() -> None:
    files = [
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "engine/item/budget.py",
        ROOT / "tests/engine/item/test_budget.py",
        ROOT / "tests/engine/core/test_config.py",
        ROOT / "tests/engine/schemas/test_bridge.py",
        ROOT / "tests/engine/schemas/test_files.py",
        ROOT / "engine/epub/parsing.py",
        ROOT / "engine/epub/ranges.py",
        ROOT / "engine/item/atoms.py",
        ROOT / "engine/item/structure.py",
        ROOT / "engine/item/metadata.py",
        ROOT / "engine/item/projection.py",
        ROOT / "engine/item/policy.py",
        ROOT / "engine/item/views.py",
        ROOT / "tests/engine/epub/test_parsing.py",
        ROOT / "tests/engine/epub/test_ranges.py",
        ROOT / "tests/engine/item/test_atoms.py",
        ROOT / "tests/engine/item/test_extractor.py",
        ROOT / "tests/engine/item/test_views.py",
        ROOT / "engine/epub/fill.py",
        ROOT / "tests/engine/epub/test_fill.py",
        ROOT / "tests/engine/item/test_markers.py",
        ROOT / "tests/engine/item/test_inline.py",
        ROOT / "tests/engine/epub/test_assembly.py",
        ROOT / "engine/services/terms/inputs.py",
        ROOT / "engine/services/terms/planning.py",
        ROOT / "tests/engine/services/terms/test_inputs.py",
        ROOT / "tests/engine/services/terms/test_planning.py",
        ROOT / "engine/services/preflight.py",
        ROOT / "tests/engine/services/test_preflight.py",
        ROOT / "engine/agents/terms.py",
        ROOT / "engine/services/terms/runner.py",
        ROOT / "engine/services/terms/storage.py",
        ROOT / "engine/services/terms/candidates.py",
        ROOT / "engine/services/terms/freeze.py",
        ROOT / "engine/services/terms/resolution.py",
        ROOT / "tests/engine/services/terms/test_runner.py",
        ROOT / "tests/engine/services/terms/test_execution.py",
        ROOT / "tests/engine/services/terms/test_response.py",
        ROOT / "tests/engine/services/terms/test_storage.py",
        ROOT / "tests/engine/services/terms/test_candidates.py",
        ROOT / "tests/engine/services/terms/test_freeze.py",
        ROOT / "tests/engine/services/terms/test_resolution.py",
        ROOT / "tests/engine/services/terms/test_audit.py",
        ROOT / "tests/engine/agents/test_budget.py",
        ROOT / "engine/item/context.py",
        ROOT / "engine/item/request.py",
        ROOT / "engine/item/packing.py",
        ROOT / "tests/engine/item/test_planner.py",
        ROOT / "tests/engine/item/test_request.py",
        ROOT / "tests/engine/item/test_packing.py",
        ROOT / "engine/item/members.py",
        ROOT / "tests/engine/item/test_members.py",
        ROOT / "engine/services/preparation.py",
        ROOT / "engine/services/ready.py",
        ROOT / "tests/engine/services/test_preparation.py",
        ROOT / "tests/engine/services/test_preparing.py",
        ROOT / "tests/engine/services/test_ready.py",
        ROOT / "engine/agents/streaming.py",
        ROOT / "engine/agents/workflow.py",
        ROOT / "tests/engine/agents/test_models.py",
        ROOT / "tests/engine/agents/test_protocol.py",
        ROOT / "tests/engine/agents/test_provider.py",
        ROOT / "tests/engine/agents/test_runtime.py",
        ROOT / "tests/engine/agents/test_workflow.py",
        ROOT / "engine/services/journal.py",
        ROOT / "engine/services/custody.py",
        ROOT / "engine/services/resume.py",
        ROOT / "tests/engine/services/test_journal.py",
        ROOT / "tests/engine/services/test_resume.py",
        ROOT / "tests/engine/services/test_store.py",
        ROOT / "tests/engine/services/test_contracts.py",
        ROOT / "engine/epub/bindings.py",
        ROOT / "engine/epub/checker.py",
        ROOT / "engine/epub/publication.py",
        ROOT / "engine/epub/publish.py",
        ROOT / "engine/epub/verification.py",
        ROOT / "tests/engine/epub/test_bindings.py",
        ROOT / "tests/engine/epub/test_checker.py",
        ROOT / "tests/engine/epub/test_publication.py",
        ROOT / "tests/engine/epub/test_publish.py",
        ROOT / "engine/cli.py",
        ROOT / "main.py",
        ROOT / "tests/engine/test_cli.py",
        ROOT / "tests/engine/test_command.py",
        ROOT / "tests/test_main.py",
        ROOT / "tests/engine/services/test_report.py",
        *sorted((ROOT / "engine/execution").glob("*.py")),
        *sorted((ROOT / "tests/engine/execution").glob("*.py")),
        *sorted((ROOT / "engine/schemas").glob("*.py")),
    ]
    for path in files:
        _assert_name(path)
        assert len(path.read_bytes().splitlines()) <= 1000, path


def test_all_python_files_follow_the_single_word_contract() -> None:
    files = [
        *sorted((ROOT / "engine").rglob("*.py")),
        *sorted((ROOT / "tests").rglob("*.py")),
        *sorted((ROOT / "benchmarks").rglob("*.py")),
        ROOT / "main.py",
    ]
    for path in files:
        _assert_name(path)


def test_acceptance_matrix_references_existing_test_files() -> None:
    matrix = ROOT / "docs/history/implementation/tests.md"
    references = set(re.findall(r"`(tests/[^`:]+\.py)", matrix.read_text()))
    assert references
    assert not [reference for reference in sorted(references) if not (ROOT / reference).is_file()]
