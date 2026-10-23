"""A numerical snapshot binds retained values to one fixed roster entry."""

from typing import Literal

from pydantic import Field, model_validator

from .common import ArtifactRef, Config, Record, RecordRef
from .literature_contribution import LiteratureQuantity
from .roster import RosterIssue


class QuantityData(Config):
    source_ref: RecordRef
    material_ref: ArtifactRef
    roster_ref: RecordRef
    source_index: int = Field(ge=0)
    response_ref: ArtifactRef | None
    source_round: int | None = Field(ge=0, le=2)
    fallback: bool
    status: Literal["complete", "partial", "failed"]
    quantities: list[LiteratureQuantity]
    notes: str | None
    source_paths: list[str]
    rejected: dict[str, list[RosterIssue]]
    issues: list[RosterIssue]

    @model_validator(mode="after")
    def _consistent(self):
        if (self.response_ref is None) != (self.source_round is None):
            raise ValueError("A source round requires its archived response.")
        if len(self.source_paths) != sum(len(q.values) for q in self.quantities) or len(
            set(self.source_paths)
        ) != len(self.source_paths):
            raise ValueError("Each retained value must have one distinct source path.")
        if len({q.quantity for q in self.quantities}) != len(self.quantities):
            raise ValueError("Submit one group for each quantity.")
        if self.status == "complete" and (
            self.notes is None
            or self.issues
            or self.rejected
            or self.fallback
            or self.response_ref is None
        ):
            raise ValueError("A complete snapshot must pass all checks.")
        if self.status == "partial" and not self.quantities:
            raise ValueError("A partial snapshot must retain a value.")
        if self.status == "failed" and self.quantities:
            raise ValueError("A failed snapshot cannot retain values.")
        return self


class QuantityRecord(Record[QuantityData]):
    RECORD_NAME = "quantity_snapshot"
