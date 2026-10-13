import pytest

from engine.services.working import RecordSequence, RecordView


def test_record_view_is_lazy_and_evicts_by_bytes_in_lru_order():
    reads = []

    def load(key):
        reads.append(key)
        return key.upper(), 6

    view = RecordView(("a", "b", "c"), load, max_bytes=12)
    assert len(view) == 3 and tuple(view) == ("a", "b", "c") and "a" in view
    assert reads == []
    assert view["a"] == "A" and view["b"] == "B" and view["a"] == "A"
    assert view["c"] == "C" and view.cached_bytes == 12
    assert view["a"] == "A" and reads == ["a", "b", "c"]
    assert view["b"] == "B" and reads == ["a", "b", "c", "b"]
    with pytest.raises(KeyError):
        _ = view["unknown"]
    assert reads[-1] == "b"


def test_oversized_record_is_not_retained_and_sequence_preserves_order():
    reads = []

    def load(key):
        reads.append(key)
        return key, 20

    view = RecordView(("b", "a"), load, max_bytes=10)
    values = RecordSequence(view)
    assert len(values) == 2 and values[-1] == "a" and values[0] == "b"
    assert values[:] == ("b", "a")
    assert list(values) == ["b", "a"]
    assert view.cached_bytes == 0
    assert reads == ["a", "b", "b", "a", "b", "a"]


def test_budget_cache_evicts_models_without_serializing_them():
    from engine.schemas.contracts import ItemRecord
    from engine.services.working import BudgetCache

    record = ItemRecord(item_id="a", segment_id="a", terms_hash="t", context_hash="c")
    probe = BudgetCache(1_000_000)
    probe["a"] = record
    size = probe.cached_bytes
    assert size > 0
    cache = BudgetCache(size * 2)
    cache["a"] = record
    cache["b"] = record.model_copy(update={"item_id": "b", "segment_id": "b"})
    assert cache["a"] == record
    cache["c"] = record.model_copy(update={"item_id": "c", "segment_id": "c"})
    assert "b" not in cache and set(cache) == {"a", "c"}
    assert dict(cache)["a"] == record  # Iteration must survive LRU touches.
    cache["large"] = "x" * (size * 3)
    assert "large" not in cache
    del cache["a"]
    assert cache.cached_bytes <= size * 2
    cache.clear()
    assert not cache and cache.cached_bytes == 0
