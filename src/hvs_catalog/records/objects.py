"""跨论文对象记录与外部资料记录。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import ArtifactRef, Config, Record, RecordRef

LinkState = Literal["confirmed", "pending", "rejected"]
ChangeKind = Literal["merge", "split", "revision"]


class ContributionLink(Config):
    target_ref: RecordRef
    item_id: str = Field(min_length=1)
    state: LinkState
    basis: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=0)
    decision_refs: list[RecordRef] = Field(min_length=0)


class ExternalLink(Config):
    target_ref: RecordRef
    entry_id: str = Field(min_length=1)
    state: LinkState
    basis: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=0)
    decision_refs: list[RecordRef] = Field(min_length=0)


class ObjectChange(Config):
    change_id: str = Field(min_length=1)
    kind: ChangeKind
    before_refs: list[RecordRef] = Field(min_length=0)
    after_refs: list[RecordRef] = Field(min_length=0)
    reason: str = Field(min_length=1)
    basis_refs: list[RecordRef] = Field(min_length=1)

    @model_validator(mode="after")
    def _refs_present(self) -> ObjectChange:
        if not self.before_refs and not self.after_refs:
            raise ValueError("合并、拆分或修订必须给出前后对象引用")
        return self


class ObjectData(Config):
    object_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=0)
    contribution_links: list[ContributionLink] = Field(min_length=0)
    external_links: list[ExternalLink] = Field(min_length=0)
    changes: list[ObjectChange] = Field(min_length=0)


class ObjectRecord(Record[ObjectData]):
    RECORD_NAME = "object"


class ExternalField(Config):
    name: str = Field(min_length=1)
    raw_value: str = Field(min_length=1)
    unit: str | None
    response_selector: str = Field(min_length=1)


class ExternalEntry(Config):
    entry_id: str = Field(min_length=1)
    fields: list[ExternalField] = Field(min_length=1)


class ExternalRecordData(Config):
    service: str = Field(min_length=1)
    catalog_version: str | None
    queried_at: datetime
    query: dict[str, object]
    raw_response: ArtifactRef
    entries: list[ExternalEntry] = Field(min_length=0)

    @model_validator(mode="after")
    def _timestamped(self) -> ExternalRecordData:
        if self.queried_at.tzinfo is None:
            raise ValueError("queried_at 必须带时区")
        return self


class ExternalRecord(Record[ExternalRecordData]):
    RECORD_NAME = "external_record"
