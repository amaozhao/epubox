"""Byte-budgeted views over verified immutable JSON records.

The loader reports an estimated resident size, not just the number of records.
Values larger than the budget can be used by the caller but are never retained.
The owning ReadySession replaces these views when source dependencies change.
"""

from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from sys import getsizeof
from typing import overload

from pydantic import BaseModel


class RecordView[T](Mapping[str, T]):
    def __init__(
        self,
        keys: Sequence[str],
        load: Callable[[str], tuple[T, int]],
        *,
        max_bytes: int,
    ) -> None:
        if max_bytes < 0 or len(set(keys)) != len(keys):
            raise ValueError("record view requires unique keys and a nonnegative budget")
        self._keys = dict.fromkeys(keys)
        self._load = load
        self.max_bytes = max_bytes
        self.cached_bytes = 0
        self._cache: OrderedDict[str, tuple[T, int]] = OrderedDict()

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: object) -> bool:
        return key in self._keys

    def __getitem__(self, key: str) -> T:
        if key not in self._keys:
            raise KeyError(key)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key][0]
        value, size = self._load(key)
        if size < 0:
            raise ValueError("record resident size must be nonnegative")
        if size <= self.max_bytes:
            while self._cache and self.cached_bytes + size > self.max_bytes:
                _, (_, removed_size) = self._cache.popitem(last=False)
                self.cached_bytes -= removed_size
            self._cache[key] = value, size
            self.cached_bytes += size
        return value


class RecordSequence[T](Sequence[T]):
    def __init__(self, records: Mapping[str, T]) -> None:
        self._records = records
        self._keys = tuple(records)

    def __len__(self) -> int:
        return len(self._keys)

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[T, ...]: ...

    def __getitem__(self, index: int | slice) -> T | tuple[T, ...]:
        if isinstance(index, slice):
            return tuple(self._records[key] for key in self._keys[index])
        return self._records[self._keys[index]]

    def __iter__(self) -> Iterator[T]:
        return (self._records[key] for key in self._keys)


class BudgetCache[K, T](MutableMapping[K, T]):
    """LRU for recomputable model/proof values, never authoritative run state."""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 0:
            raise ValueError("cache budget must be nonnegative")
        self.max_bytes = max_bytes
        self.cached_bytes = 0
        self._cache: OrderedDict[K, tuple[T, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._cache)

    def __iter__(self) -> Iterator[K]:
        return iter(tuple(self._cache))

    def __getitem__(self, key: K) -> T:
        value, _ = self._cache[key]
        self._cache.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: T) -> None:
        if key in self._cache:
            del self[key]
        size = _resident_size((key, value), self.max_bytes)
        if size > self.max_bytes:
            return
        while self._cache and self.cached_bytes + size > self.max_bytes:
            _, (_, removed_size) = self._cache.popitem(last=False)
            self.cached_bytes -= removed_size
        self._cache[key] = value, size
        self.cached_bytes += size

    def __delitem__(self, key: K) -> None:
        _, size = self._cache.pop(key)
        self.cached_bytes -= size

    def clear(self) -> None:
        self._cache.clear()
        self.cached_bytes = 0


def _resident_size(value: object, limit: int) -> int:
    # No JSON serialization or model_dump is needed just to budget a cache.
    pending = [value]
    seen: set[int] = set()
    total = 0
    while pending:
        child = pending.pop()
        if id(child) in seen:
            continue
        seen.add(id(child))
        total += getsizeof(child)
        if total > limit:
            return total
        if isinstance(child, BaseModel):
            pending.append(child.__dict__)
            pending.append(child.__pydantic_fields_set__)
        elif isinstance(child, dict):
            pending.extend(child.keys())
            pending.extend(child.values())
        elif isinstance(child, (tuple, list, set, frozenset)):
            pending.extend(child)
    return total
