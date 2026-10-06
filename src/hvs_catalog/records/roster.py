"""Rosters: objects, methods, material scope, and unlisted source ranges."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Config, GapRef, Record, RecordRef
from .paper import AuthorError, CoverageState

# Old values remain readable in preserved historical records only.
RosterMethod = Literal["roster", "direct", "candidate_review"]
Disposition = Literal["included", "excluded", "pending"]


class RosterCoverage(Config):
    state: CoverageState
    checked_file_ids: list[str] = Field(min_length=0)
    gap_refs: list[GapRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_state(self) -> RosterCoverage:
        if self.state in {"partial", "complete"} and not self.checked_file_ids:
            raise ValueError("声明已检查或部分检查时必须列出已检查文件")
        if self.state == "not_checked" and self.checked_file_ids:
            raise ValueError("未检查的名单不能声明已检查文件")
        if self.state == "complete" and self.gap_refs:
            raise ValueError("完成声明不能关联未处理的材料缺口")
        return self


class RosterIdentifier(Config):
    value: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=1)


class RosterEntry(Config):
    item_id: str = Field(min_length=1)
    identifiers: list[RosterIdentifier] = Field(min_length=1)
    disposition: Disposition
    evidence_refs: list[RecordRef] = Field(min_length=0)
    issue_refs: list[RecordRef] = Field(min_length=0)
    contribution_id: str | None = None
    gap_refs: list[GapRef] = Field(default_factory=list)
    author_errors: list[AuthorError] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disposition_consistent(self) -> RosterEntry:
        if self.disposition == "included":
            if self.contribution_id is None:
                raise ValueError("收录条目必须对应本篇贡献")
            if not self.evidence_refs:
                raise ValueError("收录条目必须带有原文证据")
        elif self.disposition == "excluded":
            if self.contribution_id is not None:
                raise ValueError("排除条目不能伪造已收录贡献")
            if not self.evidence_refs:
                raise ValueError("排除条目必须带有依据")
        else:
            if self.contribution_id is not None:
                raise ValueError("待决条目不能伪造已收录贡献")
            if not self.issue_refs and not self.gap_refs and not self.author_errors:
                raise ValueError("待决条目必须关联未决问题或材料缺口")
        if self.disposition != "pending" and self.author_errors:
            raise ValueError("影响身份或收录的原文错误必须隔离为未完成条目")
        return self


class UnresolvedGroup(Config):
    group_id: str = Field(min_length=1)
    original_range: str = Field(
        min_length=1, description="The printed range expression, preserved as is."
    )
    evidence_refs: list[RecordRef] = Field(min_length=1)
    known_member_item_ids: list[str] = Field(min_length=0)
    reason: str = Field(min_length=1)


class RosterData(Config):
    paper_key: str = Field(min_length=1)
    source_ref: RecordRef
    method: RosterMethod
    coverage: RosterCoverage
    entries: list[RosterEntry] = Field(min_length=0)
    unresolved_groups: list[UnresolvedGroup] = Field(min_length=0)
    issue_refs: list[RecordRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_ids(self) -> RosterData:
        item_ids = [entry.item_id for entry in self.entries]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("名单条目编号 item_id 重复")
        known = set(item_ids)
        for group in self.unresolved_groups:
            unknown = [
                item for item in group.known_member_item_ids if item not in known
            ]
            if unknown:
                raise ValueError(f"未列清范围引用了不存在的名单条目：{unknown}")
        contribution_ids = [
            entry.contribution_id for entry in self.entries if entry.contribution_id
        ]
        if len(contribution_ids) != len(set(contribution_ids)):
            raise ValueError("名单条目对应的贡献编号重复")
        return self


class RosterRecord(Record[RosterData]):
    RECORD_NAME = "roster"
