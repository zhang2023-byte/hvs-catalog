"""Run/release manifest records."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from .common import ArtifactRef, Config, Record, RecordRef
from .paper import PrecedentSnapshotRef
from .questions import RulesRef

RunMode = Literal["daily", "evaluation", "synthetic"]
RunState = Literal[
    "created", "running", "waiting_review", "finished", "failed", "cancelled"
]
StepState = Literal[
    "created", "pending", "running", "waiting_review", "finished", "failed", "cancelled"
]
AttemptState = Literal["pending", "succeeded", "failed"]
ChannelState = Literal["not_started", "succeeded", "failed"]


def _omit_unfilled_money(data: dict) -> dict:
    """Omit absent currency-cost fields while preserving populated historical values.

    Runs no longer calculate/report/compare monetary fees. Historical cost/currency
    values remain readable/reserializable; omit new null fields, never fabricate zero.
    """
    if data.get("cost") is None:
        data.pop("cost", None)
    if data.get("currency") is None:
        data.pop("currency", None)
    return data


class Usage(Config):
    """Usage of a call or a whole run; unknown token counts stay null.

    ``cost``/``currency`` are omittable: runs no longer compute, fill or
    compare monetary costs; unfilled fields are not written to new record
    JSON. They remain readable for historical data with values — see the
    cost compatibility boundary in the architecture document.
    """

    tokens: int | None = Field(ge=0)
    cost: float | None = Field(default=None, ge=0)
    currency: str | None = None

    @model_serializer(mode="wrap")
    def _drop_unfilled_money(self, handler):
        return _omit_unfilled_money(handler(self))


class ModelSpec(Config):
    step_id: str | None = Field(
        description="Null for the whole-pipeline default model."
    )
    provider: str | None
    model: str = Field(min_length=1)
    parameters: dict[str, object]


class InputSnapshot(Config):
    source_refs: list[RecordRef] = Field(min_length=0)
    rules: RulesRef
    method: str | None
    models: list[ModelSpec] = Field(min_length=0)
    code_version: str = Field(min_length=1)
    dependency_versions: dict[str, str]
    expert_adjudication: bool
    prompt_refs: list[ArtifactRef] = Field(min_length=0)
    example_refs: list[ArtifactRef] = Field(min_length=0)
    precedent_refs: list[PrecedentSnapshotRef] = Field(min_length=0)
    existing_decision_refs: list[RecordRef] = Field(min_length=0)
    # Fixed engineering options, such as preparation, enter complete digests; extraction leaves them
    # empty. Values are strings; callers canonically serialize structured options.
    options: dict[str, str] = Field(default_factory=dict)


class Attempt(Config):
    request: ArtifactRef | None
    response: ArtifactRef | None
    state: AttemptState
    started_at: datetime | None
    finished_at: datetime | None
    usage: Usage | None
    cost: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Legacy monetary cost; new runs leave it unfilled and the "
            "serialized output omits it."
        ),
    )
    currency: str | None = None
    service_request_id: str | None

    @model_serializer(mode="wrap")
    def _drop_unfilled_money(self, handler):
        return _omit_unfilled_money(handler(self))

    @model_validator(mode="after")
    def _consistent(self) -> Attempt:
        if self.state == "succeeded" and self.response is None:
            raise ValueError("成功的调用必须保存响应引用")
        if self.started_at is not None and self.started_at.tzinfo is None:
            raise ValueError("started_at 必须带时区")
        if self.finished_at is not None and self.finished_at.tzinfo is None:
            raise ValueError("finished_at 必须带时区")
        return self


class StepRecord(Config):
    step_id: str = Field(min_length=1)
    state: StepState
    input_refs: list[RecordRef] = Field(min_length=0)
    attempts: list[Attempt] = Field(min_length=0)
    result_refs: list[RecordRef] = Field(min_length=0)
    error_refs: list[str] = Field(min_length=0)


class RunError(Config):
    error_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ModelJudgment(Config):
    step_id: str = Field(min_length=1)
    judgment: str = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def _timestamped(self) -> ModelJudgment:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 必须带时区")
        return self


class RunData(Config):
    execution_id: str = Field(min_length=1)
    mode: RunMode
    input_snapshot: InputSnapshot
    state: RunState
    steps: list[StepRecord] = Field(min_length=0)
    result_refs: list[RecordRef] = Field(min_length=0)
    model_judgments: list[ModelJudgment] = Field(min_length=0)
    usage: Usage
    errors: list[RunError] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent(self) -> RunData:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("步骤编号 step_id 重复")
        if self.state == "failed" and not self.errors:
            raise ValueError("失败执行必须记录运行错误")
        error_ids = [error.error_id for error in self.errors]
        if len(error_ids) != len(set(error_ids)):
            raise ValueError("运行错误编号重复")
        for step in self.steps:
            unknown = [
                error_id for error_id in step.error_refs if error_id not in error_ids
            ]
            if unknown:
                raise ValueError(f"步骤引用了不存在的运行错误：{unknown}")
        return self


class RunRecord(Record[RunData]):
    RECORD_NAME = "run"

    @model_validator(mode="after")
    def _execution_binding(self) -> RunRecord:
        if (
            self.execution_id is not None
            and self.data.execution_id != self.execution_id
        ):
            raise ValueError("运行清单的 execution_id 与外层不一致")
        return self


class Approval(Config):
    approver: str = Field(min_length=1)
    approved_at: datetime
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: str = Field(min_length=1)

    @model_validator(mode="after")
    def _timestamped(self) -> Approval:
        if self.approved_at.tzinfo is None:
            raise ValueError("approved_at 必须带时区")
        return self


class Channel(Config):
    target: str = Field(min_length=1)
    state: ChannelState
    receipt_ref: ArtifactRef | None
    error: str | None

    @model_validator(mode="after")
    def _state_grounded(self) -> Channel:
        if self.state == "succeeded" and self.receipt_ref is None:
            raise ValueError("渠道成功必须保存回执引用")
        if self.state == "failed" and not (self.error or "").strip():
            raise ValueError("渠道失败必须说明错误")
        return self


def compute_release_manifest_sha256(data: ReleaseData) -> str:
    """Manifest hashes cover approvable content/targets, excluding approval
    receipts/channel execution.
    """
    from .common import sha256_of

    return sha256_of(
        {
            "data_version": data.data_version,
            "content_refs": [ref.model_dump(mode="json") for ref in data.content_refs],
            "artifacts": [ref.model_dump(mode="json") for ref in data.artifacts],
            "source_run_refs": [
                ref.model_dump(mode="json") for ref in data.source_run_refs
            ],
            "code_version": data.code_version,
            "change_summary": data.change_summary,
            "channel_targets": [channel.target for channel in data.channels],
        }
    )


class ReleaseData(Config):
    data_version: str = Field(min_length=1)
    content_refs: list[RecordRef] = Field(min_length=0)
    artifacts: list[ArtifactRef] = Field(min_length=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run_refs: list[RecordRef] = Field(min_length=1)
    code_version: str = Field(min_length=1)
    change_summary: str = Field(min_length=1)
    approval: Approval | None = None
    channels: list[Channel] = Field(min_length=0)

    @model_validator(mode="after")
    def _manifest_and_approval_consistent(self) -> ReleaseData:
        if self.manifest_sha256 != compute_release_manifest_sha256(self):
            raise ValueError("清单哈希与实际待发布内容不一致，必须按当前内容重新计算")
        if self.approval is None:
            return self
        if self.approval.manifest_sha256 != self.manifest_sha256:
            raise ValueError("批准必须绑定当前清单哈希")
        if self.channels and self.approval.target not in {
            channel.target for channel in self.channels
        }:
            raise ValueError("批准目标必须是已声明的发布渠道之一")
        return self


class ReleaseRecord(Record[ReleaseData]):
    RECORD_NAME = "release"
