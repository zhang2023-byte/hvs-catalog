"""Strict requests shared by ordinary/evaluation extraction.

Accept typed mode/method/material references/default-step models/review switches and
fixed allowed decisions. No Gold/scoring/answer paths or free metadata. extra='forbid'
rejects unknown fields and answer smuggling.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from ..records.common import Config, RecordRef
from ..records.runs import RunMode


class StepModel(Config):
    """Step model configuration; step_id=None is the unique global fallback."""

    step_id: str | None = None
    provider: str | None = None
    model: str = Field(min_length=1)
    parameters: dict[str, object] = Field(default_factory=dict)


class ExtractionRequest(Config):
    """Shared execution request.

    source_refs identifies fixed workspace packages; models requires exactly one default
    plus optional overrides. Registered step existence is checked before running.
    existing_decision_refs fixes applicable reusable decisions under architecture's
    current-round disabled-group isolation rule.
    """

    mode: RunMode
    method: str = Field(min_length=1)
    source_refs: list[RecordRef] = Field(min_length=1)
    models: list[StepModel] = Field(min_length=1)
    expert_adjudication: bool = True
    existing_decision_refs: list[RecordRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _single_default_and_unique_steps(self) -> ExtractionRequest:
        defaults = [item for item in self.models if item.step_id is None]
        if len(defaults) != 1:
            raise ValueError(
                "models 必须恰好包含一个 step_id=None 的全局默认模型"
                f"（唯一 fallback）；当前有 {len(defaults)} 个"
            )
        step_ids = [item.step_id for item in self.models if item.step_id is not None]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("同一步骤不能重复指定模型")
        return self

    def model_for(self, step_id: str) -> StepModel:
        """Resolve step models, using global fallback when no override exists."""
        default = next(item for item in self.models if item.step_id is None)
        return next((item for item in self.models if item.step_id == step_id), default)
