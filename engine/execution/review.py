"""The single v2.5 translation and review executor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from engine.execution.state import _Job
from engine.execution.translation import Translation
from engine.execution.utility import (
    _dedupe_feedback,
    _feedback,
    _segment,
)
from engine.item.inline import (
    validate_projection,
)
from engine.schemas.contracts import (
    ItemStatus,
    JsonValue,
    RequestManifest,
    canonical_hash,
)


class Review(Translation):
    def _apply_review(
        self,
        manifest: RequestManifest,
        job: _Job,
        result: Mapping[str, Any],
        rejected_suggestions: tuple[str, ...],
    ) -> None:
        record = self._current_for(manifest, job)
        item = record.items[job.item_id]
        feedback = _feedback(
            record,
            job.item_id,
            manifest.request_id,
            result.get("term_suggestions", ()),
            rejected_suggestions,
            self.documents[record.document_id],
            self.units[job.unit_id],
        )
        decision = result["decision"]
        blocking_review = any(
            issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id
            for issue in record.unresolved_issues
        )
        if blocking_review and decision != "replace":
            self._fail_item(
                record,
                job.item_id,
                "review",
                "automatic review recovery requires a replacement target",
                retry=False,
                code="review_replacement_required",
            )
            return
        if decision == "needs_attention":
            issue_values: list[JsonValue] = [dict(issue) for issue in result.get("issues", ())]
            self._fail_item(
                record,
                job.item_id,
                "review",
                str(result.get("issues", "review needs attention")),
                retry=False,
                code="review_needs_attention",
                details={"issues": issue_values},
            )
            if feedback:
                current = self.records[job.unit_id]
                self._save(current, term_feedback=_dedupe_feedback((*current.term_feedback, *feedback)))
            return
        if decision == "replace":
            review_cycle = record.counters.get("review_cycle", 0)
            replacement_key = f"replacement_cycle:{job.item_id}"
            has_item_cycles = any(key.startswith("replacement_cycle:") for key in record.counters)
            if record.counters.get(replacement_key) == review_cycle or (
                not has_item_cycles and record.counters.get("replacement_cycle") == review_cycle
            ):
                self._fail_item(
                    record,
                    job.item_id,
                    "review",
                    "replacement review limit exhausted",
                    retry=False,
                    code="replacement_review_limit_exhausted",
                )
                return
            target = str(result["target"])
            segment = _segment(record, job.item_id)
            validate_projection(segment.source_projection, target, self.units[job.unit_id].registry)
            counters = dict(record.counters) | {"replacement_cycle": review_cycle, replacement_key: review_cycle}
            replaced = item.model_copy(
                update={
                    "status": ItemStatus.LOCAL_VALID,
                    "target_projection": target,
                    "target_hash": canonical_hash(target),
                    "checks": {"replacement_requested": True, "previous": result["checks"]},
                    "request_id": manifest.request_id,
                    "failure": None,
                    "next_action": "review",
                }
            )
            revised_items = {
                item_id: current.model_copy(
                    update={
                        "status": ItemStatus.LOCAL_VALID,
                        "checks": {},
                        "request_id": None,
                        "failure": None,
                        "next_action": "review",
                    }
                )
                for item_id, current in record.items.items()
            }
            revised_items[job.item_id] = replaced
            self._save(
                record,
                revision=record.revision + 1,
                items=revised_items,
                candidate=None,
                local_checks={},
                review=None,
                accepted_revision=None,
                accepted_target_hash=None,
                counters=counters,
                term_feedback=_dedupe_feedback((*record.term_feedback, *feedback)),
                unresolved_issues=tuple(
                    issue
                    for issue in record.unresolved_issues
                    if issue.get("code") != "blocking_coherence"
                    and not (issue.get("code") == "blocking_review" and issue.get("item_id") == job.item_id)
                ),
            )
            return
        if any(issue.get("code") == "blocking_coherence" for issue in record.unresolved_issues):
            self._fail_item(
                record,
                job.item_id,
                "review",
                "coherence revision requires a replacement target",
                retry=False,
                code="review_replacement_required",
            )
            return
        reviewed = item.model_copy(
            update={
                "status": ItemStatus.REVIEWED,
                "checks": {"decision": "no_change", "checks": result["checks"], "issues": result["issues"]},
                "request_id": manifest.request_id,
                "failure": None,
                "next_action": None,
            }
        )
        self._save(
            record,
            items=dict(record.items) | {job.item_id: reviewed},
            term_feedback=_dedupe_feedback((*record.term_feedback, *feedback)),
        )
