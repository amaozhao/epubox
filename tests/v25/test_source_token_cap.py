import pytest

from engine.item.planner import MAX_SOURCE_TOKENS, PlannerConfig, PlanningError, plan_unit, source_token_count
from tests.v23.test_inline_planner import make_unit


def test_source_fragment_cap_splits_a_long_unit_independently_of_request_context() -> None:
    unit = make_unit("A short technical sentence. " * 100)
    plan = plan_unit(unit, PlannerConfig(context_tokens=16_000, max_source_tokens=25))

    assert len(plan.segments) > 1
    assert all(source_token_count(segment.source_projection) <= 25 for segment in plan.segments)


def test_one_unsplittable_grapheme_is_a_local_planning_failure() -> None:
    atom = "a" + "\u0301" * 1500
    assert source_token_count(atom) > MAX_SOURCE_TOKENS
    with pytest.raises(PlanningError, match="cannot fit"):
        plan_unit(make_unit(atom), PlannerConfig(context_tokens=16_000))


def test_source_cap_cannot_be_configured_above_1200() -> None:
    with pytest.raises(ValueError, match="cannot exceed 1200"):
        PlannerConfig(context_tokens=16_000, max_source_tokens=1201)
