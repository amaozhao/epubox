from types import SimpleNamespace
from typing import Any, cast

import pytest

from engine.schemas.contracts import Attempt
from engine.services.atomic import IdentityMismatch
from engine.services.journal import BodyJournal


@pytest.mark.parametrize("unlimited", (False, True))
def test_compact_reservation_excludes_output_estimate_only_for_unlimited_requests(unlimited):
    request = SimpleNamespace(stage="translate", context_unlimited=False, output_unlimited=unlimited)
    batch = SimpleNamespace(
        budget=SimpleNamespace(
            output_tokens=8192,
            identity=SimpleNamespace(input_limit=2000, context_limit=1000, safety_tokens=256),
        )
    )
    reserved = []

    def reserve(request_id, attempt):
        reserved.append(attempt)
        return request

    journal = cast(Any, BodyJournal.__new__(BodyJournal))
    journal.session = SimpleNamespace(verify=lambda: None)
    journal.store = SimpleNamespace(reserve_attempt=reserve)
    journal._requests = {"test": request}
    journal._translation_batch = lambda _: batch
    journal._ambiguous = lambda _: False
    journal._run_limit = 0
    journal._run_attempts = journal._body_attempts = 0
    journal._ambiguous_cache = {}
    attempt = Attempt(
        attempt_id="test",
        created_at="2026-10-09T00:00:00+00:00",
        affected_items=("member",),
        reservation={"estimated_input_tokens": 700, "attempt_number": 1},
        metadata={"wire_version": "epubox-wire-7"},
    )

    if unlimited:
        journal._reserve("test", attempt)
        assert reserved == [attempt]
        assert journal._run_attempts == 1
    else:
        with pytest.raises(IdentityMismatch, match="input capacity"):
            journal._reserve("test", attempt)
        assert reserved == []
