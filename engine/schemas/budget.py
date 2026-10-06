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


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Provider and application limits applied to one request."""

    source_tokens: int
    input_tokens: int
    output_tokens: int
    context_tokens: int
    safety_tokens: int = 256
    target_ratio: float = 1.6
    output_version: Literal[2, 3] = 2

    def __post_init__(self) -> None:
        if type(self.output_version) is not int or self.output_version not in (2, 3):
            raise ValueError("unsupported output budget version")
        integers = (self.source_tokens, self.input_tokens, self.output_tokens, self.context_tokens)
        if any(type(value) is not int or value < 1 for value in integers):
            raise ValueError("budget limits must be positive integers")
        if type(self.safety_tokens) is not int or self.safety_tokens < 0:
            raise ValueError("budget safety must be a non-negative integer")
        if isinstance(self.target_ratio, bool) or not isinstance(self.target_ratio, (int, float)):
            raise TypeError("budget target ratio must be a number")
        if not math.isfinite(self.target_ratio) or self.target_ratio <= 0:
            raise ValueError("budget target ratio must be positive")

    def to_dict(self) -> dict[str, int | float]:
        values = asdict(self)
        if self.output_version == 2:
            values.pop("output_version")
        return values


class BudgetIdentity(FrozenModel):
    """Policy identity required to validate a saved budget."""

    version: Literal[2, 3]
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
    input_limit: int = Field(gt=0, strict=True)
    output_limit: int = Field(gt=0, strict=True)
    context_limit: int = Field(gt=0, strict=True)


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
        if self.identity.version == 3 and self.output_tokens < self.identity.output_limit:
            raise ValueError("output budget must reserve the configured provider cap")
        expected_context = self.input_reserve + self.output_tokens + self.identity.safety_tokens
        if self.context_tokens != expected_context:
            raise ValueError("context budget does not equal I + R + M")

        expected_failures: list[str] = []
        if self.source_tokens > self.identity.source_limit:
            expected_failures.append(f"source budget {self.source_tokens} exceeds {self.identity.source_limit}")
        if self.input_reserve > self.identity.input_limit:
            expected_failures.append(f"input budget {self.input_reserve} exceeds {self.identity.input_limit}")
        if self.output_tokens > self.identity.output_limit:
            expected_failures.append(f"output budget {self.output_tokens} exceeds {self.identity.output_limit}")
        if self.context_tokens > self.identity.context_limit:
            expected_failures.append(f"context budget {self.context_tokens} exceeds {self.identity.context_limit}")
        if self.failures != tuple(expected_failures):
            raise ValueError("budget failures do not match the measured limits")
        return self
