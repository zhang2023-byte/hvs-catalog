"""Shared record envelopes, references, and foundational definitions."""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC
from datetime import datetime
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RecordFormatError(ValueError):
    """Record violates shared structure, such as unknown type/version."""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def canonical_json(value: object) -> str:
    """Deterministic JSON for fingerprints, deduplication, and manifest hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class SchemaTag(Config):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class RecordRef(Config):
    record_id: str = Field(min_length=1)
    revision: int = Field(ge=1)


class ItemRef(Config):
    record_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    item_id: str = Field(min_length=1)


class ArtifactRef(Config):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def _path_within_declared_root(self) -> ArtifactRef:
        parts = self.path.replace("\\", "/").split("/")
        if self.path.startswith(("/", "~")) or any(
            part in {"", ".."} for part in parts
        ):
            raise ValueError(f"文件引用路径越界或不是相对路径：{self.path}")
        return self


class GapRef(Config):
    source_ref: RecordRef
    gap_id: str = Field(min_length=1)


DataT = TypeVar("DataT")


class Record(Config, ABC, Generic[DataT]):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    schema_tag: SchemaTag = Field(alias="schema")
    record_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    created_at: datetime
    execution_id: str | None
    data: DataT

    RECORD_NAME: ClassVar[str]
    RECORD_VERSION: ClassVar[int] = 1

    @model_validator(mode="after")
    def _check_envelope(self) -> Record[DataT]:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 必须带时区")
        if self.schema_tag.name != self.RECORD_NAME:
            raise RecordFormatError(
                f"记录类型不符：外层声明 {self.schema_tag.name}，实际模型 {self.RECORD_NAME}"
            )
        if self.schema_tag.version != self.RECORD_VERSION:
            raise RecordFormatError(
                f"未知记录格式版本：{self.schema_tag.name} v{self.schema_tag.version}"
                f"（当前支持 v{self.RECORD_VERSION}）"
            )
        return self

    def ref(self) -> RecordRef:
        return RecordRef(record_id=self.record_id, revision=self.revision)

    def item_ref(self, item_id: str) -> ItemRef:
        return ItemRef(
            record_id=self.record_id, revision=self.revision, item_id=item_id
        )


def build_record(
    model: type[Record[DataT]],
    *,
    record_id: str,
    created_at: datetime,
    execution_id: str | None,
    data: DataT,
    revision: int = 1,
) -> Record[DataT]:
    """Assemble envelopes from concrete models instead of manual field spelling."""
    return model(
        schema_tag=SchemaTag(name=model.RECORD_NAME, version=model.RECORD_VERSION),
        record_id=record_id,
        revision=revision,
        created_at=created_at,
        execution_id=execution_id,
        data=data,
    )


class PatchApplicationError(ValueError):
    """A retired decision patch cannot be applied."""
