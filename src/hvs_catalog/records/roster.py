"""Immutable roster snapshots with separate validation and source facts."""

from typing import Literal

from pydantic import Field, model_validator

from .common import ArtifactRef, Config, Record, RecordRef
from .roster_input import RosterStar


class RosterIssue(Config):
    path: str
    message: str


class RosterData(Config):
    source_ref: RecordRef
    material_ref: ArtifactRef
    response_ref: ArtifactRef | None
    source_round: int | None = Field(ge=0, le=2)
    fallback: bool
    status: Literal["complete", "partial", "failed"]
    stars: list[RosterStar]
    notes: str | None
    source_indices: list[int]
    rejected: dict[str, list[RosterIssue]]
    issues: list[RosterIssue]

    @model_validator(mode="after")
    def _consistent(self):
        if (self.response_ref is None) != (self.source_round is None):
            raise ValueError("A source round requires its archived response.")
        if (
            len(self.source_indices) != len(self.stars)
            or self.source_indices != sorted(set(self.source_indices))
            or any(i < 0 for i in self.source_indices)
        ):
            raise ValueError("Star source indices must be unique and ordered")
        if self.status == "complete" and (
            self.notes is None
            or self.issues
            or self.rejected
            or self.fallback
            or self.response_ref is None
        ):
            raise ValueError("A complete roster must pass all checks")
        if self.status == "partial" and not self.stars:
            raise ValueError("A partial roster must contain a valid star")
        if self.status == "failed" and self.stars:
            raise ValueError("A failed roster cannot contain valid stars")
        return self


class RosterRecord(Record[RosterData]):
    RECORD_NAME = "roster_snapshot"
