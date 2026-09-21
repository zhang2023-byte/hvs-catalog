"""论文记录与对象贡献：名单条目对应的研究摘要、判断、数值与完成情况。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    ArtifactRef,
    Config,
    GapRef,
    ItemRef,
    Record,
    RecordRef,
    canonical_json,
)
from .rules import AuthorStatus, is_quantity_path, quantity_paths

ContributionType = Literal["candidates_found", "follow_up"]
ValueSource = Literal["this_paper", "prior_work", "unclear"]
LimitKind = Literal["none", "lower_limit", "upper_limit", "range"]
IntervalKind = Literal[
    "ordinary_range",
    "prior_support",
    "confidence",
    "credible",
    "statistical_interval",
    "unspecified",
]
CoverageState = Literal["not_checked", "partial", "complete"]

TOP_LEVEL_PARTS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "range_lower",
    "range_upper",
)
COMPONENT_PARTS = ("error", "lower_error", "upper_error")


class DirectEvidencePart(Config):
    part: str = Field(min_length=1)
    evidence_ref: RecordRef
    raw_fragment: str = Field(
        min_length=1,
        description="The printed fragment of this component; must equal the saved "
        "numeric string of that component.",
    )


class CoverageReport(Config):
    value: str = Field(min_length=1)
    unit: str | None
    direct_evidence: list[DirectEvidencePart] = Field(min_length=1)

    @model_validator(mode="after")
    def _covers_value(self) -> CoverageReport:
        if not any(part.part == "value" for part in self.direct_evidence):
            raise ValueError("覆盖概率必须带有数值组件的直接证据")
        bad = [part.part for part in self.direct_evidence if part.part != "value"]
        if bad:
            raise ValueError(f"覆盖概率不存在的组件证据：{bad}")
        _check_printed_fragments(self.direct_evidence, "value", self.value, "覆盖概率")
        return self


class QuantileReport(Config):
    value: str = Field(min_length=1)
    unit: str | None
    direct_evidence: list[DirectEvidencePart] = Field(min_length=1)

    @model_validator(mode="after")
    def _covers_value(self) -> QuantileReport:
        if not any(part.part == "value" for part in self.direct_evidence):
            raise ValueError("分位点必须带有数值组件的直接证据")
        bad = [part.part for part in self.direct_evidence if part.part != "value"]
        if bad:
            raise ValueError(f"分位点不存在的组件证据：{bad}")
        _check_printed_fragments(self.direct_evidence, "value", self.value, "分位点")
        return self


def _error_form_allowed(
    error: str | None, lower_error: str | None, upper_error: str | None, where: str
) -> None:
    if error is not None and (lower_error is not None or upper_error is not None):
        raise ValueError(f"{where}不能同时使用对称误差和非对称误差")
    if error == "":
        raise ValueError(f"{where}的误差不能是空字符串，未报告应为 null")


def _check_printed_fragments(
    parts: list[DirectEvidencePart], part_name: str, number: str, where: str
) -> None:
    """数值组件的原报片段必须与保存的数字字符串完全一致。"""
    mismatched = [
        part.raw_fragment
        for part in parts
        if part.part == part_name and part.raw_fragment != number
    ]
    if mismatched:
        raise ValueError(
            f"{where}组件 {part_name} 的数值 {number!r} 与原报片段 {mismatched} 不一致"
        )


class ErrorComponent(Config):
    name: str = Field(min_length=1)
    error: str | None
    lower_error: str | None
    upper_error: str | None
    direct_evidence: list[DirectEvidencePart] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ErrorComponent:
        _error_form_allowed(
            self.error, self.lower_error, self.upper_error, f"误差分量 {self.name}"
        )
        filled = {part for part in COMPONENT_PARTS if getattr(self, part) is not None}
        covered = {part.part for part in self.direct_evidence}
        missing = filled - covered
        extra = covered - filled
        if missing:
            raise ValueError(
                f"误差分量 {self.name} 缺少数值组件证据：{sorted(missing)}"
            )
        if extra:
            raise ValueError(
                f"误差分量 {self.name} 引用了未填写的组件：{sorted(extra)}"
            )
        for part_name in COMPONENT_PARTS:
            number = getattr(self, part_name)
            if number is not None:
                _check_printed_fragments(
                    self.direct_evidence, part_name, number, f"误差分量 {self.name}"
                )
        return self


class MeasuredValue(Config):
    """一组科学数值的保存形式；所有键在保存记录中必须出现，未提供的内容为 null。"""

    value_id: str = Field(min_length=1)
    value: str | None = Field(
        description="Printed value string; null only for pure intervals."
    )
    error: str | None
    lower_error: str | None
    upper_error: str | None
    unit: str | None
    limit_kind: LimitKind
    range_lower: str | None
    range_upper: str | None
    lower_inclusive: bool | None
    upper_inclusive: bool | None
    interval_kind: IntervalKind | None
    coverage: CoverageReport | None = None
    lower_quantile: QuantileReport | None = None
    upper_quantile: QuantileReport | None = None
    error_components: list[ErrorComponent] = Field(min_length=0)
    condition: str = Field(min_length=1)
    frame: str | None
    epoch: str | None
    coordinate_format: str | None
    representation: str | None
    purpose: str | None
    source: ValueSource
    source_note: str | None
    author_usage: str | None
    direct_evidence: list[DirectEvidencePart] = Field(min_length=1)
    context_evidence_refs: list[RecordRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_value_form(self) -> MeasuredValue:
        _error_form_allowed(self.error, self.lower_error, self.upper_error, "数值")
        if self.limit_kind in {"none", "lower_limit", "upper_limit"}:
            if self.value is None:
                raise ValueError("点值和界限必须提供 value")
            if self.range_lower is not None or self.range_upper is not None:
                raise ValueError("非区间形式不能填写区间端点")
            if self.interval_kind is not None:
                raise ValueError("非纯区间不能填写区间解释")
        else:
            if self.value is not None:
                raise ValueError("纯区间不能补中心值，端点写入 range_lower/range_upper")
            if self.range_lower is None or self.range_upper is None:
                raise ValueError("纯区间必须提供两端端点")
            if self.interval_kind is None:
                raise ValueError("纯区间必须给出区间解释，原文未说明时用 unspecified")
        if self.limit_kind == "none" and (
            self.lower_inclusive is not None or self.upper_inclusive is not None
        ):
            raise ValueError("无数值界限时不能填写端点包含方式")
        filled = {part for part in TOP_LEVEL_PARTS if getattr(self, part) is not None}
        covered = {part.part for part in self.direct_evidence}
        missing = filled - covered
        extra = covered - filled
        if missing:
            raise ValueError(f"缺少数值组件的直接证据：{sorted(missing)}")
        if extra:
            raise ValueError(f"直接证据引用了未填写的组件：{sorted(extra)}")
        names = [component.name for component in self.error_components]
        if len(names) != len(set(names)):
            raise ValueError("误差分量名称重复")
        for part_name in TOP_LEVEL_PARTS:
            number = getattr(self, part_name)
            if number is not None:
                _check_printed_fragments(
                    self.direct_evidence, part_name, number, "数值"
                )
        if not self.condition.strip():
            raise ValueError("condition 必须是非空英文说明")
        return self


class IdentifierEntry(Config):
    value: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=1)


class ContributionSummary(Config):
    text: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=1)


class ContributionTypeEntry(Config):
    value: ContributionType
    evidence_refs: list[RecordRef] = Field(min_length=1)


class PaperBoundness(Config):
    status: AuthorStatus | None = None
    evidence_refs: list[RecordRef] = Field(min_length=0)
    issue_refs: list[RecordRef] = Field(min_length=0)
    gap_refs: list[GapRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _status_or_reason(self) -> PaperBoundness:
        if self.status is None and not self.issue_refs and not self.gap_refs:
            raise ValueError("尚不能判断作者状态时必须关联问题或材料缺口")
        if (
            self.status is not None
            and self.status != "no_overall_conclusion"
            and not self.evidence_refs
        ):
            raise ValueError("明确的作者判断必须带有原文证据")
        return self


class ConditionalAssessment(Config):
    condition: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=1)


class QuantityGroup(Config):
    quantity: str = Field(min_length=1)
    values: list[MeasuredValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_quantity(self) -> QuantityGroup:
        if not is_quantity_path(self.quantity):
            raise ValueError(f"未知的物理量编码：{self.quantity}")
        return self


# 没有科学顺序含义的证据集合：识别同一测量时不参与排列比较
_ORDER_INSENSITIVE_LISTS = ("direct_evidence", "context_evidence_refs")
_REPORT_LISTS = ("coverage", "lower_quantile", "upper_quantile")


def _sorted_evidence(items: object) -> object:
    if isinstance(items, list):
        return sorted(items, key=canonical_json)
    return items


def _expression_dump(value: MeasuredValue) -> dict:
    """除技术编号外的完整数值表达；嵌套证据集合先规范化再排序上层集合。"""
    data = value.model_dump(mode="json", exclude={"value_id"})
    for key in _ORDER_INSENSITIVE_LISTS:
        data[key] = _sorted_evidence(data.get(key))
    for report_key in _REPORT_LISTS:
        report = data.get(report_key)
        if isinstance(report, dict):
            report["direct_evidence"] = _sorted_evidence(report.get("direct_evidence"))
    for component in data.get("error_components") or []:
        if isinstance(component, dict):
            component["direct_evidence"] = _sorted_evidence(
                component.get("direct_evidence")
            )
    data["error_components"] = _sorted_evidence(data.get("error_components"))
    return data


def _expression_key(value: MeasuredValue) -> str:
    """除技术编号与证据排列顺序外的完整数值表达键，用于识别同一待决测量。"""
    return canonical_json(_expression_dump(value))


def find_conflict_exclusivity_violation(quantity_conflicts, quantities) -> str | None:
    """返回冲突待决测量混入已确认数值的说明；无违例时返回 None。"""
    pending: dict[str, set[str]] = {}
    for conflict in quantity_conflicts:
        keys = pending.setdefault(conflict.quantity, set())
        keys.update(_expression_key(value) for value in conflict.alternatives)
    for group in quantities:
        banned = pending.get(group.quantity)
        if not banned:
            continue
        for value in group.values:
            if _expression_key(value) in banned:
                return f"{group.quantity} 的已确认数值 {value.value_id}"
    return None


class QuantityConflict(Config):
    conflict_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    alternatives: list[MeasuredValue] = Field(min_length=2)
    issue_ref: RecordRef

    @model_validator(mode="after")
    def _distinct_alternatives(self) -> QuantityConflict:
        if not is_quantity_path(self.quantity):
            raise ValueError(f"未知的物理量编码：{self.quantity}")
        expressions = [_expression_key(value) for value in self.alternatives]
        if any(
            expressions[i] == expressions[j]
            for i in range(len(expressions))
            for j in range(i + 1, len(expressions))
        ):
            raise ValueError("冲突双方必须至少有两个不同的完整数值表达")
        return self


class QuantityCoverageEntry(Config):
    quantity: str = Field(min_length=1)
    state: CoverageState
    checked_file_ids: list[str] = Field(min_length=0)
    gap_refs: list[GapRef] = Field(min_length=0)
    issue_refs: list[RecordRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_state(self) -> QuantityCoverageEntry:
        if not is_quantity_path(self.quantity):
            raise ValueError(f"未知的物理量编码：{self.quantity}")
        if self.state in {"partial", "complete"} and not self.checked_file_ids:
            raise ValueError("声明已检查或部分检查时必须列出已检查文件")
        if self.state == "not_checked" and self.checked_file_ids:
            raise ValueError("未检查的物理量不能声明已检查文件")
        if self.state == "complete" and self.gap_refs:
            raise ValueError("完成声明不能关联未处理的材料缺口")
        return self


class PrecedentApproval(Config):
    approved_by: str = Field(min_length=1)
    approved_at: datetime


class PrecedentSnapshotRef(Config):
    precedent_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    text: ArtifactRef
    approval: PrecedentApproval


class PrecedentUse(Config):
    precedent_ref: PrecedentSnapshotRef
    applies_to: list[ItemRef] = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ObjectContribution(Config):
    item_id: str = Field(min_length=1)
    roster_item_id: str = Field(min_length=1)
    identifiers: list[IdentifierEntry] = Field(min_length=1)
    contribution_summary: ContributionSummary
    contribution_type: ContributionTypeEntry
    paper_boundness: PaperBoundness
    conditional_assessments: list[ConditionalAssessment] = Field(min_length=0)
    quantities: list[QuantityGroup] = Field(min_length=0)
    quantity_conflicts: list[QuantityConflict] = Field(min_length=0)
    quantity_coverage: list[QuantityCoverageEntry]
    precedent_uses: list[PrecedentUse] = Field(min_length=0)

    @model_validator(mode="after")
    def _consistent_contribution(self) -> ObjectContribution:
        names = [identifier.value for identifier in self.identifiers]
        if len(names) != len(set(names)):
            raise ValueError("同一贡献内名称重复，同一名称只保存一次")
        groups = [group.quantity for group in self.quantities]
        if len(groups) != len(set(groups)):
            raise ValueError("同一种物理量只能占一组")
        value_ids = [
            value.value_id for group in self.quantities for value in group.values
        ] + [
            value.value_id
            for conflict in self.quantity_conflicts
            for value in conflict.alternatives
        ]
        if len(value_ids) != len(set(value_ids)):
            raise ValueError("value_id 在贡献内必须唯一")
        violation = find_conflict_exclusivity_violation(
            self.quantity_conflicts, self.quantities
        )
        if violation is not None:
            raise ValueError(f"冲突中的待决测量不能同时作为已确认数值保存：{violation}")
        expected = quantity_paths()
        covered = [entry.quantity for entry in self.quantity_coverage]
        if sorted(covered) != sorted(expected):
            missing = sorted(set(expected) - set(covered))
            extra = sorted(set(covered) - set(expected))
            raise ValueError(
                f"完成情况必须恰好覆盖 {len(expected)} 种物理量一次；缺 {missing}，多 {extra}"
            )
        return self


class PaperMetadata(Config):
    arxiv_id: str | None
    version: str = Field(min_length=1)
    title: str | None
    authors: list[str] = Field(min_length=0)


class PaperData(Config):
    paper_key: str = Field(min_length=1)
    metadata: PaperMetadata
    source_ref: RecordRef
    roster_ref: RecordRef
    object_contributions: list[ObjectContribution] = Field(min_length=0)

    @model_validator(mode="after")
    def _unique_items(self) -> PaperData:
        item_ids = [c.item_id for c in self.object_contributions]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("论文内贡献编号 item_id 重复")
        roster_ids = [c.roster_item_id for c in self.object_contributions]
        if len(roster_ids) != len(set(roster_ids)):
            raise ValueError("论文内名单条目编号 roster_item_id 重复")
        return self


class PaperRecord(Record[PaperData]):
    RECORD_NAME = "paper"
