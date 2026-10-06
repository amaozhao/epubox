"""The single v2.5 translation and review executor."""

from __future__ import annotations

from engine.agents.runtime import (
    RuntimePaused,
)
from engine.execution.request import Request
from engine.execution.state import TranslationPaused, TranslationRunResult
from engine.services.atomic import StoreError
from engine.services.coherence import (
    prepare_document_check,
    window_payload,
)


class TranslationEngine(Request):
    """Run only persisted BookPlan-3 work; every network result is saved item by item."""

    async def run(self) -> TranslationRunResult:
        with self.store.lock(blocking=False):
            return await self._run_locked()

    async def _run_locked(self) -> TranslationRunResult:
        paused_reason: str | None = None
        try:
            self._prepare_checks()
            self._replay_item_responses()
            self._recover_in_flight()
            self._emit_progress("translation", "running")
            while not paused_reason:
                while True:
                    self._advance_local_state()
                    jobs = self._ready_jobs()
                    if not jobs:
                        if self._recover_terminal_items():
                            continue
                        break
                    batches = self._pack_jobs(jobs)
                    if not batches:
                        break
                    for batch in batches:
                        try:
                            await self._run_batch(batch)
                        except (TranslationPaused, RuntimePaused) as error:
                            paused_reason = str(error)
                            break
                        self._emit_progress(batch[0].stage, "running")
                    if paused_reason:
                        break
                if paused_reason:
                    break
                paused_reason, revised = await self._run_coherence()
                self._emit_progress("coherence", "running")
                if not revised:
                    break
        except StoreError as error:
            return self._result("failed", str(error))
        if paused_reason:
            return self._result("paused", paused_reason)
        status, reason = self._outcome()
        return self._result(status, reason)

    def _prepare_checks(self) -> None:
        initial = not self.checks
        self.checks = {
            document_id: prepare_document_check(self.store, document, self.records)
            for document_id, document in self.documents.items()
        }
        if initial and not self._hard_run_limit:
            self.run_limit += sum(int(check["http_limit"]) for check in self.checks.values())
        if initial:
            self.predicted_http_requests += sum(
                len(self._pack_coherence_items([window_payload(window, self.records) for window in check["windows"]]))
                for check in self.checks.values()
            )
