"""The single v2.5 translation and review executor."""

from __future__ import annotations

from engine.agents.protocol import ProtocolError
from engine.core.quality import find_degenerate_translation
from engine.execution.revision import Revision
from engine.execution.state import _Job
from engine.execution.utility import (
    _segment,
)
from engine.item.inline import (
    plain_text,
    validate_projection,
)
from engine.schemas.contracts import (
    ItemStatus,
    RequestManifest,
    canonical_hash,
)


class Translation(Revision):
    def _apply_translation(self, manifest: RequestManifest, job: _Job, target: str) -> None:
        record = self._current_for(manifest, job)
        unit = self.units[job.unit_id]
        segment = _segment(record, job.item_id)
        validate_projection(segment.source_projection, target, unit.registry)
        degeneration = find_degenerate_translation(plain_text(segment.source_projection), plain_text(target))
        if degeneration:
            raise ProtocolError(degeneration)
        item = record.items[job.item_id].model_copy(
            update={
                "status": ItemStatus.LOCAL_VALID,
                "target_projection": target,
                "target_hash": canonical_hash(target),
                "request_id": manifest.request_id,
                "failure": None,
                "next_action": "review",
            }
        )
        self._save(record, items=dict(record.items) | {job.item_id: item}, candidate=None, review=None)
