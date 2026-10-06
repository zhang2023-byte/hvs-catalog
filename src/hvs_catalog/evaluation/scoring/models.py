"""Strict scoring inputs: plans, Gold, and predictions.

Reuse records.paper.MeasuredValue instead of inventing a second value structure. Objects
carry paper-local identifiers/explicit aliases, quantity_path, and values. Internal
value_id/record_id/labels/evidence references locate diagnostics only, never scientific
identity. Models serialize to JSON; score_batch reads no files or real services.
Recompute Gold/auxiliary fingerprints against actual input instead of trusting stale
supplied digests.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from hvs_catalog.records.common import SHA256_PATTERN, Config, sha256_of
from hvs_catalog.records.paper import MeasuredValue
from hvs_catalog.records.rules import is_quantity_path


def _check_quantity_path(path: str, where: str) -> None:
    if not is_quantity_path(path):
        raise ValueError(f"{where}的物理量编码未知：{path}")


class InputReference(Config):
    """Identity/fingerprint of plan-bound external materials, rules, schemes, programs, and
    other inputs.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    fingerprint: str = Field(pattern=SHA256_PATTERN)


class PaperEvaluationScope(Config):
    """Predeclared per-paper quantity scope and completeness assertions."""

    paper_key: str = Field(min_length=1)
    quantity_paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_and_unique(self) -> PaperEvaluationScope:
        if len(self.quantity_paths) != len(set(self.quantity_paths)):
            raise ValueError("评价范围内的物理量编码重复")
        for path in self.quantity_paths:
            _check_quantity_path(path, "评价范围")
        return self


class ScopedCoverage(Config):
    """Predeclared closed paper/object-identifier/quantity subset for partial Gold
    coverage.
    """

    paper_key: str = Field(min_length=1)
    object_identifiers: list[str] = Field(min_length=1)
    quantity_paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ScopedCoverage:
        if len(self.object_identifiers) != len(set(self.object_identifiers)):
            raise ValueError("封闭子范围的对象标识重复")
        if len(self.quantity_paths) != len(set(self.quantity_paths)):
            raise ValueError("封闭子范围的物理量编码重复")
        for path in self.quantity_paths:
            _check_quantity_path(path, "封闭子范围")
        return self


class LimitationScope(Config):
    """Predeclared scientific scope; labels and measured numbers never select it."""

    object_identifiers: list[str] = Field(min_length=1)
    quantity_path: str | None = None
    conditions: list[str] = Field(default_factory=list)
    attribution: dict[
        Literal[
            "source",
            "source_note",
            "frame",
            "epoch",
            "coordinate_format",
            "representation",
            "purpose",
            "author_usage",
        ],
        list[str],
    ] = Field(default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _consistent(self) -> LimitationScope:
        if any(not item.strip() for item in self.object_identifiers + self.conditions):
            raise ValueError("问题范围的标识与条件不能为空")
        for items in (self.object_identifiers, self.conditions):
            if len(items) != len(set(items)):
                raise ValueError("问题范围的标识或条件重复")
        if self.quantity_path is not None:
            _check_quantity_path(self.quantity_path, "问题范围")
        elif self.conditions or self.attribution:
            raise ValueError("条件和科学归属范围必须绑定具体物理量")
        for values in self.attribution.values():
            if not values or any(not value.strip() for value in values):
                raise ValueError("科学归属范围必须显式给出非空表达")
            if len(values) != len(set(values)):
                raise ValueError("科学归属范围表达重复")
        return self


class EvaluationLimitation(Config):
    """Reviewed potential error context, fixed before predictions are inspected."""

    limitation_id: str = Field(min_length=1)
    paper_key: str = Field(min_length=1)
    category: Literal[
        "author_issue",
        "unsupported_material",
        "external_out_of_scope",
    ]
    scope: LimitationScope
    basis: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    exclude_from_default: bool = False

    @model_validator(mode="after")
    def _allowed_exclusions(self) -> EvaluationLimitation:
        if self.exclude_from_default and self.category not in {
            "unsupported_material",
            "external_out_of_scope",
        }:
            raise ValueError("作者问题仍保留在评分范围")
        return self


class ScoringPlan(Config):
    """Scoring plan: ordered unique paper keys, predeclared scopes, and all input
    fingerprints.
    """

    paper_keys: list[str] = Field(min_length=1)
    scopes: list[PaperEvaluationScope] = Field(min_length=1)
    gold_ref: InputReference
    materials_ref: InputReference
    science_rules_ref: InputReference
    scoring_scheme_ref: InputReference
    program_ref: InputReference
    auxiliary_ref: InputReference
    comparison_mode: Literal["legacy_protocol", "numeric"] = Field(
        default="legacy_protocol", exclude_if=lambda value: value == "legacy_protocol"
    )
    readiness_gaps: dict[str, list[str]] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    input_snapshot: dict = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    scoped_coverages: list[ScopedCoverage] = Field(default_factory=list)
    known_evidence_record_ids: list[str] = Field(default_factory=list)
    limitations: list[EvaluationLimitation] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _consistent(self) -> ScoringPlan:
        if len(self.paper_keys) != len(set(self.paper_keys)):
            raise ValueError("计划中的 paper_key 必须有序且唯一")
        if set(self.readiness_gaps) - set(self.paper_keys):
            raise ValueError("就绪缺口引用了计划外论文")
        scope_keys = [scope.paper_key for scope in self.scopes]
        if sorted(scope_keys) != sorted(self.paper_keys):
            raise ValueError("评价范围声明必须与计划论文一一对应")
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("每篇论文只能有一份评价范围声明")
        scoped_keys = [coverage.paper_key for coverage in self.scoped_coverages]
        if len(scoped_keys) != len(set(scoped_keys)):
            raise ValueError("每篇论文只能预声明一个封闭子范围")
        unknown = [key for key in scoped_keys if key not in set(self.paper_keys)]
        if unknown:
            raise ValueError(f"封闭子范围引用了计划外论文：{unknown}")
        ids = [item.limitation_id for item in self.limitations]
        if len(ids) != len(set(ids)):
            raise ValueError("评分问题编号重复")
        scopes = {item.paper_key: set(item.quantity_paths) for item in self.scopes}
        for item in self.limitations:
            if item.paper_key not in scopes:
                raise ValueError("评分问题引用了计划外论文")
            path = item.scope.quantity_path
            if path is not None and path not in scopes[item.paper_key]:
                raise ValueError("评分问题的物理量超出计划范围")
        return self


# Allowed top-level fragment support domains match compared content field names.
FRAGMENT_SUPPORT_BASES = frozenset(
    {
        "value",
        "error",
        "lower_error",
        "upper_error",
        "range_lower",
        "range_upper",
        "unit",
        "condition",
        "coverage",
        "lower_quantile",
        "upper_quantile",
        "error_components",
    }
)


class EvidenceFragment(Config):
    """Locatable material-bound source fragment declaring supported fields in a scoring
    sidecar.

    Auxiliary judgments require a fixed material name/fingerprint, specific location,
    and explicit value/error/unit/condition or named-component support; prefixes cover
    subfields. value never supports error. Both sides need field-relevant fragments
    before calling; otherwise retain uncertainty without calls. This controlled sidecar
    does not prove actual source authenticity.
    """

    fragment_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    location: str = Field(min_length=1)
    material_name: str = Field(min_length=1)
    material_fingerprint: str = Field(pattern=SHA256_PATTERN)
    supports_fields: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_supports(self) -> EvidenceFragment:
        if len(self.supports_fields) != len(set(self.supports_fields)):
            raise ValueError("片段支持字段声明重复")
        for token in self.supports_fields:
            if not re.fullmatch(r"[a-z_]+(\.[A-Za-z0-9_\-]+)*", token):
                raise ValueError(f"片段支持字段声明非法：{token}")
            if token.split(".")[0] not in FRAGMENT_SUPPORT_BASES:
                raise ValueError(f"片段支持字段声明未知顶层域：{token}")
        return self

    def supports(self, field_name: str) -> bool:
        """Whether a support declaration covers the compared field, including
        prefix-matched subfields.
        """
        return any(
            token == field_name or field_name.startswith(token + ".")
            for token in self.supports_fields
        )


class GoldValueEntry(Config):
    record_id: str = Field(min_length=1)
    value: MeasuredValue
    context_evidence: list[str] = Field(default_factory=list)
    evidence_fragments: list[EvidenceFragment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_fragments(self) -> GoldValueEntry:
        ids = [fragment.fragment_id for fragment in self.evidence_fragments]
        if len(ids) != len(set(ids)):
            raise ValueError("同一数值的原文片段 fragment_id 重复")
        return self


class GoldQuantityEntry(Config):
    quantity_path: str = Field(min_length=1)
    values: list[GoldValueEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_quantity(self) -> GoldQuantityEntry:
        _check_quantity_path(self.quantity_path, "Gold")
        return self


class GoldObjectEntry(Config):
    label: str = Field(min_length=1)
    identifiers: list[str] = Field(min_length=1)
    contribution_type: str | None = None
    author_judgment: str | None = None
    quantities: list[GoldQuantityEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> GoldObjectEntry:
        if len(self.identifiers) != len(set(self.identifiers)):
            raise ValueError("同一 Gold 对象的标识/别名重复")
        paths = [group.quantity_path for group in self.quantities]
        if len(paths) != len(set(paths)):
            raise ValueError("同一 Gold 对象的物理量编码重复")
        return self


class ConfirmedRange(Config):
    """Confirmed complete object/quantity subset within partial Gold coverage."""

    object_identifiers: list[str] = Field(min_length=1)
    quantity_paths: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ConfirmedRange:
        if len(self.object_identifiers) != len(set(self.object_identifiers)):
            raise ValueError("确认子范围的对象标识重复")
        if len(self.quantity_paths) != len(set(self.quantity_paths)):
            raise ValueError("确认子范围的物理量编码重复")
        for path in self.quantity_paths:
            _check_quantity_path(path, "确认子范围")
        return self


class GoldPaperEntry(Config):
    paper_key: str = Field(min_length=1)
    coverage: Literal["complete", "partial"]
    confirmed_ranges: list[ConfirmedRange] = Field(default_factory=list)
    objects: list[GoldObjectEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_coverage(self) -> GoldPaperEntry:
        labels = [obj.label for obj in self.objects]
        if len(labels) != len(set(labels)):
            raise ValueError("论文内 Gold 对象 label 重复")
        record_ids = [
            entry.record_id
            for obj in self.objects
            for group in obj.quantities
            for entry in group.values
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("论文内 Gold 数值记录 record_id 重复")
        if self.coverage == "complete" and self.confirmed_ranges:
            raise ValueError("完整 Gold 不能声明确认子范围")
        if self.coverage == "partial" and not self.confirmed_ranges:
            raise ValueError("部分覆盖 Gold 必须声明已确认完整的子范围")
        return self


class PredictionValueEntry(Config):
    record_id: str = Field(min_length=1)
    value: MeasuredValue
    context_evidence: list[str] = Field(default_factory=list)
    evidence_fragments: list[EvidenceFragment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_fragments(self) -> PredictionValueEntry:
        ids = [fragment.fragment_id for fragment in self.evidence_fragments]
        if len(ids) != len(set(ids)):
            raise ValueError("同一数值的原文片段 fragment_id 重复")
        return self


class PredictionQuantityEntry(Config):
    quantity_path: str = Field(min_length=1)
    values: list[PredictionValueEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _known_quantity(self) -> PredictionQuantityEntry:
        _check_quantity_path(self.quantity_path, "预测")
        return self


class PredictionObjectEntry(Config):
    paper_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    identifiers: list[str] = Field(min_length=1)
    contribution_type: str | None = None
    author_judgment: str | None = None
    quantities: list[PredictionQuantityEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> PredictionObjectEntry:
        if len(self.identifiers) != len(set(self.identifiers)):
            raise ValueError("同一预测对象的标识重复")
        paths = [group.quantity_path for group in self.quantities]
        if len(paths) != len(set(paths)):
            raise ValueError("同一预测对象的物理量编码重复")
        return self


class PredictionPaperEntry(Config):
    """L0 delivery, format, completion, and errors with consistent derived state."""

    paper_key: str = Field(min_length=1)
    delivered: bool
    format_valid: bool
    completion: Literal["complete", "partial", "failed", "unfinished", "pending"]
    errors: list[str] = Field(default_factory=list)
    objects: list[PredictionObjectEntry] = Field(default_factory=list)
    execution_state: str | None = Field(default=None, exclude_if=lambda v: v is None)
    science_state: str | None = Field(default=None, exclude_if=lambda v: v is None)
    pending_questions: int | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _consistent_state(self) -> PredictionPaperEntry:
        labels = [obj.label for obj in self.objects]
        if len(labels) != len(set(labels)):
            raise ValueError("论文内预测对象 label 重复")
        record_ids = [
            entry.record_id
            for obj in self.objects
            for group in obj.quantities
            for entry in group.values
        ]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("论文内预测数值记录 record_id 重复")
        attributed = [obj.paper_key for obj in self.objects]
        if attributed and set(attributed) != {self.paper_key}:
            raise ValueError("预测对象的论文归属与所在预测论文不一致")
        if not self.delivered:
            if self.completion not in {"failed", "unfinished", "pending"}:
                raise ValueError("未交付论文的完成状态不能是 complete/partial")
            if self.format_valid:
                raise ValueError("未交付论文不能声明格式合格")
            if self.objects:
                raise ValueError("未交付论文不能携带可评价名单")
        else:
            if self.completion not in {"complete", "partial"}:
                raise ValueError("已交付论文的完成状态必须是 complete/partial")
            if not self.format_valid and self.objects:
                raise ValueError(
                    "格式不合格的预测不得携带可评价名单；保留原始诊断后置空名单"
                )
        return self


def gold_input_digest(entries: list[GoldPaperEntry]) -> str:
    """Canonical actual-Gold digest for checking plan fingerprints."""
    return sha256_of([entry.model_dump(mode="json") for entry in entries])


def predictions_input_digest(entries: list[PredictionPaperEntry]) -> str:
    """Canonical actual-prediction digest for report and auxiliary-request bindings."""
    return sha256_of([entry.model_dump(mode="json") for entry in entries])
