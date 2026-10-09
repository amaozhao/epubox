from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from engine.schemas.bridge import RequestBatch
from engine.schemas.budget import MAX_CHUNK_TOKENS, BudgetLimits
from engine.schemas.contracts import RequestManifest
from engine.services.frames import limits as frame_limits
from engine.services.ready import limits_from_config
from tests.engine.schemas.bridge import batch_data


def test_source_hard_limit_caps_only_new_policy_limits() -> None:
    legacy = BudgetLimits(2000, 50_000, 4096, 60_000, source_tolerance_tokens=1000)
    capped = BudgetLimits(
        2000,
        50_000,
        4096,
        60_000,
        source_tolerance_tokens=1000,
        source_hard_limit=MAX_CHUNK_TOKENS,
    )

    assert legacy.source_ceiling == 3000
    assert "source_hard_limit" not in legacy.to_dict()
    assert capped.source_ceiling == MAX_CHUNK_TOKENS
    assert capped.to_dict()["source_hard_limit"] == MAX_CHUNK_TOKENS
    with pytest.raises(ValueError, match="source hard limit"):
        BudgetLimits(2000, 50_000, 4096, 60_000, source_hard_limit=MAX_CHUNK_TOKENS + 1)


def test_manifest_and_budget_must_freeze_the_same_source_policy() -> None:
    legacy = RequestBatch.model_validate(batch_data())
    assert "source_hard_limit" not in legacy.manifest.model_dump(mode="json")
    assert "source_hard_limit" not in legacy.budget.identity.model_dump(mode="json")

    current = batch_data(source_limit=MAX_CHUNK_TOKENS)
    current["manifest"]["source_hard_limit"] = MAX_CHUNK_TOKENS
    current["budget"]["identity"]["source_hard_limit"] = MAX_CHUNK_TOKENS
    assert RequestBatch.model_validate(current).manifest.source_hard_limit == MAX_CHUNK_TOKENS

    current["manifest"]["source_hard_limit"] = None
    with pytest.raises(ValidationError, match="source hard limits differ"):
        RequestBatch.model_validate(current)


def test_saved_manifest_policy_overrides_new_config_during_frame_rebuild() -> None:
    config = {"source_chunk_tokens": 2000, "source_hard_limit": MAX_CHUNK_TOKENS}
    journal = SimpleNamespace(session=SimpleNamespace(prepared=SimpleNamespace(plan=SimpleNamespace(translation_config=config))))
    legacy = RequestManifest.model_validate(batch_data()["manifest"])
    current = legacy.model_copy(update={"source_hard_limit": MAX_CHUNK_TOKENS})

    assert limits_from_config(config).source_hard_limit == MAX_CHUNK_TOKENS
    assert frame_limits(cast(Any, journal), legacy).source_hard_limit is None
    assert frame_limits(cast(Any, journal), current).source_hard_limit == MAX_CHUNK_TOKENS
