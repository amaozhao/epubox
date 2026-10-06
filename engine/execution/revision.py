"""The single v2.5 translation and review executor."""

from __future__ import annotations

from collections.abc import Mapping

from engine.agents.protocol import ProtocolError
from engine.execution.state import _Job
from engine.execution.utility import (
    _segment,
)
from engine.execution.workflow import Workflow
from engine.schemas.contracts import (
    ItemStatus,
    JsonValue,
    RequestManifest,
    UnitRecord,
)


class Revision(Workflow):
    def _current_for(self, manifest: RequestManifest, job: _Job) -> UnitRecord:
        record = self.store.read_unit(job.unit_id)
        item = record.items[job.item_id]
        segment = _segment(record, job.item_id)
        if (
            manifest.stage != job.stage
            or manifest.freeze_id != self.book.freeze_id
            or manifest.glossary_file_sha256 != self.book.glossary_file_sha256
            or manifest.item_unit_ids.get(job.item_id) != (job.unit_id,)
            or manifest.unit_document_ids.get(job.unit_id) != record.document_id
            or record.record_version < manifest.record_versions[job.unit_id]
            or item.request_id != manifest.request_id
            or record.input_hash != manifest.input_hashes[job.item_id]
            or record.plan_epoch != manifest.plan_epochs[job.unit_id]
            or record.revision != manifest.revisions[job.unit_id]
            or segment.selected_term_ids != manifest.term_ids_by_item[job.item_id]
            or segment.terms_hash != manifest.terms_hashes[job.item_id]
            or segment.context_hash != manifest.context_hashes[job.item_id]
            or (manifest.stage == "review" and item.target_hash != manifest.target_hashes[job.item_id])
        ):
            raise ProtocolError(f"stale response identity: {job.item_id}")
        self.records[job.unit_id] = record
        return record

    def _fail_item(
        self,
        record: UnitRecord,
        item_id: str,
        stage: str,
        message: str,
        *,
        retry: bool,
        code: str = "request_failed",
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        current = self.store.read_unit(record.unit_id)
        item = current.items[item_id]
        limit = self._logical_limit(current, item_id, stage)
        blocking_review = stage == "review" and any(
            issue.get("code") == "blocking_review" and issue.get("item_id") == item_id
            for issue in current.unresolved_issues
        )
        retry = (
            retry
            and not blocking_review
            and self._logical_calls(item_id, stage, current.revision if stage == "review" else None) < limit
            and max(current.counters.get("http_attempts", 0), self._spent_by_unit.get(current.unit_id, 0))
            < self._unit_limit(current)
        )
        failed = item.model_copy(
            update={
                "stage": stage,
                "status": ItemStatus.RETRY_WAIT if retry else ItemStatus.NEEDS_ATTENTION,
                "failure": {"stage": stage, "code": code, "message": message[:2000], **dict(details or {})},
                "next_action": stage if retry else "repair",
            }
        )
        self._save(current, items=dict(current.items) | {item_id: failed})

    def _fail_remaining(self, record: UnitRecord, code: str, message: str) -> None:
        current = self.store.read_unit(record.unit_id)
        items = {
            item_id: item
            if item.status == ItemStatus.REVIEWED
            else item.model_copy(
                update={
                    "status": ItemStatus.NEEDS_ATTENTION,
                    "failure": {"stage": item.stage, "code": code, "message": message[:2000]},
                    "next_action": "repair",
                }
            )
            for item_id, item in current.items.items()
        }
        self._save(current, items=items)
