"""固定的旧输入与草稿/新答案的逐项比较：只读，不自动修订答案。

报告包含所比较内容的哈希，逐项给出一致、调整、缺失与新增；比较不判
断科学对错，只陈述两侧内容差异。旧条目按草稿记录的 ``legacy_index``
对应，全新草稿（无旧输入对应）不适用本接口。

含义相同的表达按以下约定正规化后比较：旧空字符串与新格式 null 视为
同一含义；证据集合、误差分量集合不参与排列比较（排序后比较）；证据
的 PDF 文件绑定（``file_id``）属于新格式的格式级信息，不参与内容比
较（迁移报告已单独记录）。其余字段——包括科学状态、上下文证据、嵌
套证据、冲突结构与页序号——全部参与比较；新增内容（如专家填写的
新词表状态、页序号）计为差异项，须由专家审定，不由本工具判断对错。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..records.common import Config, canonical_json
from .errors import GoldFormatError
from .legacy import LegacyAnnotation, read_legacy_annotation
from .models import (
    GoldAnnotation,
    GoldContribution,
    GoldExclusion,
    read_gold_annotation,
)


class CompareItem(Config):
    subject: str
    status: str
    description: str


class CompareReport(Config):
    legacy_file: str
    legacy_sha256: str
    current_file: str
    current_sha256: str
    items: list[CompareItem]
    matched_count: int
    difference_count: int
    unmatched_old: list[str]
    unmatched_new: list[str]
    mapping_file: str | None = None
    mapping_sha256: str | None = None


_EMPTY = ("", None)

_NUMERIC_FIELDS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "unit",
    "limit_kind",
    "range_lower",
    "range_upper",
    "coordinate_format",
    "condition",
    "source",
    "source_note",
    "interval_kind",
    "representation",
)


def _norm(text: object) -> object:
    """旧空字符串与新格式 null 视为同一含义：未提供。"""
    return None if text in _EMPTY else text


def _evidence_entry(data: dict) -> dict:
    """证据的参与比较内容：位置、引文与页序号（不含 file_id）。"""
    return {
        "location": data["location"],
        "quote": _norm(data.get("quote")),
        "page": data.get("page"),
    }


def _evidence_set(entries: list[dict]) -> list[dict]:
    return sorted((_evidence_entry(entry) for entry in entries), key=canonical_json)


def _value_dump(value) -> dict:
    """数值的完整规范化表达：全部科学字段、嵌套证据与区间元数据。"""
    data = value.model_dump(mode="json")
    dump: dict[str, object] = {key: _norm(data.get(key)) for key in _NUMERIC_FIELDS}
    dump["evidence"] = _evidence_set(data.get("evidence", []))
    dump["context_evidence"] = _evidence_set(data.get("context_evidence", []))
    dump["error_components"] = sorted(
        (
            {
                "name": component["name"],
                "error": _norm(component.get("error")),
                "lower_error": _norm(component.get("lower_error")),
                "upper_error": _norm(component.get("upper_error")),
                "evidence": _evidence_set(component.get("evidence", [])),
            }
            for component in data.get("error_components", [])
        ),
        key=canonical_json,
    )
    for report_name in ("coverage", "lower_quantile", "upper_quantile"):
        report = data.get(report_name)
        dump[report_name] = (
            None
            if report is None
            else {
                "value": report.get("value"),
                "unit": report.get("unit"),
                "evidence": _evidence_set(report.get("evidence", [])),
            }
        )
    return dump


def _value_signature(value) -> str:
    return canonical_json(_value_dump(value))


def _value_brief(dump: dict) -> str:
    """数值差异报告中的一行摘要：科学字段与全部证据位置。"""
    parts: list[str] = []
    for key in (
        "value",
        "error",
        "lower_error",
        "upper_error",
        "range_lower",
        "range_upper",
        "unit",
        "condition",
    ):
        item = dump.get(key)
        if item is not None:
            parts.append(f"{key}={item}")
    if dump.get("interval_kind") is not None:
        parts.append(f"interval={dump['interval_kind']}")
    locations = ";".join(entry["location"] for entry in dump.get("evidence", []))
    context = ";".join(entry["location"] for entry in dump.get("context_evidence", []))
    if context:
        parts.append(f"证据[{locations or '无'}；上下文[{context}]]")
    else:
        parts.append(f"证据[{locations or '无'}]")
    return ", ".join(parts)


def _quantity_differences(
    old_contribution, new_contribution: GoldContribution, subject: str
) -> list[CompareItem]:
    items: list[CompareItem] = []
    old_groups = {group.quantity: group for group in old_contribution.quantities}
    new_groups = {group.quantity: group for group in new_contribution.quantities}
    for quantity in sorted(old_groups.keys() | new_groups.keys()):
        where = f"{subject}/quantities/{quantity}"
        old_group = old_groups.get(quantity)
        new_group = new_groups.get(quantity)
        if old_group is None:
            items.append(
                CompareItem(
                    subject=where,
                    status="added",
                    description=f"新答案新增物理量 {quantity}（旧输入没有）",
                )
            )
            continue
        if new_group is None:
            items.append(
                CompareItem(
                    subject=where,
                    status="removed",
                    description=f"物理量 {quantity} 在新答案中缺失",
                )
            )
            continue
        old_dumps = sorted(
            (_value_dump(value) for value in old_group.values),
            key=canonical_json,
        )
        new_dumps = sorted(
            (_value_dump(value) for value in new_group.values),
            key=canonical_json,
        )
        if [canonical_json(item) for item in old_dumps] == [
            canonical_json(item) for item in new_dumps
        ]:
            items.append(
                CompareItem(
                    subject=where,
                    status="identical",
                    description=f"物理量 {quantity} 的 {len(old_dumps)} 个数值"
                    "逐字一致（含证据位置、引文与嵌套结构）",
                )
            )
            continue
        old_keys = [canonical_json(item) for item in old_dumps]
        new_keys = [canonical_json(item) for item in new_dumps]
        missing = [
            _value_brief(dump)
            for dump, key in zip(old_dumps, old_keys, strict=False)
            if key not in new_keys
        ]
        added = [
            _value_brief(dump)
            for dump, key in zip(new_dumps, new_keys, strict=False)
            if key not in old_keys
        ]
        details = []
        if missing:
            details.append(
                "旧侧有而新侧没有（含被改动后与旧值不再一致的原条目）："
                + "；".join(missing)
            )
        if added:
            details.append("新侧有而旧侧没有（含改动后的新条目）：" + "；".join(added))
        items.append(
            CompareItem(
                subject=where,
                status="different",
                description=(
                    f"物理量 {quantity} 数值集合不同（旧 {len(old_dumps)} 项、"
                    f"新 {len(new_dumps)} 项）。"
                    + "。".join(details)
                    + "。差异报告不判断科学对错，改动须经专家审定"
                ),
            )
        )
    return items


def _boundness_item(
    old_contribution, new_contribution: GoldContribution
) -> CompareItem:
    """作者总体状态单独比较：旧状态逐字保留情况与新词表状态的填写。"""
    subject = f"contributions/{new_contribution.contribution_id}/paper_boundness"
    old_status = old_contribution.paper_boundness.status
    boundness = new_contribution.paper_boundness
    legacy_kept = boundness.legacy_status == old_status
    old_evidence = _evidence_set(
        [
            {"location": entry.location, "quote": entry.quote, "page": None}
            for entry in old_contribution.paper_boundness.evidence
        ]
    )
    new_evidence = _evidence_set(
        [entry.model_dump(mode="json") for entry in boundness.evidence]
    )
    evidence_kept = old_evidence == new_evidence
    facts = (
        f"旧状态 {old_status!r}；新答案 legacy_status={boundness.legacy_status!r}、"
        f"新词表状态 {boundness.status!r}"
    )
    if legacy_kept and boundness.status is None and evidence_kept:
        return CompareItem(
            subject=subject,
            status="identical",
            description=(f"{facts}；旧状态逐字保留、新词表状态待定，证据逐字一致"),
        )
    changes = []
    if not legacy_kept:
        changes.append("旧状态未被逐字保留（legacy_status 与旧输入不同）")
    if boundness.status is not None:
        changes.append(
            f"填写了新词表状态 {boundness.status!r}（旧输入没有对应字段，"
            "属于新增科学判断，须经专家审定）"
        )
    if not evidence_kept:
        changes.append("证据位置、引文或页序号存在差异")
    return CompareItem(
        subject=subject,
        status="different",
        description=f"{facts}；" + "；".join(changes),
    )


def _header_item(old_contribution, new_contribution: GoldContribution) -> CompareItem:
    subject = f"contributions/{new_contribution.contribution_id}/header"
    old_names = sorted(identifier.value for identifier in old_contribution.identifiers)
    new_names = sorted(identifier.value for identifier in new_contribution.identifiers)
    old_identifier_evidence = sorted(
        (
            _evidence_set(
                [
                    {"location": entry.location, "quote": entry.quote, "page": None}
                    for entry in identifier.evidence
                ]
            )
            for identifier in old_contribution.identifiers
        ),
        key=canonical_json,
    )
    new_identifier_evidence = sorted(
        (
            _evidence_set(
                [entry.model_dump(mode="json") for entry in identifier.evidence]
            )
            for identifier in new_contribution.identifiers
        ),
        key=canonical_json,
    )
    identical_names = old_names == new_names
    identical_identifier_evidence = old_identifier_evidence == new_identifier_evidence
    identical_summary = (
        old_contribution.contribution_summary == new_contribution.contribution_summary
    )
    identical_type = (
        old_contribution.contribution_type == new_contribution.contribution_type
    )
    identical = (
        identical_names
        and identical_identifier_evidence
        and identical_summary
        and identical_type
    )
    if identical:
        description = "名称、名称证据、贡献类型与摘要逐字一致"
    else:
        changes = []
        if not identical_names:
            changes.append(f"名称集合不同：旧 {old_names}，新 {new_names}")
        if not identical_identifier_evidence:
            changes.append("名称证据的位置、引文或页序号存在差异")
        if not identical_type:
            changes.append(
                f"贡献类型不同：旧 {old_contribution.contribution_type!r}，"
                f"新 {new_contribution.contribution_type!r}"
            )
        if not identical_summary:
            changes.append("研究摘要被改动")
        description = "；".join(changes)
    return CompareItem(
        subject=subject,
        status="identical" if identical else "different",
        description=description,
    )


def _conflict_items(
    old_contribution, new_contribution: GoldContribution, subject: str
) -> list[CompareItem]:
    where = f"{subject}/quantity_conflicts"
    old_conflicts = list(old_contribution.quantity_conflicts)
    new_conflicts = list(new_contribution.quantity_conflicts)
    if not old_conflicts and not new_conflicts:
        return []
    items: list[CompareItem] = []
    differences: list[str] = []
    for index, (old, new) in enumerate(zip(old_conflicts, new_conflicts, strict=False)):
        old_evidence = _evidence_set(
            [
                {"location": entry.location, "quote": entry.quote, "page": None}
                for entry in old.evidence
            ]
        )
        new_evidence = _evidence_set(
            [entry.model_dump(mode="json") for entry in new.evidence]
        )
        entry_changes = []
        if old.quantity != new.quantity:
            entry_changes.append(f"物理量不同（旧 {old.quantity}，新 {new.quantity}）")
        if old.reason != new.reason:
            entry_changes.append("冲突原因被改动")
        if old_evidence != new_evidence:
            entry_changes.append("证据位置、引文或页序号存在差异")
        if new.alternatives:
            entry_changes.append(
                f"新答案补充了双方完整数值结构（{len(new.alternatives)} 项备选，"
                "旧格式没有对应字段，须经专家审定）"
            )
        if entry_changes:
            differences.append(
                f"第 {index + 1} 项（{new.conflict_id}）：" + "；".join(entry_changes)
            )
    if len(old_conflicts) > len(new_conflicts):
        differences.append(
            f"新答案缺少 {len(old_conflicts) - len(new_conflicts)} 项旧冲突"
        )
    if len(new_conflicts) > len(old_conflicts):
        differences.append(
            f"新答案新增 {len(new_conflicts) - len(old_conflicts)} 项冲突"
            "（旧输入没有，须经专家审定）"
        )
    items.append(
        CompareItem(
            subject=where,
            status="different" if differences else "identical",
            description=(
                f"冲突 {len(new_conflicts)} 项（旧 {len(old_conflicts)} 项）："
                + (
                    "逐项内容与证据逐字一致"
                    if not differences
                    else "；".join(differences)
                )
            ),
        )
    )
    return items


def _exclusions_item(legacy: LegacyAnnotation, current: GoldAnnotation) -> CompareItem:
    old_exclusions = list(legacy.reviewed_exclusions)
    new_exclusions = list(current.reviewed_exclusions)
    differences: list[str] = []
    for index, (old, new) in enumerate(
        zip(old_exclusions, new_exclusions, strict=False)
    ):
        old_evidence = _evidence_set(
            [
                {"location": entry.location, "quote": entry.quote, "page": None}
                for entry in old.evidence
            ]
        )
        new_evidence = _evidence_set(
            [entry.model_dump(mode="json") for entry in new.evidence]
        )
        if old.reason != new.reason:
            differences.append(f"第 {index + 1} 项排除原因被改动")
        if old_evidence != new_evidence:
            differences.append(f"第 {index + 1} 项证据位置、引文或页序号存在差异")
    if len(old_exclusions) > len(new_exclusions):
        differences.append(
            f"新答案缺少 {len(old_exclusions) - len(new_exclusions)} 条排除记录"
        )
    if len(new_exclusions) > len(old_exclusions):
        differences.append(
            f"新答案新增 {len(new_exclusions) - len(old_exclusions)} 条排除记录"
        )
    return CompareItem(
        subject="reviewed_exclusions",
        status="different" if differences else "identical",
        description=(
            f"排除记录旧 {len(old_exclusions)} 条、新 {len(new_exclusions)} 条："
            + ("原因与证据逐字一致" if not differences else "；".join(differences))
        ),
    )


def _document_items(
    legacy: LegacyAnnotation, current: GoldAnnotation
) -> list[CompareItem]:
    items: list[CompareItem] = []
    status_identical = legacy.status == current.status
    items.append(
        CompareItem(
            subject="status",
            status="identical" if status_identical else "different",
            description=(
                f"文档结论旧 {legacy.status!r}、新 {current.status!r}："
                + ("逐字保留" if status_identical else "结论被改动，须经专家审定")
            ),
        )
    )
    legacy_arxiv = (
        current.provenance.legacy.arxiv_id
        if current.provenance.legacy is not None
        else None
    )
    paper_identical = (
        current.paper.arxiv_id == legacy.arxiv_id
        and current.paper.arxiv_id == legacy_arxiv
    )
    items.append(
        CompareItem(
            subject="paper",
            status="identical" if paper_identical else "different",
            description=(
                f"论文标识：旧输入 {legacy.arxiv_id!r}、迁移来源 "
                f"{legacy_arxiv!r}、新答案 {current.paper.arxiv_id!r}"
                + ("；一致" if paper_identical else "；存在差异，须经专家审定")
            ),
        )
    )
    return items


def compare_legacy_with_current(
    *,
    legacy_path: str | Path,
    current_path: str | Path,
    mapping_path: str | Path | None = None,
) -> CompareReport:
    """比较旧输入与草稿/新答案；两侧文件都只读。"""
    legacy_path = Path(legacy_path)
    current_path = Path(current_path)
    legacy_raw = legacy_path.read_text(encoding="utf-8")
    current_raw = current_path.read_text(encoding="utf-8")
    legacy: LegacyAnnotation = read_legacy_annotation(legacy_path)
    if (
        current_path.name == "answer.json"
        and (current_path.parent / "metadata.json").is_file()
    ):
        if mapping_path is None:
            raise GoldFormatError(
                "Comparing a compact v2 answer with v4 requires an explicit --mapping "
                "JSON file containing legacy_index_by_contribution_id; cand order is not a v4 index"
            )
        return _compare_legacy_with_compact(
            legacy=legacy,
            legacy_path=legacy_path,
            legacy_raw=legacy_raw,
            current_path=current_path,
            current_raw=current_raw,
            mapping_path=Path(mapping_path),
        )
    current: GoldAnnotation = read_gold_annotation(current_path)
    legacy_sha = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()
    current_sha = hashlib.sha256(current_raw.encode("utf-8")).hexdigest()

    items: list[CompareItem] = _document_items(legacy, current)
    by_legacy_index = {}
    unmatched_new = []
    for contribution in current.contributions:
        index = contribution.legacy_index
        if index is None:
            unmatched_new.append(contribution.contribution_id)
        elif index >= len(legacy.contributions) or index in by_legacy_index:
            # Invalid mappings must not overwrite or hide an entire contribution.
            unmatched_new.append(contribution.contribution_id)
            items.append(
                CompareItem(
                    subject=f"contributions/{contribution.contribution_id}/legacy_index",
                    status="different",
                    description=f"旧索引 {index} 越界或重复；此贡献未建立唯一对应",
                )
            )
        else:
            by_legacy_index[index] = contribution
    unmatched_old: list[str] = []

    items.append(_exclusions_item(legacy, current))

    for index, old_contribution in enumerate(legacy.contributions):
        if index not in by_legacy_index:
            unmatched_old.append(f"contributions[{index}]")
            continue
        new_contribution = by_legacy_index[index]
        items.append(_header_item(old_contribution, new_contribution))
        items.append(_boundness_item(old_contribution, new_contribution))
        items.extend(
            _conflict_items(
                old_contribution,
                new_contribution,
                f"contributions/{new_contribution.contribution_id}",
            )
        )
        items.extend(
            _quantity_differences(
                old_contribution,
                new_contribution,
                f"contributions/{new_contribution.contribution_id}",
            )
        )

    matched = sum(1 for item in items if item.status == "identical")
    differences = [item for item in items if item.status != "identical"]
    return CompareReport(
        legacy_file=legacy_path.name,
        legacy_sha256=legacy_sha,
        current_file=current_path.name,
        current_sha256=current_sha,
        items=items,
        matched_count=matched,
        difference_count=len(differences),
        unmatched_old=unmatched_old,
        unmatched_new=unmatched_new,
    )


def _compact_as_comparable(contribution, old_status: str) -> GoldContribution:
    """Adapt v2 science in memory; the legacy status is comparison context only."""
    return GoldContribution.model_validate(
        {
            "contribution_id": contribution.contribution_id,
            "identifiers": [
                item.model_dump(mode="python") for item in contribution.identifiers
            ],
            "contribution_type": contribution.contribution_type.value,
            "contribution_summary": contribution.contribution_summary.value,
            "contribution_evidence": [
                item.model_dump(mode="python")
                for item in contribution.contribution_summary.evidence
            ],
            "paper_boundness": {
                "status": contribution.paper_boundness.value,
                "legacy_status": old_status,
                "evidence": [
                    item.model_dump(mode="python")
                    for item in contribution.paper_boundness.evidence
                ],
            },
            "quantities": [
                item.model_dump(mode="python") for item in contribution.quantities
            ],
            "quantity_conflicts": [
                {
                    "conflict_id": item.conflict_id,
                    "quantity": item.quantity,
                    "reason": item.reason.value,
                    "evidence": [
                        e.model_dump(mode="python") for e in item.reason.evidence
                    ],
                    "alternatives": [
                        v.model_dump(mode="python") for v in item.alternatives
                    ],
                }
                for item in contribution.quantity_conflicts
            ],
        }
    )


def _compact_boundness_item(old, new) -> CompareItem:
    subject = f"contributions/{new.contribution_id}/paper_boundness"
    old_evidence = _evidence_set(
        [
            {"location": entry.location, "quote": entry.quote, "page": None}
            for entry in old.paper_boundness.evidence
        ]
    )
    new_evidence = _evidence_set(
        [item.model_dump(mode="json") for item in new.paper_boundness.evidence]
    )
    same = (
        old.paper_boundness.status == new.paper_boundness.value
        and old_evidence == new_evidence
    )
    return CompareItem(
        subject=subject,
        status="identical" if same else "different",
        description=(
            f"v4 status {old.paper_boundness.status!r}; current value "
            f"{new.paper_boundness.value!r}; evidence "
            + ("matches" if old_evidence == new_evidence else "differs")
            + (
                "; status matches"
                if old.paper_boundness.status == new.paper_boundness.value
                else "; status changed under current rules"
            )
        ),
    )


def _compare_legacy_with_compact(
    *,
    legacy: LegacyAnnotation,
    legacy_path: Path,
    legacy_raw: str,
    current_path: Path,
    current_raw: str,
    mapping_path: Path,
) -> CompareReport:
    from .compact import read_compact_gold_pair

    pair = read_compact_gold_pair(current_path)
    mapping_raw = mapping_path.read_text(encoding="utf-8")
    try:
        mapping_document = json.loads(mapping_raw)
    except json.JSONDecodeError as exc:
        raise GoldFormatError(f"Invalid v2 comparison mapping JSON: {exc}") from exc
    mapping = (
        mapping_document.get("legacy_index_by_contribution_id")
        if isinstance(mapping_document, dict)
        else None
    )
    if not isinstance(mapping, dict):
        raise GoldFormatError(
            "v2 comparison mapping must contain legacy_index_by_contribution_id"
        )
    current_ids = {item.contribution_id for item in pair.answer.contributions}
    if set(mapping) != current_ids:
        raise GoldFormatError(
            f"v2 comparison mapping must cover every current ID exactly: missing={sorted(current_ids - set(mapping))}, extra={sorted(set(mapping) - current_ids)}"
        )
    indices = [value for value in mapping.values() if value is not None]
    if any(
        type(value) is not int or value < 0 or value >= len(legacy.contributions)
        for value in indices
    ):
        raise GoldFormatError("v2 comparison mapping has an invalid v4 index")
    if len(indices) != len(set(indices)):
        raise GoldFormatError("v2 comparison mapping repeats a v4 index")
    items: list[CompareItem] = []
    status_same = legacy.status == pair.answer.status
    items.append(
        CompareItem(
            subject="status",
            status="identical" if status_same else "different",
            description=f"v4 status {legacy.status!r}; current status {pair.answer.status!r}",
        )
    )
    paper_same = legacy.arxiv_id == pair.answer.arxiv_id == pair.metadata.paper.arxiv_id
    items.append(
        CompareItem(
            subject="paper",
            status="identical" if paper_same else "different",
            description=f"v4 arXiv ID {legacy.arxiv_id!r}; current arXiv ID {pair.answer.arxiv_id!r}",
        )
    )
    from types import SimpleNamespace

    comparable_exclusions = SimpleNamespace(
        reviewed_exclusions=[
            GoldExclusion.model_validate(
                {
                    "exclusion_id": item.exclusion_id,
                    "reason": item.reason.value,
                    "evidence": [
                        e.model_dump(mode="python") for e in item.reason.evidence
                    ],
                }
            )
            for item in pair.answer.reviewed_exclusions
        ]
    )
    items.append(_exclusions_item(legacy, comparable_exclusions))
    by_id = {item.contribution_id: item for item in pair.answer.contributions}
    reverse = {
        index: identifier for identifier, index in mapping.items() if index is not None
    }
    unmatched_new = sorted(
        identifier for identifier, index in mapping.items() if index is None
    )
    unmatched_old: list[str] = []
    for index, old in enumerate(legacy.contributions):
        identifier = reverse.get(index)
        if identifier is None:
            unmatched_old.append(f"contributions[{index}]")
            continue
        new = by_id[identifier]
        comparable = _compact_as_comparable(new, old.paper_boundness.status)
        items.append(_header_item(old, comparable))
        items.append(_compact_boundness_item(old, new))
        items.extend(_conflict_items(old, comparable, f"contributions/{identifier}"))
        items.extend(
            _quantity_differences(old, comparable, f"contributions/{identifier}")
        )
    matched = sum(item.status == "identical" for item in items)
    return CompareReport(
        legacy_file=legacy_path.name,
        legacy_sha256=hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest(),
        current_file=current_path.name,
        current_sha256=hashlib.sha256(current_raw.encode("utf-8")).hexdigest(),
        items=items,
        matched_count=matched,
        difference_count=len(items) - matched,
        unmatched_old=unmatched_old,
        unmatched_new=unmatched_new,
        mapping_file=mapping_path.name,
        mapping_sha256=hashlib.sha256(mapping_raw.encode("utf-8")).hexdigest(),
    )
