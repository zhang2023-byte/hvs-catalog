"""评分严格输入模型：计划、Gold、预测。

数值 payload 复用 ``records.paper.MeasuredValue``，评分侧不自造另一套
数值结构。输入对象携带论文内标识/明确别名、``quantity_path`` 和
``MeasuredValue``；所有内部 ID（``value_id``、``record_id``、对象
``label``、证据引用）只用于定位诊断，不参与科学身份匹配。

全部模型可 JSON 序列化；``score_batch`` 不读取文件、不调用真实服务。
计划中的 Gold/辅助配置指纹在评分时对实际输入重算核对，调用方提供的
过期指纹不被无条件信任。
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
    """计划绑定的外部输入（材料/规则/方案/程序等）的身份与指纹。"""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    fingerprint: str = Field(pattern=SHA256_PATTERN)


class PaperEvaluationScope(Config):
    """预声明的逐论文评价物理量范围与完整性声明。"""

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
    """预声明的封闭子范围（论文、对象标识、物理量），用于部分覆盖 Gold。"""

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


class ScoringPlan(Config):
    """评分计划：有序唯一 paper_key、预声明范围与全部输入指纹。"""

    paper_keys: list[str] = Field(min_length=1)
    scopes: list[PaperEvaluationScope] = Field(min_length=1)
    gold_ref: InputReference
    materials_ref: InputReference
    science_rules_ref: InputReference
    scoring_scheme_ref: InputReference
    program_ref: InputReference
    auxiliary_ref: InputReference
    scoped_coverages: list[ScopedCoverage] = Field(default_factory=list)
    known_evidence_record_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> ScoringPlan:
        if len(self.paper_keys) != len(set(self.paper_keys)):
            raise ValueError("计划中的 paper_key 必须有序且唯一")
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
        return self


# 片段可声明的支持字段顶层域：与内容比较的待判字段同名。
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
    """可定位、绑定材料、声明支持字段的原文片段（评分侧 sidecar）。

    供辅助评分 Agent 判定使用：片段必须指向固定材料（名称+指纹）中的
    具体位置，并显式声明它能支持的待判字段（``value``/``error``/
    ``unit``/``condition`` 及具名分量路径，前缀声明覆盖其子字段）。
    声明 ``value`` 的片段不能支持 ``error`` 的判定；只有双方都有与
    当前字段相关的片段时才允许发起请求，否则保持未知、不调用。
    该结构不证明片段确为真实原文，仅是受控评分 sidecar。
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
        """该片段是否支持给定待判字段（前缀声明覆盖子字段）。"""
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
    """部分覆盖 Gold 中已确认完整的对象/物理量子范围。"""

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
    """L0 输入：交付、格式、完成状态与错误；派生状态须一致。"""

    paper_key: str = Field(min_length=1)
    delivered: bool
    format_valid: bool
    completion: Literal["complete", "partial", "failed", "unfinished", "pending"]
    errors: list[str] = Field(default_factory=list)
    objects: list[PredictionObjectEntry] = Field(default_factory=list)

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
    """实际 Gold 输入的规范 JSON 摘要；用于核对计划指纹。"""
    return sha256_of([entry.model_dump(mode="json") for entry in entries])


def predictions_input_digest(entries: list[PredictionPaperEntry]) -> str:
    """实际预测输入的规范 JSON 摘要；进入报告与辅助请求绑定。"""
    return sha256_of([entry.model_dump(mode="json") for entry in entries])
