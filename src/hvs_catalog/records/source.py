"""Source-package, material-gap, and source-evidence formats."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Config, Record, RecordRef

ReadState = Literal["ready", "unparsed", "unreadable"]
GapReason = Literal["missing", "unreadable", "unparsed", "external_not_included"]


class SyntheticOrigin(Config):
    kind: Literal["synthetic"]


class ArxivOrigin(Config):
    kind: Literal["arxiv"]
    arxiv_id: str = Field(min_length=1)
    retrieval: str = Field(
        min_length=1,
        description="How and when the source package was retrieved, e.g. the download basis.",
    )


SourceOrigin = Annotated[SyntheticOrigin | ArxivOrigin, Field(discriminator="kind")]


class SourceFile(Config):
    file_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1)
    role: str = Field(min_length=1)
    read_state: ReadState
    derived_from: list[str]

    @model_validator(mode="after")
    def _relative_path(self) -> SourceFile:
        parts = self.path.replace("\\", "/").split("/")
        if self.path.startswith(("/", "~")) or any(
            part in {"", ".."} for part in parts
        ):
            raise ValueError(f"包内文件路径越界或不是相对路径：{self.path}")
        return self


RosterJudgment = Literal["inclusion", "identity"]


class AffectedItem(Config):
    item_id: str | None = Field(
        description="Contribution item affected by the gap, when identifiable."
    )
    quantity: str | None = Field(
        description="Quantity path affected by the gap, when the scope names one."
    )
    judgment: RosterJudgment | None = Field(
        description="Roster-level judgment the gap affects: inclusion (whether the "
        "object belongs to the roster) or identity (whether printed names denote "
        "one object). Mutually exclusive with quantity in one item; null when the "
        "item names quantities only."
    )

    @model_validator(mode="after")
    def _names_target(self) -> AffectedItem:
        if self.item_id is None and self.quantity is None and self.judgment is None:
            raise ValueError("影响范围条目必须指明对象、物理量或收录/身份判断")
        if self.quantity is not None and self.judgment is not None:
            raise ValueError("影响范围条目不能同时指明物理量和收录/身份判断")
        return self


class AffectedTargets(Config):
    scope: Literal["known", "unknown"]
    items: list[AffectedItem] = Field(min_length=0)

    @model_validator(mode="after")
    def _scope_matches_items(self) -> AffectedTargets:
        if self.scope == "known" and not self.items:
            raise ValueError("范围已知时必须列出受影响对象、物理量或判断")
        if self.scope == "unknown" and self.items:
            raise ValueError("范围未知时不能列出具体影响条目")
        return self


class Gap(Config):
    gap_id: str = Field(min_length=1)
    reason: GapReason
    description: str = Field(min_length=1)
    file_id: str | None = Field(
        description="Omitted for gaps without a specific package file."
    )
    mention_evidence_refs: list[RecordRef] = Field(min_length=0)
    affected_targets: AffectedTargets


def gap_affects(gap: Gap, *, item_id: str | None, quantity: str | None) -> bool:
    """Check declared gap impact for objects/quantities.

    Unknown scope may affect all targets. In known scope, missing item_id means all
    objects for a quantity, and missing quantity means the whole object.
    Inclusion/identity entries do not affect quantity completion.
    """
    if gap.affected_targets.scope == "unknown":
        return True
    return any(
        target.judgment is None
        and (target.item_id is None or target.item_id == item_id)
        and (target.quantity is None or target.quantity == quantity)
        for target in gap.affected_targets.items
    )


def gaps_affecting_quantity(
    gaps: Iterable[Gap], item_id: str, quantity: str
) -> list[str]:
    """List gaps affecting an object's quantity."""
    return [
        gap.gap_id
        for gap in gaps
        if gap_affects(gap, item_id=item_id, quantity=quantity)
    ]


def gaps_affecting_roster(gaps: Iterable[Gap]) -> list[str]:
    """List roster inclusion/identity gaps.

    Unknown scope may change any roster conclusion. Known gaps affect rosters only
    through explicit inclusion/identity declarations; quantity-only gaps do not.
    """
    affecting: list[str] = []
    for gap in gaps:
        if gap.affected_targets.scope == "unknown" or any(
            item.judgment is not None for item in gap.affected_targets.items
        ):
            affecting.append(gap.gap_id)
    return affecting


class SourcePackageData(Config):
    paper_key: str = Field(min_length=1)
    origin: SourceOrigin
    source_version: str = Field(min_length=1)
    files: list[SourceFile] = Field(min_length=0)
    gaps: list[Gap] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_ids(self) -> SourcePackageData:
        file_ids = [f.file_id for f in self.files]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("来源包内 file_id 重复")
        known = set(file_ids)
        for file in self.files:
            unknown = [item for item in file.derived_from if item not in known]
            if unknown:
                raise ValueError(f"derived_from 引用了不存在的文件：{unknown}")
        gap_ids = [g.gap_id for g in self.gaps]
        if len(gap_ids) != len(set(gap_ids)):
            raise ValueError("来源包内 gap_id 重复")
        for gap in self.gaps:
            if gap.file_id is not None and gap.file_id not in known:
                raise ValueError(f"缺口引用了不存在的文件：{gap.file_id}")
        return self


class TextLocator(Config):
    kind: Literal["text"]
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered_lines(self) -> TextLocator:
        if self.end_line < self.start_line:
            raise ValueError("end_line 不能小于 start_line")
        return self


class TableCellLocator(Config):
    kind: Literal["table_cell"]
    row_key: dict[str, object] = Field(min_length=1)
    column: str = Field(min_length=1)
    row_number: int | None = Field(ge=1)


class DataElementSelector(Config):
    kind: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @model_validator(mode="after")
    def _known_selector(self) -> DataElementSelector:
        if self.kind == "json_pointer" and not self.value.startswith("/"):
            raise ValueError("JSON Pointer 定位必须以 / 开头")
        return self


class DataElementDecoder(Config):
    name: str = Field(min_length=1)
    encoding: str | None


class DataElementLocator(Config):
    kind: Literal["data_element"]
    selector: DataElementSelector
    decoder: DataElementDecoder


Locator = Annotated[
    TextLocator | TableCellLocator | DataElementLocator, Field(discriminator="kind")
]


class EvidenceData(Config):
    paper_key: str = Field(min_length=1)
    source_ref: RecordRef
    file_id: str = Field(min_length=1)
    locator: Locator
    excerpt: str = Field(min_length=1)


class SourcePackageRecord(Record[SourcePackageData]):
    RECORD_NAME = "source_package"


class EvidenceRecord(Record[EvidenceData]):
    RECORD_NAME = "evidence"
