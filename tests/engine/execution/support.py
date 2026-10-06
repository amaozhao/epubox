from __future__ import annotations

import json

from engine.agents.runtime import ProviderError
from engine.epub.preparation import PreparationConfig
from engine.item.inline import plain_text
from engine.services.preparation import prepare_translation
from engine.services.store import RunStore
from engine.services.terms.planning import TERM_PLANNER_VERSION
from tests.engine.epub.factory import make_epub
from tests.engine.epub.preparation import StubChecker
from tests.engine.services.contracts import _bookplan, _frozen_store, _unit_record


class ScriptedTransport:
    def __init__(
        self,
        *,
        pause_review: bool = False,
        bad_translation_once: bool = False,
        replace_review_times: int = 0,
        review_decision: str = "no_change",
        review_issues: list[dict] | None = None,
        term_suggestions: list[dict] | None = None,
    ):
        self.pause_review = pause_review
        self.bad_translation_once = bad_translation_once
        self.replace_review_times = replace_review_times
        self.review_decision = review_decision
        self.review_issues = review_issues or []
        self.term_suggestions = term_suggestions or []
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, stage: str, payload: dict):
        self.calls.append((stage, payload))
        item = payload["items"][0]
        if stage == "translate":
            if self.bad_translation_once:
                self.bad_translation_once = False
                return {
                    "raw": json.dumps({"protocol": "epubox-text-1", "request_id": payload["request_id"], "items": []})
                }
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [{"item_id": item["item_id"], "target": "该进程使用内存。"}],
                    },
                    ensure_ascii=False,
                )
            }
        if stage == "coherence":
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-coherence-1",
                        "request_id": payload["request_id"],
                        "items": [{"item_id": item["item_id"], "unit_ids": [], "issues": []}],
                    }
                )
            }
        if self.pause_review:
            raise ProviderError("account paused", status_code=401)
        decision = self.review_decision
        replacement = {}
        if self.replace_review_times:
            self.replace_review_times -= 1
            decision = "replace"
            replacement = {"target": "进程会使用内存。"}
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "base_revision": item["base_revision"],
                            "decision": decision,
                            "checks": {
                                "accuracy": "pass",
                                "fluency": "pass",
                                "terminology": "not_applicable",
                                "bindings": "not_applicable",
                                "script": "pass",
                            },
                            "issues": self.review_issues,
                            "term_suggestions": self.term_suggestions,
                            **replacement,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        }


class PartialBatchTransport:
    def __init__(
        self,
        *,
        coherence_major_once: bool = False,
        coherence_empty_once: bool = False,
        coherence_always_empty: bool = False,
        coherence_omit_once: bool = False,
        coherence_pause_once: bool = False,
        coherence_request_failures: int = 0,
        coherence_malformed_once: bool = False,
        coherence_length_once: bool = False,
        title_target: str | None = None,
    ):
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.omitted: str | None = None
        self.coherence_major_once = coherence_major_once
        self.coherence_empty_once = coherence_empty_once
        self.coherence_always_empty = coherence_always_empty
        self.coherence_omit_once = coherence_omit_once
        self.coherence_pause_once = coherence_pause_once
        self.coherence_request_failures = coherence_request_failures
        self.coherence_malformed_once = coherence_malformed_once
        self.coherence_length_once = coherence_length_once
        self.coherence_omitted: str | None = None
        self.title_target = title_target

    async def __call__(self, stage: str, payload: dict):
        ids = tuple(item["item_id"] for item in payload["items"])
        self.calls.append((stage, ids))
        if stage == "translate":
            included = list(payload["items"])
            if self.omitted is None and len(included) > 1:
                self.omitted = included[1]["item_id"]
                included.pop(1)
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "target": self.title_target
                                if self.title_target is not None and plain_text(item["source"]).strip() == "Chapter 1"
                                else item["source"],
                            }
                            for item in included
                        ],
                    },
                    ensure_ascii=False,
                )
            }
        if stage == "coherence":
            if self.coherence_malformed_once:
                self.coherence_malformed_once = False
                return {"raw": 1}
            if self.coherence_request_failures:
                self.coherence_request_failures -= 1
                raise ProviderError("invalid coherence request", status_code=400)
            if self.coherence_pause_once:
                self.coherence_pause_once = False
                raise ProviderError("account paused", status_code=401)
            included = list(payload["items"])
            if self.coherence_always_empty:
                included = []
            elif self.coherence_empty_once:
                self.coherence_empty_once = False
                included = []
            elif self.coherence_omit_once and len(included) > 1:
                self.coherence_omit_once = False
                self.coherence_omitted = included.pop(1)["item_id"]
            blocking = self.coherence_major_once
            self.coherence_major_once = False
            response = {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-coherence-1",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "unit_ids": [item["unit_ids"][0]] if blocking else [],
                                "issues": [
                                    {
                                        "code": "continuity",
                                        "severity": "major",
                                        "message": "repair this transition",
                                    }
                                ]
                                if blocking
                                else [],
                            }
                            for item in included
                        ],
                    }
                )
            }
            if self.coherence_length_once:
                self.coherence_length_once = False
                response["finish_reason"] = "length"
            return response
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "base_revision": item["base_revision"],
                            "decision": "replace" if item.get("required_revision") else "no_change",
                            "checks": {
                                "accuracy": "pass",
                                "fluency": "pass",
                                "terminology": "pass" if item["applicability"]["terminology"] else "not_applicable",
                                "bindings": "pass" if item["applicability"]["bindings"] else "not_applicable",
                                "script": "pass",
                            },
                            "issues": [],
                            **({"target": item["target"]} if item.get("required_revision") else {}),
                        }
                        for item in payload["items"]
                    ],
                },
                ensure_ascii=False,
            )
        }


class TruncatedOnceTransport:
    def __init__(self, stage: str, *, input_tokens: int | None = None):
        self.stage = stage
        self.input_tokens = input_tokens
        self.truncated = False
        self.base = PartialBatchTransport()
        self.base.omitted = "disabled"

    @property
    def calls(self):
        return self.base.calls

    async def __call__(self, stage: str, payload: dict):
        response = await self.base(stage, payload)
        if self.input_tokens is not None:
            response = dict(response) | {"usage": {"input_tokens": self.input_tokens, "output_tokens": 1}}
            self.input_tokens = None
        if stage == self.stage and not self.truncated:
            self.truncated = True
            response = dict(response) | {"raw": "{", "finish_reason": "length"}
        return response


class ProjectionFailuresTransport(ScriptedTransport):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures

    async def __call__(self, stage: str, payload: dict):
        if stage == "translate" and self.failures:
            self.failures -= 1
            self.calls.append((stage, payload))
            item = payload["items"][0]
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-text-1",
                        "request_id": payload["request_id"],
                        "items": [{"item_id": item["item_id"], "target": "⟦unknown⟧"}],
                    }
                )
            }
        return await super().__call__(stage, payload)


class AlwaysTruncatedTransport(ScriptedTransport):
    async def __call__(self, stage: str, payload: dict):
        if stage == "translate":
            self.calls.append((stage, payload))
            return {"raw": "{", "finish_reason": "length"}
        return await super().__call__(stage, payload)


class NeedsAttentionOnceTransport(PartialBatchTransport):
    def __init__(self):
        super().__init__()
        self.omitted = "disabled"
        self.first_review = True
        self.review_payloads: list[dict] = []

    async def __call__(self, stage: str, payload: dict):
        if stage == "review":
            self.review_payloads.append(payload)
        if stage == "review" and self.first_review:
            self.first_review = False
            self.calls.append((stage, tuple(item["item_id"] for item in payload["items"])))
            return {
                "raw": json.dumps(
                    {
                        "protocol": "epubox-review-2",
                        "request_id": payload["request_id"],
                        "items": [
                            {
                                "item_id": item["item_id"],
                                "base_revision": item["base_revision"],
                                "decision": "needs_attention",
                                "checks": {
                                    "accuracy": "fail",
                                    "fluency": "pass",
                                    "terminology": "not_applicable",
                                    "bindings": "not_applicable",
                                    "script": "pass",
                                },
                                "issues": [{"code": "meaning", "severity": "major", "message": "repair meaning"}],
                            }
                            for item in payload["items"]
                        ],
                    }
                )
            }
        return await super().__call__(stage, payload)


class BatchNeedsAttentionTransport(PartialBatchTransport):
    async def __call__(self, stage: str, payload: dict):
        if stage != "review":
            return await super().__call__(stage, payload)
        self.calls.append((stage, tuple(item["item_id"] for item in payload["items"])))
        return {
            "raw": json.dumps(
                {
                    "protocol": "epubox-review-2",
                    "request_id": payload["request_id"],
                    "items": [
                        {
                            "item_id": item["item_id"],
                            "base_revision": item["base_revision"],
                            "decision": "needs_attention",
                            "checks": {
                                "accuracy": "fail",
                                "fluency": "pass",
                                "terminology": ("fail" if item["applicability"]["terminology"] else "not_applicable"),
                                "bindings": ("fail" if item["applicability"]["bindings"] else "not_applicable"),
                                "script": "pass",
                            },
                            "issues": [{"code": "meaning", "severity": "major", "message": "journal replacement"}],
                        }
                        for item in payload["items"]
                    ],
                }
            )
        }


def ready_store(tmp_path):
    store, unit = _frozen_store(tmp_path)
    record = store.save_unit(_unit_record(store, unit))
    store.write_bookplan(_bookplan(store, record))
    return store, unit, record


async def ready_batch_store(
    tmp_path,
    run_id: str = "batch-run",
    documents: dict[str, str] | None = None,
    tpm: int | None = None,
) -> RunStore:
    source = make_epub(
        tmp_path / f"{run_id}.epub",
        documents
        or {
            "chapter.xhtml": (
                "<div><p>First item.</p></div><div><p>Second item.</p></div><div><p>Third item.</p></div>"
            )
        },
    )
    prepared = await prepare_translation(
        source,
        tmp_path / f"work-{run_id}",
        PreparationConfig(
            run_id=run_id,
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 32_768,
                "max_output_tokens": 8192,
                "max_batch_items": 64,
                **({"tpm": tpm} if tpm is not None else {}),
            },
        ),
        StubChecker(),
    )
    return RunStore(prepared.work_dir)


async def ready_long_store(tmp_path, run_id: str) -> RunStore:
    source = make_epub(tmp_path / f"{run_id}.epub", {"chapter.xhtml": "<p>" + ("Long sentence. " * 100) + "</p>"})
    prepared = await prepare_translation(
        source,
        tmp_path / f"work-{run_id}",
        PreparationConfig(
            run_id=run_id,
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 4096,
                "max_output_tokens": 256,
                "review_output_tokens": 160,
                "max_batch_items": 2,
            },
        ),
        StubChecker(),
    )
    return RunStore(prepared.work_dir)


async def ready_derived_store(tmp_path, run_id: str = "derived-run") -> tuple[RunStore, dict]:
    source = make_epub(
        tmp_path / f"{run_id}.epub",
        {"chapter.xhtml": "<h1>Chapter 1</h1><p>Independent body.</p>"},
    )
    prepared = await prepare_translation(
        source,
        tmp_path / f"work-{run_id}",
        PreparationConfig(
            run_id=run_id,
            auto_extract=False,
            extraction_config={
                "auto_extract": False,
                "strategy": TERM_PLANNER_VERSION,
                "prompt_version": "epubox-v25-3",
                "model": "fake",
                "target_language": "zh-Hans",
            },
            translation_config={
                "target_language": "zh-Hans",
                "model": "fake",
                "context_tokens": 32_768,
                "max_output_tokens": 8192,
                "max_batch_items": 64,
            },
        ),
        StubChecker(),
    )
    store = RunStore(prepared.work_dir)
    preparation = store.read_preparation()
    binding = next(
        binding
        for document_id in preparation.document_hashes
        for binding in store.read_document(document_id).derived_bindings
        if binding.get("kind") == "derived_navigation"
    )
    return store, binding
