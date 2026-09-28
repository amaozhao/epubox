from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.schemas.v25 import canonical_hash
from engine.services.term_inputs import load_user_terms


def load(path: Path | None):  # type: ignore[no-untyped-def]
    return load_user_terms(path, document_ids={"d1", "d2"}, unit_ids={"u1", "u2"})


def test_missing_configuration_is_an_explicit_empty_snapshot() -> None:
    terms, terms_hash = load(None)
    assert terms == ()
    assert terms_hash == canonical_hash(())


def test_named_file_must_exist_and_contain_strict_json(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load(tmp_path / "missing.json")

    broken = tmp_path / "broken.json"
    broken.write_text('{"cache":', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load(broken)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"cache":"缓存","cache":"高速缓存"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        load(duplicate)


def test_legacy_map_gets_preferred_book_defaults_and_stable_local_ids(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text('{"cache":"缓存","memory":"内存"}', encoding="utf-8")

    first, first_hash = load(path)
    second, second_hash = load(path)

    assert first == second
    assert first_hash == second_hash == canonical_hash(first)
    assert {term.source: term.target for term in first} == {"cache": "缓存", "memory": "内存"}
    assert all(term.term_id.startswith("ut-") for term in first)
    assert all(term.aliases == () for term in first)
    assert all(term.scope.kind == "book" for term in first)
    assert all(term.mode == "preferred" and term.match_policy == "exact" and term.note == "" for term in first)


def test_explicit_hard_rules_and_all_scope_shapes_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps(
            [
                {"source": "RAM", "target": "内存", "mode": "required", "scope": {"kind": "book"}},
                {
                    "source": "OpenAI",
                    "mode": "keep_source",
                    "aliases": ["OPENAI", "OpenAI"],
                    "scope": {"kind": "documents", "document_ids": ["d2", "d1", "d1"]},
                },
                {
                    "source": " agent ",
                    "target": " 智能体 ",
                    "scope": {"kind": "units", "unit_ids": ["u2"]},
                    "match_policy": "casefold",
                    "note": " Only in the defined AI sense. ",
                },
            ]
        ),
        encoding="utf-8",
    )

    terms, _ = load(path)
    by_source = {term.source: term for term in terms}
    assert by_source["RAM"].mode == "required"
    assert by_source["OpenAI"].mode == "keep_source"
    assert by_source["OpenAI"].aliases == ("OPENAI", "OpenAI")
    assert by_source["OpenAI"].scope.document_ids == ("d1", "d2")
    assert by_source["agent"].scope.unit_ids == ("u2",)
    assert by_source["agent"].match_policy == "casefold"
    assert by_source["agent"].note == "Only in the defined AI sense."


@pytest.mark.parametrize(
    "scope, message",
    [
        ({"kind": "documents", "document_ids": ["missing"]}, "unknown document"),
        ({"kind": "units", "unit_ids": ["missing"]}, "unknown Unit"),
        ({"kind": "documents", "document_ids": []}, "requires only document_ids"),
    ],
)
def test_scope_ids_and_shapes_must_match_this_book(tmp_path: Path, scope: dict[str, object], message: str) -> None:
    path = tmp_path / "terms.json"
    path.write_text(json.dumps([{"source": "cache", "target": "缓存", "scope": scope}]), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load(path)


def test_loader_does_not_modify_user_file_and_preserves_conflicting_rules(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text('[{"source":"memory","target":"内存"},{"source":"memory","target":"记忆"}]', encoding="utf-8")
    before = path.read_bytes()

    terms, _ = load(path)

    assert path.read_bytes() == before
    assert {(term.source, term.target) for term in terms} == {("memory", "内存"), ("memory", "记忆")}


def test_illegal_structure_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "terms.json"
    path.write_text('[{"source":"cache","target":"缓存","unexpected":true}]', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        load(path)
