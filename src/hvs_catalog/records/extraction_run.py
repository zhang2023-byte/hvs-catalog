"""The public extraction run summary contains facts and exact archive references."""

from typing import Literal

from pydantic import AwareDatetime, Field

from .common import ArtifactRef, Config, RecordRef

ResultStatus = Literal["complete", "partial", "failed"]


class RunStatus(Config):
    execution: str
    result: ResultStatus | None
    resumable: bool


class RunTimestamps(Config):
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None


class StageSummary(Config):
    state: str
    status: ResultStatus | None
    snapshot_ref: RecordRef | None
    source_execution: str | None
    rounds_completed: int = Field(ge=0)
    source_round: int | None = Field(ge=0, le=2)
    fallback: bool | None
    notes_available: bool | None
    source_index: int | None = None


class RunStages(Config):
    roster: StageSummary
    quantities: list[StageSummary]


class RunUsage(Config):
    attempts: int = Field(ge=0)
    known_tokens: int = Field(ge=0)
    total_tokens: int | None = Field(ge=0)


class ExtractionRun(Config):
    format: Literal["hvs.extraction-run"] = "hvs.extraction-run"
    version: Literal[1] = 1
    execution_id: str
    scope: Literal["full", "roster"]
    status: RunStatus
    timestamps: RunTimestamps
    input_ref: ArtifactRef
    stages: RunStages
    usage: RunUsage
    errors: list[dict]
    trace_ref: ArtifactRef
    result_ref: ArtifactRef | None
    evidence_audit_ref: ArtifactRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
