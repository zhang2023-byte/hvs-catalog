"""合成输入的构造帮助：为 ``MeasuredValue`` 补齐证据样板。

这些函数只填充直接证据样板，不绕过 ``records.paper.MeasuredValue``
的任何校验，也不引入评分逻辑；供固定样例、测试和集成侧数据适配
复用。所有材料均为合成（SYN-* / synthetic-*），不涉及真实论文。
"""

from __future__ import annotations

from typing import Any

from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import (
    CoverageReport,
    DirectEvidencePart,
    ErrorComponent,
    MeasuredValue,
    QuantileReport,
)

COMPONENT_PARTS = ("error", "lower_error", "upper_error")


def _part(part: str, number: str, evidence_ref: RecordRef) -> DirectEvidencePart:
    return DirectEvidencePart(part=part, evidence_ref=evidence_ref, raw_fragment=number)


def _report(report: tuple[str, str | None] | None, evidence_ref: RecordRef):
    if report is None:
        return None
    value, unit = report
    return CoverageReport(
        value=value,
        unit=unit,
        direct_evidence=[_part("value", value, evidence_ref)],
    )


def build_measured_value(
    *,
    value_id: str,
    condition: str,
    value: str | None = None,
    error: str | None = None,
    lower_error: str | None = None,
    upper_error: str | None = None,
    unit: str | None = None,
    limit_kind: str = "none",
    range_lower: str | None = None,
    range_upper: str | None = None,
    lower_inclusive: bool | None = None,
    upper_inclusive: bool | None = None,
    interval_kind: str | None = None,
    coverage: tuple[str, str | None] | None = None,
    lower_quantile: tuple[str, str | None] | None = None,
    upper_quantile: tuple[str, str | None] | None = None,
    error_components: list[dict[str, Any]] | None = None,
    source: str = "this_paper",
    source_note: str | None = None,
    frame: str | None = None,
    epoch: str | None = None,
    coordinate_format: str | None = None,
    representation: str | None = None,
    purpose: str | None = None,
    author_usage: str | None = None,
    evidence_record_id: str = "synthetic-source",
    evidence_revision: int = 1,
) -> MeasuredValue:
    """构造一条完整校验的合成 ``MeasuredValue``；证据样板自动生成。"""
    evidence_ref = RecordRef(record_id=evidence_record_id, revision=evidence_revision)
    direct: list[DirectEvidencePart] = []
    for part_name, raw in (
        ("value", value),
        ("error", error),
        ("lower_error", lower_error),
        ("upper_error", upper_error),
        ("range_lower", range_lower),
        ("range_upper", range_upper),
    ):
        if raw is not None:
            direct.append(_part(part_name, raw, evidence_ref))
    components: list[ErrorComponent] = []
    for spec in error_components or []:
        name = spec["name"]
        parts = [
            _part(part_name, spec[part_name], evidence_ref)
            for part_name in COMPONENT_PARTS
            if spec.get(part_name) is not None
        ]
        components.append(
            ErrorComponent(
                name=name,
                error=spec.get("error"),
                lower_error=spec.get("lower_error"),
                upper_error=spec.get("upper_error"),
                direct_evidence=parts,
            )
        )
    return MeasuredValue(
        value_id=value_id,
        value=value,
        error=error,
        lower_error=lower_error,
        upper_error=upper_error,
        unit=unit,
        limit_kind=limit_kind,
        range_lower=range_lower,
        range_upper=range_upper,
        lower_inclusive=lower_inclusive,
        upper_inclusive=upper_inclusive,
        interval_kind=interval_kind,
        coverage=_report(coverage, evidence_ref),
        lower_quantile=(
            QuantileReport(
                value=lower_quantile[0],
                unit=lower_quantile[1],
                direct_evidence=[_part("value", lower_quantile[0], evidence_ref)],
            )
            if lower_quantile is not None
            else None
        ),
        upper_quantile=(
            QuantileReport(
                value=upper_quantile[0],
                unit=upper_quantile[1],
                direct_evidence=[_part("value", upper_quantile[0], evidence_ref)],
            )
            if upper_quantile is not None
            else None
        ),
        error_components=components,
        condition=condition,
        frame=frame,
        epoch=epoch,
        coordinate_format=coordinate_format,
        representation=representation,
        purpose=purpose,
        source=source,
        source_note=source_note,
        author_usage=author_usage,
        direct_evidence=direct,
        context_evidence_refs=[],
    )
