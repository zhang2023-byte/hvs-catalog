"""Cross-record checks for references, versions, paper ownership, roster correspondence,
and material locations.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .common import ArtifactRef, GapRef, ItemRef, RecordRef
from .file_checks import file_digest, scoped_verification
from .objects import ExternalRecord, ObjectChange, ObjectRecord
from .paper import (
    CoverageReport,
    MeasuredValue,
    ObjectContribution,
    PaperRecord,
    QuantileReport,
)
from .patches import (
    UNSUPPORTED_ITEM_TARGET,
    PatchApplicationError,
    apply_item_changes,
    locate_patch_item,
)
from .questions import (
    DecisionPatch,
    DecisionRecord,
    QuestionRecord,
    compute_question_fingerprint,
)
from .registry import AnyRecord
from .roster import RosterEntry, RosterRecord
from .runs import ReleaseRecord, RunRecord
from .source import (
    DataElementLocator,
    EvidenceRecord,
    SourcePackageRecord,
    gaps_affecting_quantity,
    gaps_affecting_roster,
)
from .submission import SubmissionError  # noqa: F401  public exception type


@dataclass(frozen=True)
class ValidationIssue:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class CollectionValidationError(ValueError):
    """Record collection failed validation; issues aggregates all problems."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(issue) for issue in issues))


class _Index:
    def __init__(self, records: list[AnyRecord]) -> None:
        self.by_id: dict[str, dict[int, AnyRecord]] = {}
        for record in records:
            self.by_id.setdefault(record.record_id, {})[record.revision] = record

    def resolve(self, ref: RecordRef) -> AnyRecord | None:
        revisions = self.by_id.get(ref.record_id)
        if revisions is None:
            return None
        return revisions.get(ref.revision)


def _iter_refs(value: Any):
    """Recursively traverse references, yielding (path, reference)."""
    if isinstance(value, (RecordRef, ItemRef, GapRef, ArtifactRef)):
        yield "", value
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _iter_refs(getattr(value, name))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_refs(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_refs(item)


def iter_record_refs(value: Any):
    """Reuse shared traversal to yield exact record references, including item/gap
    references.
    """
    for _, ref in _iter_refs(value):
        if isinstance(ref, (RecordRef, ItemRef)):
            yield RecordRef(record_id=ref.record_id, revision=ref.revision)
        elif isinstance(ref, GapRef):
            yield ref.source_ref


def _check_evidence_ref(
    ref: RecordRef,
    *,
    index: _Index,
    issues: list[ValidationIssue],
    where: str,
    field: str,
    paper_keys: set[str] | None,
) -> EvidenceRecord | None:
    """Verify one evidence reference's exact revision, type, and paper ownership.

    Return resolved evidence or None when absent; general resolution already reports
    missing targets.
    """
    target = index.resolve(ref)
    if target is None:
        return None
    if not isinstance(target, EvidenceRecord):
        issues.append(
            ValidationIssue(
                where,
                f"{field} 应指向 evidence 记录，"
                f"实际是 {target.RECORD_NAME}（{ref.record_id} v{ref.revision}）",
            )
        )
        return None
    if paper_keys is not None and target.data.paper_key not in paper_keys:
        issues.append(
            ValidationIssue(
                where,
                f"{field} 指向另一篇论文的证据：{ref.record_id} v{ref.revision}"
                f"（{target.data.paper_key}）",
            )
        )
        return None
    return target


def _check_evidence_refs(
    refs: Iterable[RecordRef],
    *,
    index: _Index,
    issues: list[ValidationIssue],
    where: str,
    field: str,
    paper_keys: set[str] | None,
) -> None:
    for ref in refs:
        _check_evidence_ref(
            ref,
            index=index,
            issues=issues,
            where=where,
            field=field,
            paper_keys=paper_keys,
        )


def _fragment_checks(
    parts: list[Any],
    *,
    index: _Index,
    issues: list[ValidationIssue],
    where: str,
    field: str,
    paper_keys: set[str] | None,
) -> None:
    """Numeric components retain valid evidence references; containment is not checked."""
    for part in parts:
        evidence = _check_evidence_ref(
            part.evidence_ref,
            index=index,
            issues=issues,
            where=where,
            field=field,
            paper_keys=paper_keys,
        )
        if evidence is None:
            continue  # Missing/type/ownership errors were reported above.


def _value_fragment_checks(
    values: list[MeasuredValue],
    *,
    index: _Index,
    issues: list[ValidationIssue],
    where: str,
    paper_keys: set[str],
) -> None:
    for value in values:
        spot = f"{where} 数值 {value.value_id}"
        _fragment_checks(
            value.direct_evidence,
            index=index,
            issues=issues,
            where=spot,
            field="直接证据",
            paper_keys=paper_keys,
        )
        reports: list[tuple[str, CoverageReport | QuantileReport]] = []
        if value.coverage is not None:
            reports.append(("覆盖概率", value.coverage))
        if value.lower_quantile is not None:
            reports.append(("下分位点", value.lower_quantile))
        if value.upper_quantile is not None:
            reports.append(("上分位点", value.upper_quantile))
        for label, report in reports:
            _fragment_checks(
                report.direct_evidence,
                index=index,
                issues=issues,
                where=f"{spot} {label}",
                field=label,
                paper_keys=paper_keys,
            )
        for component in value.error_components:
            _fragment_checks(
                component.direct_evidence,
                index=index,
                issues=issues,
                where=f"{spot} 分量 {component.name}",
                field="分量证据",
                paper_keys=paper_keys,
            )
        _check_evidence_refs(
            value.context_evidence_refs,
            index=index,
            issues=issues,
            where=spot,
            field="语境证据",
            paper_keys=paper_keys,
        )


def _item_ids(record: AnyRecord) -> set[str] | None:
    if isinstance(record, PaperRecord):
        return {c.item_id for c in record.data.object_contributions}
    if isinstance(record, RosterRecord):
        return {e.item_id for e in record.data.entries}
    return None


def _source_paper_keys(
    source_refs: Iterable[RecordRef], index: _Index
) -> set[str] | None:
    """Derive paper keys from source references; None means all unresolved and already
    reported elsewhere.
    """
    keys: set[str] = set()
    resolved_any = False
    for ref in source_refs:
        target = index.resolve(ref)
        if isinstance(target, SourcePackageRecord):
            resolved_any = True
            keys.add(target.data.paper_key)
    return keys if resolved_any else None


def _check_coverage_against_source(
    *,
    where: str,
    state: str,
    checked_file_ids: list[str],
    gap_refs: list[GapRef],
    source: SourcePackageRecord,
    affecting: list[str],
) -> list[ValidationIssue]:
    """Check completion against source-package constraints through shared
    conversion/direct-load rules.
    """
    issues: list[ValidationIssue] = []
    known_files = {file.file_id for file in source.data.files}
    unknown_files = [
        file_id for file_id in checked_file_ids if file_id not in known_files
    ]
    if unknown_files:
        issues.append(
            ValidationIssue(where, f"已检查文件不在来源包中：{sorted(unknown_files)}")
        )
    listed = {gap_ref.gap_id for gap_ref in gap_refs}
    wrong_source = [
        gap_ref.gap_id for gap_ref in gap_refs if gap_ref.source_ref != source.ref()
    ]
    if wrong_source:
        issues.append(
            ValidationIssue(
                where, f"缺口引用不属于该论文的来源包：{sorted(wrong_source)}"
            )
        )
    unrelated = sorted(listed - set(affecting))
    if unrelated:
        issues.append(
            ValidationIssue(
                where,
                f"引用了按影响范围与此目标无关的缺口：{unrelated}",
            )
        )
    if state == "complete" and affecting:
        issues.append(
            ValidationIssue(
                where,
                f"完成声明与已知材料缺口矛盾：{sorted(affecting)}",
            )
        )
    elif state == "partial":
        missing = sorted(set(affecting) - listed)
        if missing:
            issues.append(
                ValidationIssue(where, f"部分检查声明缺少影响该目标的缺口：{missing}")
            )
    return issues


def _check_contribution(
    contribution: ObjectContribution,
    *,
    index: _Index,
    issues: list[ValidationIssue],
    spot: str,
    keys: set[str],
    source: SourcePackageRecord | None,
) -> None:
    """Check contribution evidence, numeric fragments, and completion for
    conversion/patches.
    """
    for identifier in contribution.identifiers:
        _check_evidence_refs(
            identifier.evidence_refs,
            index=index,
            issues=issues,
            where=f"{spot} 标识符 {identifier.value}",
            field="标识符证据",
            paper_keys=keys,
        )
    _check_evidence_refs(
        contribution.contribution_summary.evidence_refs,
        index=index,
        issues=issues,
        where=f"{spot} 研究摘要",
        field="摘要证据",
        paper_keys=keys,
    )
    _check_evidence_refs(
        contribution.contribution_type.evidence_refs,
        index=index,
        issues=issues,
        where=f"{spot} 贡献类型",
        field="类型证据",
        paper_keys=keys,
    )
    _check_evidence_refs(
        contribution.paper_boundness.evidence_refs,
        index=index,
        issues=issues,
        where=f"{spot} 作者判断",
        field="判断证据",
        paper_keys=keys,
    )
    for assessment in contribution.conditional_assessments:
        _check_evidence_refs(
            assessment.evidence_refs,
            index=index,
            issues=issues,
            where=f"{spot} 条件判断",
            field="条件判断证据",
            paper_keys=keys,
        )
    for group in contribution.quantities:
        _value_fragment_checks(
            group.values,
            index=index,
            issues=issues,
            where=f"{spot} {group.quantity}",
            paper_keys=keys,
        )
    for conflict in contribution.quantity_conflicts:
        _value_fragment_checks(
            conflict.alternatives,
            index=index,
            issues=issues,
            where=f"{spot} 冲突 {conflict.conflict_id}",
            paper_keys=keys,
        )
    for entry in contribution.quantity_coverage:
        for ref in entry.issue_refs:
            target = index.resolve(ref)
            if target is not None and not isinstance(target, QuestionRecord):
                issues.append(
                    ValidationIssue(
                        spot,
                        f"coverage issue_refs 应指向 question 记录，"
                        f"实际是 {target.RECORD_NAME} ({ref.record_id})",
                    )
                )
        if source is not None:
            issues.extend(
                _check_coverage_against_source(
                    where=f"{spot} 完成声明 {entry.quantity}",
                    state=entry.state,
                    checked_file_ids=entry.checked_file_ids,
                    gap_refs=entry.gap_refs,
                    source=source,
                    affecting=gaps_affecting_quantity(
                        source.data.gaps,
                        contribution.item_id,
                        entry.quantity,
                    ),
                )
            )


def _check_roster_entry(
    entry: RosterEntry,
    *,
    index: _Index,
    issues: list[ValidationIssue],
    where: str,
    keys: set[str],
) -> None:
    """Check roster evidence/question references for records/patches."""
    for identifier in entry.identifiers:
        _check_evidence_refs(
            identifier.evidence_refs,
            index=index,
            issues=issues,
            where=f"{where} 条目 {entry.item_id} 标识符",
            field="标识符证据",
            paper_keys=keys,
        )
    _check_evidence_refs(
        entry.evidence_refs,
        index=index,
        issues=issues,
        where=f"{where} 条目 {entry.item_id}",
        field="条目证据",
        paper_keys=keys,
    )
    for ref in entry.issue_refs:
        target = index.resolve(ref)
        if target is not None and not isinstance(target, QuestionRecord):
            issues.append(
                ValidationIssue(
                    where,
                    f"条目 {entry.item_id} issue_refs 应指向 question 记录，"
                    f"实际是 {target.RECORD_NAME} ({ref.record_id})",
                )
            )


def _check_typed_refs(
    record: AnyRecord, index: _Index, issues: list[ValidationIssue]
) -> None:
    """Verify target record types/content correspondence by field responsibilities."""

    def expect(ref: RecordRef, types: tuple[type[AnyRecord], ...], field: str) -> None:
        target = index.resolve(ref)
        if target is None:
            return  # General resolution already reported missing references.
        if not isinstance(target, types):
            names = "/".join(t.RECORD_NAME for t in types)
            issues.append(
                ValidationIssue(
                    f"{record.record_id}",
                    f"{field} 应指向 {names} 记录，实际是 {target.RECORD_NAME} ({ref.record_id})",
                )
            )

    where = record.record_id
    if isinstance(record, SourcePackageRecord):
        for gap in record.data.gaps:
            _check_evidence_refs(
                gap.mention_evidence_refs,
                index=index,
                issues=issues,
                where=where,
                field=f"缺口 {gap.gap_id} 提及证据",
                paper_keys={record.data.paper_key},
            )
    elif isinstance(record, EvidenceRecord):
        expect(record.data.source_ref, (SourcePackageRecord,), "source_ref")
        target = index.resolve(record.data.source_ref)
        if isinstance(target, SourcePackageRecord):
            if record.data.file_id not in {f.file_id for f in target.data.files}:
                issues.append(
                    ValidationIssue(
                        where, f"证据文件 {record.data.file_id} 不在来源包中"
                    )
                )
            if record.data.paper_key != target.data.paper_key:
                issues.append(ValidationIssue(where, "证据与来源包的 paper_key 不一致"))
    elif isinstance(record, PaperRecord):
        expect(record.data.source_ref, (SourcePackageRecord,), "source_ref")
        expect(record.data.roster_ref, (RosterRecord,), "roster_ref")
        for field, ref in (
            ("source_ref", record.data.source_ref),
            ("roster_ref", record.data.roster_ref),
        ):
            target = index.resolve(ref)
            if target is not None and target.data.paper_key != record.data.paper_key:
                issues.append(ValidationIssue(where, f"{field} 指向另一篇论文的记录"))
        roster = index.resolve(record.data.roster_ref)
        if isinstance(roster, RosterRecord):
            _check_roster_binding(record, roster, issues)
        source = index.resolve(record.data.source_ref)
        keys = {record.data.paper_key}
        for contribution in record.data.object_contributions:
            _check_contribution(
                contribution,
                index=index,
                issues=issues,
                spot=f"{where} 贡献 {contribution.item_id}",
                keys=keys,
                source=source if isinstance(source, SourcePackageRecord) else None,
            )
    elif isinstance(record, RosterRecord):
        expect(record.data.source_ref, (SourcePackageRecord,), "source_ref")
        source = index.resolve(record.data.source_ref)
        if (
            isinstance(source, SourcePackageRecord)
            and source.data.paper_key != record.data.paper_key
        ):
            issues.append(ValidationIssue(where, "名单与来源包的 paper_key 不一致"))
        keys = {record.data.paper_key}
        for entry in record.data.entries:
            _check_roster_entry(
                entry, index=index, issues=issues, where=where, keys=keys
            )
        for group in record.data.unresolved_groups:
            _check_evidence_refs(
                group.evidence_refs,
                index=index,
                issues=issues,
                where=f"{where} 未列清范围 {group.group_id}",
                field="范围证据",
                paper_keys=keys,
            )
        if isinstance(source, SourcePackageRecord):
            issues.extend(
                _check_coverage_against_source(
                    where=f"{where} 名单材料范围",
                    state=record.data.coverage.state,
                    checked_file_ids=record.data.coverage.checked_file_ids,
                    gap_refs=record.data.coverage.gap_refs,
                    source=source,
                    affecting=gaps_affecting_roster(source.data.gaps),
                )
            )
    elif isinstance(record, QuestionRecord):
        for ref in record.data.source_refs:
            expect(ref, (SourcePackageRecord,), "source_refs")
        for ref in record.data.decision_refs:
            expect(ref, (DecisionRecord,), "decision_refs")
        keys = _source_paper_keys(record.data.source_refs, index)
        _check_evidence_refs(
            record.data.evidence_refs,
            index=index,
            issues=issues,
            where=where,
            field="问题证据",
            paper_keys=keys,
        )
        for target_ref in record.data.target_refs:
            target = index.resolve(target_ref)
            if target is None:
                continue
            if (
                isinstance(target, PaperRecord)
                and keys is not None
                and target.data.paper_key not in keys
            ):
                issues.append(
                    ValidationIssue(
                        where,
                        f"问题目标 {target_ref.record_id} 与其材料不属于同一篇论文",
                    )
                )
            items = _item_ids(target)
            if items is not None and target_ref.item_id not in items:
                issues.append(
                    ValidationIssue(
                        where,
                        f"问题目标条目 {target_ref.item_id} 不在 {target.RECORD_NAME} 记录中",
                    )
                )
    elif isinstance(record, DecisionRecord):
        expect(record.data.question_ref, (QuestionRecord,), "question_ref")
        question = index.resolve(record.data.question_ref)
        if isinstance(question, QuestionRecord):
            recomputed = compute_question_fingerprint(
                category=question.data.category,
                target_refs=question.data.target_refs,
                evidence_refs=question.data.evidence_refs,
                source_refs=question.data.source_refs,
                rules_ref=question.data.rules_ref,
            )
            if question.data.fingerprint != recomputed:
                issues.append(
                    ValidationIssue(
                        question.record_id,
                        "问题指纹与实际固定内容不一致，保存的指纹必须按当前内容重新计算",
                    )
                )
            if record.data.question_fingerprint != recomputed:
                issues.append(
                    ValidationIssue(
                        where,
                        "决定的指纹与问题当前修订不一致，旧决定不能套用新问题",
                    )
                )
            keys = _source_paper_keys(question.data.source_refs, index)
            _check_evidence_refs(
                record.data.evidence_refs,
                index=index,
                issues=issues,
                where=where,
                field="决定证据",
                paper_keys=keys,
            )
            if record.data.result_patch is not None:
                _check_patch(
                    record,
                    question=question,
                    patch=record.data.result_patch,
                    index=index,
                    issues=issues,
                )
    elif isinstance(record, ObjectRecord):
        for link in record.data.contribution_links:
            target = index.resolve(link.target_ref)
            if isinstance(target, PaperRecord):
                items = _item_ids(target)
                if items is not None and link.item_id not in items:
                    issues.append(
                        ValidationIssue(
                            where,
                            f"对象关联的条目 {link.item_id} 不在论文 {target.record_id} 中",
                        )
                    )
                _check_evidence_refs(
                    link.evidence_refs,
                    index=index,
                    issues=issues,
                    where=f"{where} 贡献关联",
                    field="关联证据",
                    paper_keys={target.data.paper_key},
                )
            elif target is not None:
                issues.append(
                    ValidationIssue(
                        where,
                        f"对象贡献关联应指向 paper 记录，实际是 {target.RECORD_NAME}",
                    )
                )
            for ref in link.decision_refs:
                expect(ref, (DecisionRecord,), "贡献关联 decision_refs")
        for link in record.data.external_links:
            target = index.resolve(link.target_ref)
            expect(link.target_ref, (ExternalRecord,), "external_links")
            if isinstance(target, ExternalRecord):
                entry_ids = {e.entry_id for e in target.data.entries}
                if link.entry_id not in entry_ids:
                    issues.append(
                        ValidationIssue(
                            where,
                            f"外部关联条目 {link.entry_id} 不在 {target.record_id} 中",
                        )
                    )
            _check_evidence_refs(
                link.evidence_refs,
                index=index,
                issues=issues,
                where=f"{where} 外部关联",
                field="关联证据",
                paper_keys=None,
            )
            for ref in link.decision_refs:
                expect(ref, (DecisionRecord,), "外部关联 decision_refs")
        for change in record.data.changes:
            _check_object_change(change, index, where, issues)
    elif isinstance(record, RunRecord):
        for ref in record.data.input_snapshot.source_refs:
            expect(ref, (SourcePackageRecord,), "input_snapshot source_refs")
        for ref in record.data.input_snapshot.existing_decision_refs:
            expect(ref, (DecisionRecord,), "existing_decision_refs")
    elif isinstance(record, ReleaseRecord):
        for ref in record.data.source_run_refs:
            expect(ref, (RunRecord,), "source_run_refs")


def _check_object_change(
    change: ObjectChange,
    index: _Index,
    where: str,
    issues: list[ValidationIssue],
) -> None:
    for ref in [*change.before_refs, *change.after_refs]:
        target = index.resolve(ref)
        if target is not None and not isinstance(target, ObjectRecord):
            issues.append(
                ValidationIssue(
                    where,
                    f"对象变更 {change.change_id} 的前后引用应指向 object 记录，"
                    f"实际是 {target.RECORD_NAME}",
                )
            )


def _check_ref_targets(
    where: str, value: Any, index: _Index, issues: list[ValidationIssue]
) -> None:
    """Verify every data reference target exists, sharing record/patch checks."""
    for _, ref in _iter_refs(value):
        if isinstance(ref, (RecordRef, ItemRef)):
            if index.resolve(ref) is None:
                issues.append(
                    ValidationIssue(
                        where,
                        f"引用的记录不存在：{ref.record_id} v{ref.revision}",
                    )
                )
        elif isinstance(ref, GapRef):
            target = index.resolve(ref.source_ref)
            if not isinstance(target, SourcePackageRecord):
                issues.append(
                    ValidationIssue(
                        where,
                        f"缺口引用不指向来源包：{ref.source_ref.record_id}",
                    )
                )
            elif ref.gap_id not in {g.gap_id for g in target.data.gaps}:
                issues.append(
                    ValidationIssue(
                        where,
                        f"来源包中不存在缺口：{ref.gap_id}",
                    )
                )


def _check_patch(
    decision: DecisionRecord,
    *,
    question: QuestionRecord,
    patch: DecisionPatch,
    index: _Index,
    issues: list[ValidationIssue],
) -> None:
    """Patch targets must lie within question scope; validate final item states/references
    together.

    Reuse records.patches pure target/field interpretation with actual application.
    Validate declarations only, without persistence/new revisions.
    """
    where = decision.record_id
    allowed = {
        (ref.record_id, ref.revision, ref.item_id) for ref in question.data.target_refs
    }
    key = (patch.target.record_id, patch.target.revision, patch.target.item_id)
    if key not in allowed:
        issues.append(
            ValidationIssue(
                where,
                f"补丁目标 {patch.target.record_id} v{patch.target.revision} "
                f"条目 {patch.target.item_id} 不在问题 {question.record_id} 的目标范围内",
            )
        )
        return
    target_record = index.resolve(patch.target)
    if target_record is None:
        return  # General resolution already reported missing targets.
    item = locate_patch_item(target_record, patch.target.item_id)
    if item is UNSUPPORTED_ITEM_TARGET:
        issues.append(
            ValidationIssue(
                where,
                f"补丁目标记录 {target_record.record_id}（{target_record.RECORD_NAME}）"
                "不支持条目级修改",
            )
        )
        return
    if item is None:
        issues.append(
            ValidationIssue(
                where,
                f"补丁目标条目 {patch.target.item_id} 不在 "
                f"{target_record.RECORD_NAME} 记录 {target_record.record_id} 中",
            )
        )
        return
    try:
        patched = apply_item_changes(item, patch.changes)
    except PatchApplicationError as exc:
        issues.extend(ValidationIssue(where, reason) for reason in exc.reasons)
        return
    # Apply identical science-reference checks to patches and ordinary record paths.
    _check_ref_targets(f"{where} 补丁", patched, index, issues)
    keys = {target_record.data.paper_key}
    if isinstance(patched, ObjectContribution) and isinstance(
        target_record, PaperRecord
    ):
        source = index.resolve(target_record.data.source_ref)
        _check_contribution(
            patched,
            index=index,
            issues=issues,
            spot=f"{where} 补丁 贡献 {patched.item_id}",
            keys=keys,
            source=source if isinstance(source, SourcePackageRecord) else None,
        )
    elif isinstance(patched, RosterEntry):
        _check_roster_entry(
            patched, index=index, issues=issues, where=f"{where} 补丁", keys=keys
        )


def _check_roster_binding(
    paper: PaperRecord, roster: RosterRecord, issues: list[ValidationIssue]
) -> None:
    where = f"{paper.record_id} 名单对应"
    entries = {entry.item_id: entry for entry in roster.data.entries}
    contributions = {c.roster_item_id: c for c in paper.data.object_contributions}
    for entry in roster.data.entries:
        if entry.disposition != "included":
            if entry.contribution_id is not None:
                issues.append(
                    ValidationIssue(
                        where,
                        f"非收录条目 {entry.item_id} 不应携带贡献编号",
                    )
                )
            continue
        contribution = contributions.get(entry.item_id)
        if contribution is None or contribution.item_id != entry.contribution_id:
            issues.append(
                ValidationIssue(
                    where,
                    f"收录条目 {entry.item_id} 没有一一对应的贡献",
                )
            )
    for contribution in paper.data.object_contributions:
        entry = entries.get(contribution.roster_item_id)
        if entry is None:
            issues.append(
                ValidationIssue(
                    where,
                    f"贡献 {contribution.item_id} 引用的名单条目不存在",
                )
            )
        elif (
            entry.disposition != "included"
            or entry.contribution_id != contribution.item_id
        ):
            issues.append(
                ValidationIssue(
                    where,
                    f"贡献 {contribution.item_id} 与名单条目 {entry.item_id} 不对应",
                )
            )


def _check_materials(
    records: list[AnyRecord],
    index: _Index,
    materials_root: Path,
    issues: list[ValidationIssue],
) -> None:
    for record in records:
        if isinstance(record, ExternalRecord):
            # External responses are paper materials; archived run requests/responses belong to runs
            # and are verified by workspace saves/recovery, not this material root.
            for _, ref in _iter_refs(record.data):
                if isinstance(ref, ArtifactRef):
                    _check_artifact(record.record_id, ref, materials_root, issues)
    for record in records:
        if isinstance(record, SourcePackageRecord):
            for file in record.data.files:
                path = (materials_root / file.path).resolve()
                try:
                    path.relative_to(materials_root.resolve())
                except ValueError:
                    issues.append(
                        ValidationIssue(record.record_id, f"文件路径越界：{file.path}")
                    )
                    continue
                if not path.is_file():
                    issues.append(
                        ValidationIssue(
                            record.record_id,
                            f"材料文件缺失：{file.path}",
                        )
                    )
                    continue
                digest = file_digest(path)
                if digest != file.sha256:
                    issues.append(
                        ValidationIssue(
                            record.record_id,
                            f"材料文件哈希改变：{file.path}",
                        )
                    )
        elif isinstance(record, EvidenceRecord):
            _check_locator(record, index, materials_root, issues)


def _check_artifact(
    where: str, ref: ArtifactRef, materials_root: Path, issues: list[ValidationIssue]
) -> None:
    path = (materials_root / ref.path).resolve()
    try:
        path.relative_to(materials_root.resolve())
    except ValueError:
        issues.append(ValidationIssue(where, f"文件引用路径越界：{ref.path}"))
        return
    if not path.is_file():
        issues.append(ValidationIssue(where, f"引用文件缺失：{ref.path}"))
        return
    digest = file_digest(path)
    if digest != ref.sha256:
        issues.append(ValidationIssue(where, f"引用文件哈希改变：{ref.path}"))


def _check_locator(
    record: EvidenceRecord,
    index: _Index,
    materials_root: Path,
    issues: list[ValidationIssue],
) -> None:
    source = index.resolve(record.data.source_ref)
    if not isinstance(source, SourcePackageRecord):
        return
    file = next(
        (f for f in source.data.files if f.file_id == record.data.file_id), None
    )
    if file is None:
        return
    path = (materials_root / file.path).resolve()
    if not path.is_file():
        return
    locator = record.data.locator
    where = record.record_id
    if locator.kind in {"pdf", "pdf_image"}:
        from .pdf import pdf_evidence_block

        try:
            block = pdf_evidence_block(
                source,
                materials_root,
                file.file_id,
                locator.page,
                locator.block_id,
                require_image=locator.kind == "pdf_image",
            )
            if locator.kind == "pdf" and record.data.excerpt != block["text"]:
                issues.append(
                    ValidationIssue(
                        where, "PDF excerpt differs from the fixed parser block"
                    )
                )
        except (ValueError, KeyError, OSError) as exc:
            issues.append(ValidationIssue(where, str(exc)))
    elif locator.kind == "text":
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not (1 <= locator.start_line <= locator.end_line <= len(lines)):
            issues.append(
                ValidationIssue(
                    where, f"行范围越界：{locator.start_line}-{locator.end_line}"
                )
            )
            return
        actual = "\n".join(lines[locator.start_line - 1 : locator.end_line])
        if actual != record.data.excerpt:
            issues.append(ValidationIssue(where, "引文与指定版本的原文行不一致"))
    elif locator.kind == "table_cell":
        # Share preparation's CSV/ECSV logical readers consistently across saving/backup/consumption.
        # Zero matches fail; unique matches need no ordinal; multiple matches require additional keys
        # or explicit ordinals, never silent first-row selection. row_number is one-based among row_key matches,
        # not a physical source line number.
        from .tabular import TabularReadError, read_table_records

        try:
            records = read_table_records(
                path.read_bytes(), media_type=file.media_type, name=file.path
            )
        except TabularReadError as exc:
            issues.append(ValidationIssue(where, f"表格文件无法按逻辑记录读取：{exc}"))
            return
        columns = records.column_list()
        if locator.column not in columns:
            issues.append(ValidationIssue(where, f"表格列不存在：{locator.column}"))
            return
        missing_key_columns = [key for key in locator.row_key if key not in columns]
        if missing_key_columns:
            issues.append(
                ValidationIssue(
                    where,
                    "行键列不存在："
                    + ", ".join(missing_key_columns)
                    + "（不能用不存在的列认证任意对象行）",
                )
            )
            return
        if any(value is None for value in locator.row_key.values()):
            issues.append(ValidationIssue(where, "行键含空值：不能与实际单元格比较"))
            return
        matched = [
            row
            for row in records.rows
            if all(
                str(row.get(key)) == str(value)
                for key, value in locator.row_key.items()
            )
        ]
        if not matched:
            issues.append(ValidationIssue(where, "行键零命中：定位不到对象行"))
            return
        if len(matched) > 1 and locator.row_number is None:
            issues.append(
                ValidationIssue(
                    where,
                    f"行键命中 {len(matched)} 行且未填 row_number："
                    "多命中必须补足行键或明确序号，不默取第一行",
                )
            )
            return
        row_number = locator.row_number or 1
        if not (1 <= row_number <= len(matched)):
            issues.append(
                ValidationIssue(
                    where,
                    f"行序号 {row_number} 超出命中行数 {len(matched)}",
                )
            )
            return
        cell = matched[row_number - 1].get(locator.column)
        if cell != record.data.excerpt:
            issues.append(ValidationIssue(where, "单元格引文与表格内容不一致"))
    elif locator.kind == "data_element":
        if not (
            isinstance(locator, DataElementLocator)
            and locator.selector.kind == "json_pointer"
        ):
            issues.append(
                ValidationIssue(
                    where,
                    f"未支持的材料定位选择器：{locator.selector.kind}"
                    "（材料未验证，不能视为已通过材料核对）",
                )
            )
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            value = _resolve_json_pointer(document, locator.selector.value)
        except (json.JSONDecodeError, LookupError, ValueError) as exc:
            issues.append(ValidationIssue(where, f"JSON 定位无法解析：{exc}"))
            return
        expected = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if expected != record.data.excerpt:
            issues.append(ValidationIssue(where, "数据元素引文与解码值不一致"))


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError(f"非法 JSON Pointer：{pointer}")
    value = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            if token not in value:
                raise LookupError(f"JSON Pointer 缺少键：{token}")
            value = value[token]
        elif isinstance(value, list):
            index = int(token) if token.isdigit() else None
            if index is None:
                raise TypeError(f"JSON Pointer 数组下标必须是数字：{token}")
            if index >= len(value):
                raise LookupError(f"JSON Pointer 数组越界：{index}")
            value = value[index]
        else:
            raise TypeError(f"JSON Pointer 无法继续解析：{token}")
    return value


@scoped_verification
def validate_collection(
    records: list[AnyRecord], materials_root: Path | str | None = None
) -> list[ValidationIssue]:
    """Validate collections; raise CollectionValidationError on issues, otherwise return an
    empty list.
    """
    issues: list[ValidationIssue] = []

    index = _Index(records)
    seen: set[tuple[str, int]] = set()
    last_revision: dict[str, int] = {}
    for record in records:
        key = (record.record_id, record.revision)
        if key in seen:
            issues.append(
                ValidationIssue(record.record_id, f"修订 {record.revision} 重复保存")
            )
        seen.add(key)
        previous = last_revision.get(record.record_id)
        if previous is not None and record.revision <= previous:
            issues.append(
                ValidationIssue(
                    record.record_id,
                    f"修订 {record.revision} 未递增（前一修订为 {previous}）",
                )
            )
        last_revision[record.record_id] = record.revision

    for record in records:
        _check_ref_targets(record.record_id, record.data, index, issues)
        _check_typed_refs(record, index, issues)

    if materials_root is not None:
        _check_materials(records, index, Path(materials_root), issues)

    if issues:
        raise CollectionValidationError(issues)
    return issues
