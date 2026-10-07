from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

import engine.item.members as members_module
from engine.services.preparation import prepare_translation
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.preparing import atomic_config, forbidden


def test_blocked_member_packing_returns_actionable_preflight_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_epub(
        tmp_path / "source.epub",
        {"chapter.xhtml": "<p>This sentence definitely exceeds one token.</p>"},
    )
    original = members_module.pack_members
    captured = {}

    def block_members(stage, items, glossary, index, limits, **kwargs):
        receipt = next((tmp_path / "work").glob("*/atomic-run/checks/preflight.json"))
        captured["receipt"] = receipt.read_bytes()
        result = original(
            stage, items, glossary, index, replace(limits, source_tokens=1, source_tolerance_tokens=0), **kwargs
        )
        blocked = next(item for item in result.blocked if item.resource_path == "OEBPS/chapter.xhtml")
        result = result.model_copy(update={"blocked": (blocked,)})
        captured["packing"] = result
        return result

    monkeypatch.setattr(members_module, "pack_members", block_members)
    result = asyncio.run(
        prepare_translation(
            source,
            tmp_path / "work",
            atomic_config(),
            StubChecker(),
            term_transport=forbidden,
            resolution_transport=forbidden,
        )
    )

    packing = captured["packing"]
    blocked = packing.blocked[0]
    diagnostic = result.diagnostics[0]
    assert result.status == "needs_attention" and result.phase == "preflight"
    assert len(packing.blocked) == len(result.diagnostics) == 1
    assert diagnostic.status == "blocked"
    assert diagnostic.resource_path == blocked.resource_path == "OEBPS/chapter.xhtml"
    assert diagnostic.item_id == blocked.item_id
    assert (
        diagnostic.source_tokens,
        diagnostic.input_tokens,
        diagnostic.output_tokens,
        diagnostic.context_tokens,
        diagnostic.failures,
    ) == (
        blocked.budget.source_tokens,
        blocked.budget.input_reserve,
        blocked.budget.output_tokens,
        blocked.budget.context_tokens,
        blocked.budget.failures,
    )
    assert blocked.budget.failures == (f"source budget {blocked.budget.source_tokens} exceeds 1",)
    assert result.reason is not None
    assert all(value in result.reason for value in (blocked.resource_path, blocked.item_id, *blocked.budget.failures))
    receipt = result.work_dir / "checks" / "preflight.json"
    assert receipt.read_bytes() == captured["receipt"]
    assert not (result.work_dir / "prepared.json").exists()
    assert not list((result.work_dir / "requests").glob("*.json"))
