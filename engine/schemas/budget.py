"""Persistable contracts for request token budgets."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import Field, model_validator

from engine.schemas.base import FrozenModel

type BudgetStage = Literal["translate", "review"]
type ReviewTargets = Literal["actual", "estimated"]

BUDGET_VERSION = 2
MAX_CHUNK_TOKENS = 1500


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Provider and application limits applied to one request."""

    source_tokens: int
    input_tokens: int
    output_tokens: int | None
    context_tokens: int
    safety_tokens: int = 256
    target_ratio: float = 1.6
    output_version: Literal[2, 3, 4, 5, 6, 7] = 2
    minimum_source_tokens: int = 0
    source_tolerance_tokens: int = 0
    source_hard_limit: int | None = None
    context_unlimited: bool = False

    def __post_init__(self) -> None:
        if type(self.output_version) is not int or self.output_version not in (2, 3, 4, 5, 6, 7):
            raise ValueError("unsupported output budget version")
        integers = (self.source_tokens, self.input_tokens, self.context_tokens)
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("budget limits must be positive integers")
        if self.output_version == 7:
            if self.output_tokens is not None:
                raise ValueError("output budget version 7 must not define an output limit")
        elif type(self.output_tokens) is not int or self.output_tokens < 1:
            raise ValueError("budget limits must be positive integers")
        if type(self.safety_tokens) is not int or self.safety_tokens < 0:
            raise ValueError("budget safety must be a non-negative integer")
        if type(self.minimum_source_tokens) is not int or self.minimum_source_tokens < 0:
            raise ValueError("minimum source tokens must be a non-negative integer")
        if type(self.source_tolerance_tokens) is not int or not 0 <= self.source_tolerance_tokens <= 1000:
            raise ValueError("source tolerance must be between 0 and 1000 tokens")
        if self.source_hard_limit is not None and (
            type(self.source_hard_limit) is not int or not 0 < self.source_hard_limit <= MAX_CHUNK_TOKENS
        ):
            raise ValueError(f"source hard limit must be between 1 and {MAX_CHUNK_TOKENS} tokens")
        if type(self.context_unlimited) is not bool:
            raise TypeError("context_unlimited must be a boolean")
        if isinstance(self.target_ratio, bool) or not isinstance(self.target_ratio, (int, float)):
            raise TypeError("budget target ratio must be a number")
        if not math.isfinite(self.target_ratio) or self.target_ratio <= 0:
            raise ValueError("budget target ratio must be positive")

    def to_dict(self) -> dict[str, int | float | None]:
        values = asdict(self)
        if self.output_version == 2:
            values.pop("output_version")
        if not self.minimum_source_tokens:
            values.pop("minimum_source_tokens")
        if not self.source_tolerance_tokens:
            values.pop("source_tolerance_tokens")
        if self.source_hard_limit is None:
            values.pop("source_hard_limit")
        if not self.context_unlimited:
            values.pop("context_unlimited")
        return values

    @property
    def source_ceiling(self) -> int:
        nominal = self.source_tokens + self.source_tolerance_tokens
        return min(nominal, self.source_hard_limit) if self.source_hard_limit is not None else nominal


class BudgetIdentity(FrozenModel):
    """Policy identity required to validate a saved budget."""

    version: Literal[2, 3, 4, 5, 6, 7]
    tokenizer: str = Field(min_length=1)
    tokenizer_version: str = Field(min_length=1)
    tokenizer_model: str = Field(min_length=1)
    tokenizer_fallback: bool = Field(strict=True)
    strategy: Literal["cl100k+50pct+256"]
    margin_percent: Literal[50]
    wrapper_tokens: Literal[256]
    safety_tokens: int = Field(ge=0, strict=True)
    target_ratio: float = Field(gt=0, allow_inf_nan=False)
    source_limit: int = Field(gt=0, strict=True)
    source_hard_limit: int | None = Field(
        default=None, gt=0, le=MAX_CHUNK_TOKENS, strict=True, exclude_if=lambda value: value is None
    )
    input_limit: int = Field(gt=0, strict=True)
    output_limit: int | None = Field(gt=0, strict=True)
    context_limit: int = Field(gt=0, strict=True)
    context_unlimited: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def validate_output_limit(self) -> BudgetIdentity:
        if self.source_hard_limit is not None and self.source_limit > self.source_hard_limit:
            raise ValueError("source limit exceeds the saved hard limit")
        if self.version == 7:
            if self.output_limit is not None:
                raise ValueError("output budget version 7 must not define an output limit")
        elif self.output_limit is None:
            raise ValueError("legacy output budgets require an output limit")
        return self


class BudgetResult(FrozenModel):
    """Measured S/I/R/M budget bound to one complete payload."""

    stage: BudgetStage
    source_tokens: int = Field(ge=0, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    review_target_input_tokens: int = Field(ge=0, strict=True)
    input_reserve: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    context_tokens: int = Field(ge=0, strict=True)
    review_targets: ReviewTargets | None
    failures: tuple[str, ...]
    identity: BudgetIdentity
    wire_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def fits(self) -> bool:
        return not self.failures

    @model_validator(mode="after")
    def validate_measurement(self) -> BudgetResult:
        if self.stage == "translate":
            if self.review_targets is not None or self.review_target_input_tokens:
                raise ValueError("translate budget cannot contain review target estimates")
        elif self.review_targets is None:
            raise ValueError("review budget requires an actual or estimated target basis")
        elif self.review_targets == "actual" and self.review_target_input_tokens:
            raise ValueError("actual review budget cannot add an estimated target input")
        elif self.review_targets == "estimated" and not self.review_target_input_tokens:
            raise ValueError("estimated review budget requires target input reserve")

        expected_input = (
            self.input_tokens
            + math.ceil(self.input_tokens * self.identity.margin_percent / 100)
            + self.identity.wrapper_tokens
            + self.review_target_input_tokens
        )
        if self.input_reserve != expected_input:
            raise ValueError("input reserve does not match the saved budget policy")
        output_limit = self.identity.output_limit
        if self.identity.version in {3, 4, 5, 6} and output_limit is not None and self.output_tokens < output_limit:
            raise ValueError("output budget must reserve the configured provider cap")
        expected_context = self.input_reserve + self.output_tokens + self.identity.safety_tokens
        if self.context_tokens != expected_context:
            raise ValueError("context budget does not equal I + R + M")

        expected_failures: list[str] = []
        if self.source_tokens > self.identity.source_limit:
            expected_failures.append(f"source budget {self.source_tokens} exceeds {self.identity.source_limit}")
        if self.input_reserve > self.identity.input_limit:
            expected_failures.append(f"input budget {self.input_reserve} exceeds {self.identity.input_limit}")
        if output_limit is not None and self.output_tokens > output_limit:
            expected_failures.append(f"output budget {self.output_tokens} exceeds {output_limit}")
        context_gate = (
            self.input_reserve + self.identity.safety_tokens if self.identity.version == 7 else self.context_tokens
        )
        if not self.identity.context_unlimited and context_gate > self.identity.context_limit:
            expected_failures.append(f"context budget {context_gate} exceeds {self.identity.context_limit}")
        if self.failures != tuple(expected_failures):
            raise ValueError("budget failures do not match the measured limits")
        return self
