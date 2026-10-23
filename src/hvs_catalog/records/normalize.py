"""Deterministically convert submissions: completion expansion, evidence/references, and
identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .common import (
    GapRef,
    ItemRef,
    RecordRef,
    build_record,
    canonical_json,
)
from .identifiers import normalize_catalog_identifier
from .paper import (
    ConditionalAssessment,
    ContributionSummary,
    ContributionTypeEntry,
    CoverageReport,
    DirectEvidencePart,
    ErrorComponent,
    IdentifierEntry,
    MeasuredValue,
    ObjectContribution,
    PaperBoundness,
    PrecedentUse,
    QuantileReport,
    QuantityConflict,
    QuantityCoverageEntry,
    QuantityGroup,
    author_error_blocks,
    find_conflict_exclusivity_violation,
)
from .questions import QuestionRecord, RulesRef, compute_question_fingerprint
from .roster import RosterData, RosterEntry, RosterIdentifier, UnresolvedGroup
from .rules import current_rules_ref, quantity_paths
from .source import (
    EvidenceData,
    EvidenceRecord,
    SourcePackageRecord,
    gaps_affecting_quantity,
    gaps_affecting_roster,
)
from .submission import (
    ConflictSubmission,
    ContributionFieldsSubmission,
    CoverageDeclaration,
    InlineEvidence,
    InlineQuestion,
    ObjectQuantitySubmission,
    QuantitySubmission,
    RosterSubmission,
    SubmissionError,
    ValueSubmission,
)


@dataclass
class StepContext:
    """Inputs and identifier sources fixed at step start."""

    source: SourcePackageRecord
    paper_ref: RecordRef
    created_at: datetime
    requested_quantities: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    existing_evidence: tuple[EvidenceRecord, ...] = ()
    existing_questions: dict[str, RecordRef] = field(default_factory=dict)
    id_prefix: str = "record"
    execution_id: str | None = None
    rules_ref: RulesRef | None = None
    legacy_questions: bool = False

    def resolved_rules_ref(self) -> RulesRef:
        if self.rules_ref is not None:
            return self.rules_ref
        return RulesRef(**current_rules_ref())

    @property
    def file_ids(self) -> frozenset[str]:
        return frozenset(file.file_id for file in self.source.data.files)

    @property
    def gap_ids(self) -> frozenset[str]:
        return frozenset(gap.gap_id for gap in self.source.data.gaps)

    def require_gap_ids(self, gap_ids: list[str], where: str) -> None:
        unknown = [gap_id for gap_id in gap_ids if gap_id not in self.gap_ids]
        if unknown:
            raise SubmissionError(f"{where} 引用了输入中不存在的材料缺口：{unknown}")

    def require_file_ids(self, file_ids: list[str], where: str) -> None:
        unknown = [file_id for file_id in file_ids if file_id not in self.file_ids]
        if unknown:
            raise SubmissionError(f"{where} 引用了输入中不存在的文件：{unknown}")

    def require_question_ids(self, issue_ids: list[str], where: str) -> None:
        unknown = [
            issue_id
            for issue_id in issue_ids
            if issue_id not in self.existing_questions
        ]
        if unknown:
            raise SubmissionError(f"{where} 引用了输入中不存在的问题：{unknown}")


class _Numbering:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._counters: dict[str, int] = {}

    def next_id(self, kind: str) -> str:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return f"{self._prefix}-{kind}-{self._counters[kind]}"


class _EvidencePool:
    """Create one record for identical evidence under a fixed source version."""

    def __init__(self, context: StepContext, numbering: _Numbering) -> None:
        self._context = context
        self._numbering = numbering
        self._by_key: dict[str, RecordRef] = {}
        self.records: list[EvidenceRecord] = []
        for record in context.existing_evidence:
            key = _evidence_key(
                record.data.source_ref,
                record.data.file_id,
                record.data.locator,
                record.data.excerpt,
            )
            self._by_key.setdefault(key, record.ref())

    def ref_for(self, inline: InlineEvidence) -> RecordRef:
        if inline.source.file_id not in self._context.file_ids:
            raise SubmissionError(
                f"证据引用了输入中不存在的文件：{inline.source.file_id}"
            )
        key = _evidence_key(
            self._context.source.ref(),
            inline.source.file_id,
            inline.source.locator,
            inline.source.excerpt,
        )
        existing = self._by_key.get(key)
        if existing is not None:
            return existing
        data = EvidenceData(
            paper_key=self._context.source.data.paper_key,
            source_ref=self._context.source.ref(),
            file_id=inline.source.file_id,
            locator=inline.source.locator,
            excerpt=inline.source.excerpt,
        )
        record = build_record(
            EvidenceRecord,
            record_id=self._numbering.next_id("ev"),
            created_at=self._context.created_at,
            execution_id=self._context.execution_id,
            data=data,
        )
        self.records.append(record)
        self._by_key[key] = record.ref()
        return record.ref()


def _evidence_key(
    source_ref: RecordRef, file_id: str, locator: object, excerpt: str
) -> str:
    return canonical_json(
        {
            "source": source_ref.model_dump(mode="json"),
            "file": file_id,
            "locator": locator.model_dump(mode="json"),
            "excerpt": excerpt,
        }
    )


class _QuestionFactory:
    def __init__(self, context: StepContext, numbering: _Numbering) -> None:
        self._context = context
        self._numbering = numbering
        self.records: list[QuestionRecord] = []

    def create(
        self,
        *,
        category: str,
        target_refs: list[ItemRef],
        prompt: str,
        evidence_refs: list[RecordRef],
        gap_refs: list[GapRef],
    ) -> QuestionRecord:
        source_ref = self._context.source.ref()
        rules_ref = self._context.resolved_rules_ref()
        fingerprint = compute_question_fingerprint(
            category=category,
            target_refs=target_refs,
            evidence_refs=evidence_refs,
            source_refs=[source_ref],
            rules_ref=rules_ref,
        )
        data = {
            "category": category,
            "target_refs": [ref.model_dump(mode="json") for ref in target_refs],
            "source_refs": [source_ref.model_dump(mode="json")],
            "rules_ref": rules_ref.model_dump(mode="json"),
            "prompt": prompt,
            "evidence_refs": [ref.model_dump(mode="json") for ref in evidence_refs],
            "gap_refs": [ref.model_dump(mode="json") for ref in gap_refs],
            "context_refs": [],
            "fingerprint": fingerprint,
            "status": "open",
            "decision_refs": [],
        }
        record = build_record(
            QuestionRecord,
            record_id=self._numbering.next_id("question"),
            created_at=self._context.created_at,
            execution_id=self._context.execution_id,
            data=data,
        )
        self.records.append(record)
        return record


def _refs(items: list[InlineEvidence], pool: _EvidencePool) -> list[RecordRef]:
    return [pool.ref_for(item) for item in items]


def _author_errors(items, pool, numbering, *, quantity=False):
    from .paper import AuthorError, QuantityAuthorError

    result = []
    for item in items:
        fields = {
            "error_id": numbering.next_id("author-error"),
            "reason": item.reason,
            "raw_values": list(item.raw_values),
            "evidence_refs": _refs(item.evidence, pool),
        }
        if quantity:
            fields.update(
                quantity=item.quantity,
                condition=item.condition,
                source=item.source,
                frame=item.frame,
                epoch=item.epoch,
            )
        result.append((QuantityAuthorError if quantity else AuthorError)(**fields))
    return result


def _context_refs(items: list[InlineEvidence], pool: _EvidencePool) -> list[RecordRef]:
    refs: list[RecordRef] = []
    seen: set[str] = set()
    for item in items:
        if item.context:
            ref = pool.ref_for(item)
            key = canonical_json(ref.model_dump(mode="json"))
            if key not in seen:
                seen.add(key)
                refs.append(ref)
    return refs


def _unique_refs(refs: list[RecordRef]) -> list[RecordRef]:
    unique: list[RecordRef] = []
    seen: set[str] = set()
    for ref in refs:
        key = canonical_json(ref.model_dump(mode="json"))
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _merge_issue_refs(
    *sources: dict[str, list[RecordRef]],
) -> dict[str, list[RecordRef]]:
    """Merge quantity issue references in occurrence order, keeping each once."""
    merged: dict[str, list[RecordRef]] = {}
    for source in sources:
        for quantity, refs in source.items():
            bucket = merged.setdefault(quantity, [])
            bucket.extend(ref for ref in refs if ref not in bucket)
    return merged


def _component_parts(
    items: list[InlineEvidence],
    pool: _EvidencePool,
    allowed_parts: frozenset[str],
    where: str,
) -> list[DirectEvidencePart]:
    parts: list[DirectEvidencePart] = []
    for item in items:
        ref = pool.ref_for(item)
        for part in item.parts:
            if part.part not in allowed_parts:
                raise SubmissionError(
                    f"{where} 的证据组件 {part.part!r} 不在允许范围 {sorted(allowed_parts)}"
                )
            parts.append(
                DirectEvidencePart(
                    part=part.part, evidence_ref=ref, raw_fragment=part.raw_fragment
                )
            )
    return parts


def normalize_value(
    submission: ValueSubmission,
    pool: _EvidencePool,
    value_id: str,
    where: str,
) -> MeasuredValue:
    """Complete simplified values; MeasuredValue rejects invalid saved forms."""
    direct: list[DirectEvidencePart] = []
    for item in submission.evidence:
        ref = pool.ref_for(item)
        for part in item.parts:
            direct.append(
                DirectEvidencePart(
                    part=part.part, evidence_ref=ref, raw_fragment=part.raw_fragment
                )
            )
    coverage = None
    if submission.coverage is not None:
        coverage = CoverageReport(
            value=submission.coverage.value,
            unit=submission.coverage.unit,
            direct_evidence=_component_parts(
                submission.coverage.evidence,
                pool,
                frozenset({"value"}),
                f"{where} 覆盖概率",
            ),
        )
    lower_quantile = None
    if submission.lower_quantile is not None:
        lower_quantile = QuantileReport(
            value=submission.lower_quantile.value,
            unit=submission.lower_quantile.unit,
            direct_evidence=_component_parts(
                submission.lower_quantile.evidence,
                pool,
                frozenset({"value"}),
                f"{where} 下分位点",
            ),
        )
    upper_quantile = None
    if submission.upper_quantile is not None:
        upper_quantile = QuantileReport(
            value=submission.upper_quantile.value,
            unit=submission.upper_quantile.unit,
            direct_evidence=_component_parts(
                submission.upper_quantile.evidence,
                pool,
                frozenset({"value"}),
                f"{where} 上分位点",
            ),
        )
    error_components = [
        ErrorComponent(
            name=component.name,
            error=component.error,
            lower_error=component.lower_error,
            upper_error=component.upper_error,
            direct_evidence=_component_parts(
                component.evidence,
                pool,
                frozenset({"error", "lower_error", "upper_error"}),
                f"{where} 误差分量 {component.name}",
            ),
        )
        for component in submission.error_components
    ]
    try:
        return MeasuredValue(
            value_id=value_id,
            value=submission.value,
            error=submission.error,
            lower_error=submission.lower_error,
            upper_error=submission.upper_error,
            unit=submission.unit,
            limit_kind=submission.limit_kind,
            range_lower=submission.range_lower,
            range_upper=submission.range_upper,
            lower_inclusive=submission.lower_inclusive,
            upper_inclusive=submission.upper_inclusive,
            interval_kind=submission.interval_kind,
            coverage=coverage,
            lower_quantile=lower_quantile,
            upper_quantile=upper_quantile,
            error_components=error_components,
            condition=submission.condition,
            frame=submission.frame,
            epoch=submission.epoch,
            coordinate_format=submission.coordinate_format,
            representation=submission.representation,
            purpose=submission.purpose,
            source=submission.source,
            source_note=submission.source_note,
            author_usage=submission.author_usage,
            direct_evidence=direct,
            context_evidence_refs=_context_refs(submission.evidence, pool),
        )
    except ValueError as exc:
        raise SubmissionError(f"{where}：{exc}") from exc


def _gaps_affecting_quantity(
    context: StepContext, item_id: str, quantity: str
) -> list[str]:
    return gaps_affecting_quantity(context.source.data.gaps, item_id, quantity)


def expand_coverage(
    declaration: CoverageDeclaration,
    *,
    context: StepContext,
    item_id: str,
    extra_issue_refs: dict[str, list[RecordRef]],
) -> list[QuantityCoverageEntry]:
    """Expand shared declarations/exceptions into completion records for every requested
    quantity.

    Require complete shared declarations; exceptions replace quantities completely.
    Reject claims contradicting known gaps.
    """
    from .submission import CoverageDeclarationBody

    requested = list(context.requested_quantities)
    if not requested:
        raise SubmissionError("缺少请求物理量，无法展开完成声明")

    common = declaration.common
    context.require_file_ids(common.checked_file_ids, "共同完成声明")
    context.require_gap_ids(common.gap_ids, "共同完成声明")
    context.require_question_ids(common.issue_ids, "共同完成声明")

    rows: dict[str, CoverageDeclarationBody] = {
        quantity: common for quantity in requested
    }
    seen: set[str] = set()
    for exception in declaration.exceptions:
        if exception.quantity not in rows:
            raise SubmissionError(f"例外声明了未请求的物理量：{exception.quantity}")
        if exception.quantity in seen:
            raise SubmissionError(f"例外声明重复：{exception.quantity}")
        seen.add(exception.quantity)
        where = f"例外声明 {exception.quantity}"
        context.require_file_ids(exception.checked_file_ids, where)
        context.require_gap_ids(exception.gap_ids, where)
        context.require_question_ids(exception.issue_ids, where)
        rows[exception.quantity] = exception

    source_ref = context.source.ref()
    entries: list[QuantityCoverageEntry] = []
    for quantity in requested:
        row = rows[quantity]
        where = f"完成声明 {quantity}"
        affecting = _gaps_affecting_quantity(context, item_id, quantity)
        for gap_id in row.gap_ids:
            if gap_id not in affecting:
                raise SubmissionError(
                    f"{where} 引用的缺口 {gap_id} 按已知影响范围不影响该物理量"
                )
        gap_refs = [
            GapRef(source_ref=source_ref, gap_id=gap_id)
            for gap_id in dict.fromkeys([*row.gap_ids, *affecting])
        ]
        if row.state == "complete" and gap_refs:
            raise SubmissionError(f"{where} 的完成声明与已知材料缺口矛盾")
        issue_refs = _unique_refs(
            [context.existing_questions[issue_id] for issue_id in row.issue_ids]
            + list(extra_issue_refs.get(quantity, []))
        )
        try:
            entries.append(
                QuantityCoverageEntry(
                    quantity=quantity,
                    state=row.state,
                    checked_file_ids=list(row.checked_file_ids),
                    gap_refs=gap_refs,
                    issue_refs=issue_refs,
                )
            )
        except ValueError as exc:
            raise SubmissionError(f"{where}：{exc}") from exc
    return entries


def _initial_coverage() -> list[QuantityCoverageEntry]:
    return [
        QuantityCoverageEntry(
            quantity=quantity,
            state="not_checked",
            checked_file_ids=[],
            gap_refs=[],
            issue_refs=[],
        )
        for quantity in quantity_paths()
    ]


def _inline_question(
    question: InlineQuestion,
    context: StepContext,
    pool: _EvidencePool,
    questions: _QuestionFactory,
    target_refs: list[ItemRef],
) -> RecordRef:
    context.require_gap_ids(question.gap_ids, "就地问题")
    record = questions.create(
        category=question.category,
        target_refs=target_refs,
        prompt=question.prompt,
        evidence_refs=_refs(question.evidence, pool),
        gap_refs=[
            GapRef(source_ref=context.source.ref(), gap_id=gap_id)
            for gap_id in question.gap_ids
        ],
    )
    return record.ref()


# ---------------------------------------------------------------------------
# Roster extraction.
# ---------------------------------------------------------------------------


@dataclass
class ContributionDraft:
    """Roster/candidate contribution contents assembled with identifiers from roster
    entries.
    """

    contribution_id: str
    summary: ContributionSummary
    contribution_type: ContributionTypeEntry
    paper_boundness: PaperBoundness
    conditional_assessments: list[ConditionalAssessment]
    quantity_coverage: list[QuantityCoverageEntry]


@dataclass
class RosterConversionResult:
    roster: RosterData
    contributions: list[ObjectContribution]
    evidence: list[EvidenceRecord]
    questions: list[QuestionRecord]


def convert_roster_submission(
    context: StepContext, submission: RosterSubmission
) -> RosterConversionResult:
    numbering = _Numbering(context.id_prefix)
    pool = _EvidencePool(context, numbering)
    questions = _QuestionFactory(context, numbering)

    context.require_file_ids(submission.coverage.checked_file_ids, "名单材料范围")
    context.require_gap_ids(submission.coverage.gap_ids, "名单材料范围")
    roster_gaps = gaps_affecting_roster(context.source.data.gaps)
    if submission.coverage.state == "complete" and roster_gaps:
        raise SubmissionError(
            "名单完成声明与影响范围未知的材料缺口矛盾："
            f"{sorted(roster_gaps)}；不能视为已完成或未报告，应声明部分检查并引用缺口"
        )
    unrelated = [
        gap_id for gap_id in submission.coverage.gap_ids if gap_id not in roster_gaps
    ]
    if unrelated:
        raise SubmissionError(
            f"名单材料范围引用了按影响范围与名单无关的缺口：{sorted(unrelated)}"
        )
    coverage_gap_ids = list(dict.fromkeys([*submission.coverage.gap_ids, *roster_gaps]))
    roster_ref = RecordRef(record_id=f"{context.id_prefix}-roster", revision=1)
    source_ref = context.source.ref()

    entries: list[RosterEntry] = []
    contributions: list[ObjectContribution] = []
    identifier_owner: dict[str, str] = {}
    for entry_sub in submission.entries:
        item_id = numbering.next_id("roster")
        identifiers: list[RosterIdentifier] = []
        by_value: dict[str, RosterIdentifier] = {}
        for identifier in entry_sub.identifiers:
            value = normalize_catalog_identifier(identifier.value)
            owner = identifier_owner.get(value)
            if owner is not None and owner != item_id:
                raise SubmissionError(
                    f"名称 {value!r} 同时出现在 {owner} 和 {item_id}，"
                    "同一名称只能属于一个对象"
                )
            identifier_owner[value] = item_id
            refs = _refs(identifier.evidence, pool)
            if value in by_value:
                previous = by_value[value]
                previous.evidence_refs.extend(
                    ref for ref in refs if ref not in previous.evidence_refs
                )
                continue
            normalized = RosterIdentifier(value=value, evidence_refs=refs)
            by_value[value] = normalized
            identifiers.append(normalized)
        contribution_identifiers = [
            IdentifierEntry(value=item.value, evidence_refs=item.evidence_refs)
            for item in identifiers
        ]
        context.require_gap_ids(entry_sub.gap_ids, f"名单条目 {item_id}")
        if not context.legacy_questions and entry_sub.question is not None:
            raise SubmissionError("Expert questions are retired")
        entry_issue_refs: list[RecordRef] = []
        if entry_sub.disposition == "pending" and entry_sub.question is not None:
            entry_issue_refs = [
                _inline_question(
                    entry_sub.question,
                    context,
                    pool,
                    questions,
                    [
                        ItemRef(
                            record_id=roster_ref.record_id,
                            revision=roster_ref.revision,
                            item_id=item_id,
                        )
                    ],
                )
            ]
        contribution_id: str | None = None
        if entry_sub.disposition == "included":
            contribution_id = numbering.next_id("contribution")
            draft = _contribution_fields(
                context,
                pool,
                questions,
                contribution_id=contribution_id,
                fields=entry_sub.contribution,
            )
            contributions.append(
                _assemble_contribution(
                    draft, roster_item_id=item_id, identifiers=contribution_identifiers
                )
            )
        try:
            entries.append(
                RosterEntry(
                    item_id=item_id,
                    identifiers=identifiers,
                    disposition=entry_sub.disposition,
                    evidence_refs=_refs(entry_sub.evidence, pool),
                    issue_refs=entry_issue_refs,
                    contribution_id=contribution_id,
                    gap_refs=[
                        GapRef(source_ref=source_ref, gap_id=g)
                        for g in entry_sub.gap_ids
                    ],
                    author_errors=_author_errors(
                        entry_sub.author_errors, pool, numbering
                    ),
                )
            )
        except ValueError as exc:
            raise SubmissionError(f"名单条目 {item_id}：{exc}") from exc

    unresolved_groups: list[UnresolvedGroup] = []
    for group in submission.unresolved_groups:
        member_ids = []
        for value in group.member_identifier_values:
            member = identifier_owner.get(normalize_catalog_identifier(value))
            if member is None:
                raise SubmissionError(
                    f"未列清范围的已明确成员 {value!r} 不在名单条目中"
                )
            member_ids.append(member)
        unresolved_groups.append(
            UnresolvedGroup(
                group_id=numbering.next_id("group"),
                original_range=group.original_range,
                evidence_refs=_refs(group.evidence, pool),
                known_member_item_ids=member_ids,
                reason=group.reason,
            )
        )

    try:
        roster = RosterData(
            paper_key=context.source.data.paper_key,
            source_ref=source_ref,
            method="roster",
            coverage={
                "state": "partial"
                if any(e.author_errors for e in entries)
                else submission.coverage.state,
                "checked_file_ids": list(submission.coverage.checked_file_ids),
                "gap_refs": [
                    GapRef(source_ref=source_ref, gap_id=gap_id)
                    for gap_id in coverage_gap_ids
                ],
            },
            entries=entries,
            unresolved_groups=unresolved_groups,
            issue_refs=[record.ref() for record in questions.records],
        )
    except ValueError as exc:
        raise SubmissionError(f"名单记录：{exc}") from exc
    return RosterConversionResult(
        roster=roster,
        contributions=contributions,
        evidence=pool.records,
        questions=questions.records,
    )


def _contribution_fields(
    context: StepContext,
    pool: _EvidencePool,
    questions: _QuestionFactory,
    *,
    contribution_id: str,
    fields: ContributionFieldsSubmission,
) -> ContributionDraft:
    context.require_gap_ids(fields.boundness.gap_ids, "作者判断")
    if not context.legacy_questions and fields.boundness.question is not None:
        raise SubmissionError("Expert questions are retired")
    boundness_issue_refs: list[RecordRef] = []
    if fields.boundness.question is not None:
        boundness_issue_refs = [
            _inline_question(
                fields.boundness.question,
                context,
                pool,
                questions,
                [
                    ItemRef(
                        record_id=context.paper_ref.record_id,
                        revision=context.paper_ref.revision,
                        item_id=contribution_id,
                    )
                ],
            )
        ]
    try:
        return ContributionDraft(
            contribution_id=contribution_id,
            summary=ContributionSummary(
                text=fields.summary.text,
                evidence_refs=_refs(fields.summary.evidence, pool),
            ),
            contribution_type=ContributionTypeEntry(
                value=fields.contribution_type.value,
                evidence_refs=_refs(fields.contribution_type.evidence, pool),
            ),
            paper_boundness=PaperBoundness(
                status=fields.boundness.status,
                evidence_refs=_refs(fields.boundness.evidence, pool),
                issue_refs=boundness_issue_refs,
                gap_refs=[
                    GapRef(source_ref=context.source.ref(), gap_id=gap_id)
                    for gap_id in fields.boundness.gap_ids
                ],
                author_errors=_author_errors(
                    fields.boundness.author_errors, pool, questions._numbering
                ),
            ),
            conditional_assessments=[
                ConditionalAssessment(
                    condition=item.condition,
                    statement=item.statement,
                    evidence_refs=_refs(item.evidence, pool),
                )
                for item in fields.conditional_assessments
            ],
            quantity_coverage=_initial_coverage(),
        )
    except ValueError as exc:
        raise SubmissionError(f"对象贡献 {contribution_id}：{exc}") from exc


def _assemble_contribution(
    draft: ContributionDraft,
    *,
    roster_item_id: str,
    identifiers: list[IdentifierEntry],
) -> ObjectContribution:
    try:
        return ObjectContribution(
            item_id=draft.contribution_id,
            roster_item_id=roster_item_id,
            identifiers=identifiers,
            contribution_summary=draft.summary,
            contribution_type=draft.contribution_type,
            paper_boundness=draft.paper_boundness,
            conditional_assessments=draft.conditional_assessments,
            quantities=[],
            quantity_conflicts=[],
            quantity_coverage=draft.quantity_coverage,
            precedent_uses=[],
        )
    except ValueError as exc:
        raise SubmissionError(f"对象贡献 {draft.contribution_id}：{exc}") from exc


# ---------------------------------------------------------------------------
# Quantity extraction.
# ---------------------------------------------------------------------------


@dataclass
class QuantityObjectUpdate:
    item_id: str
    quantities: list[QuantityGroup]
    quantity_conflicts: list[QuantityConflict]
    quantity_coverage: list[QuantityCoverageEntry]
    precedent_uses: list[PrecedentUse]
    author_errors: list


@dataclass
class QuantityConversionResult:
    objects: list[QuantityObjectUpdate]
    evidence: list[EvidenceRecord]
    questions: list[QuestionRecord]


def convert_quantity_submission(
    context: StepContext, submission: QuantitySubmission
) -> QuantityConversionResult:
    if tuple(context.requested_quantities) != quantity_paths():
        raise SubmissionError("请求物理量必须与规则范围内 18 种一致")
    given = set(context.object_ids) if context.object_ids else None
    responded = [obj.object_id for obj in submission.objects]
    if len(responded) != len(set(responded)):
        raise SubmissionError("同一对象填写结果重复")
    if given is not None:
        missing = sorted(given - set(responded))
        if missing:
            raise SubmissionError(f"缺少对象的填写结果：{missing}")
        unknown = sorted(set(responded) - given)
        if unknown:
            raise SubmissionError(f"填写结果使用了未提供的对象编号：{unknown}")

    numbering = _Numbering(context.id_prefix)
    pool = _EvidencePool(context, numbering)
    questions = _QuestionFactory(context, numbering)

    if not context.legacy_questions and submission.questions:
        raise SubmissionError("Expert questions are retired")
    question_refs_by_quantity: dict[tuple[str, str], list[RecordRef]] = {}
    for question in submission.questions:
        if given is not None and question.object_id not in given:
            raise SubmissionError(f"问题引用了未提供的对象编号：{question.object_id}")
        ref = _inline_question(
            question,
            context,
            pool,
            questions,
            [
                ItemRef(
                    record_id=context.paper_ref.record_id,
                    revision=context.paper_ref.revision,
                    item_id=question.object_id,
                )
            ],
        )
        if question.quantity is not None:
            key = (question.object_id, question.quantity)
            question_refs_by_quantity.setdefault(key, []).append(ref)

    object_updates = [
        _convert_object_quantities(
            context,
            obj,
            numbering,
            pool,
            questions,
            {
                quantity: refs
                for (object_id, quantity), refs in question_refs_by_quantity.items()
                if object_id == obj.object_id
            },
        )
        for obj in submission.objects
    ]
    return QuantityConversionResult(
        objects=object_updates,
        evidence=pool.records,
        questions=questions.records,
    )


def _convert_object_quantities(
    context: StepContext,
    obj: ObjectQuantitySubmission,
    numbering: _Numbering,
    pool: _EvidencePool,
    questions: _QuestionFactory,
    inline_issue_refs: dict[str, list[RecordRef]],
) -> QuantityObjectUpdate:
    item_id = obj.object_id

    groups: list[QuantityGroup] = []
    seen_quantities: set[str] = set()
    for group in obj.quantities:
        if group.quantity in seen_quantities:
            raise SubmissionError(f"对象 {item_id} 的物理量 {group.quantity} 分成多组")
        seen_quantities.add(group.quantity)
        values = [
            normalize_value(
                value,
                pool,
                numbering.next_id("value"),
                f"对象 {item_id} {group.quantity} 第 {index} 条数值",
            )
            for index, value in enumerate(group.values, start=1)
        ]
        groups.append(QuantityGroup(quantity=group.quantity, values=values))

    conflict_issues: dict[str, list[RecordRef]] = {}
    conflicts = [
        _convert_conflict(
            context, item_id, conflict, numbering, pool, questions, conflict_issues
        )
        for conflict in obj.quantity_conflicts
    ]

    violation = find_conflict_exclusivity_violation(conflicts, groups)
    if violation is not None:
        raise SubmissionError(
            f"对象 {item_id}：冲突中的待决测量不能同时作为已确认数值保存：{violation}"
        )

    coverage = expand_coverage(
        obj.coverage,
        context=context,
        item_id=item_id,
        extra_issue_refs=_merge_issue_refs(conflict_issues, inline_issue_refs),
    )
    author_errors = _author_errors(obj.author_errors, pool, numbering, quantity=True)
    for error in author_errors:
        if any(
            group.quantity == error.quantity
            and any(author_error_blocks(error, value) for value in group.values)
            for group in groups
        ):
            raise SubmissionError("Source-error measurements cannot also be confirmed")
    blocked = {error.quantity for error in author_errors} | {
        conflict.quantity for conflict in conflicts if conflict.issue_ref is None
    }
    coverage = [
        entry.model_copy(update={"state": "partial"})
        if entry.quantity in blocked and entry.state == "complete"
        else entry
        for entry in coverage
    ]

    if obj.precedent_uses:
        raise SubmissionError("Precedent input is retired")
    return QuantityObjectUpdate(
        item_id=item_id,
        quantities=groups,
        quantity_conflicts=conflicts,
        quantity_coverage=coverage,
        precedent_uses=[],
        author_errors=author_errors,
    )


def _convert_conflict(
    context: StepContext,
    item_id: str,
    conflict: ConflictSubmission,
    numbering: _Numbering,
    pool: _EvidencePool,
    questions: _QuestionFactory,
    conflict_issues: dict[str, list[RecordRef]],
) -> QuantityConflict:
    alternatives = [
        normalize_value(
            value,
            pool,
            numbering.next_id("value"),
            f"对象 {item_id} {conflict.quantity} 冲突第 {index} 方",
        )
        for index, value in enumerate(conflict.alternatives, start=1)
    ]
    evidence_refs: list[RecordRef] = []
    for value in alternatives:
        evidence_refs.extend(part.evidence_ref for part in value.direct_evidence)
        evidence_refs.extend(value.context_evidence_refs)
    question = (
        questions.create(
            category="numeric_conflict",
            target_refs=[
                ItemRef(
                    record_id=context.paper_ref.record_id,
                    revision=context.paper_ref.revision,
                    item_id=item_id,
                )
            ],
            prompt=conflict.reason,
            evidence_refs=_unique_refs(evidence_refs),
            gap_refs=[],
        )
        if context.legacy_questions
        else None
    )
    if question is not None:
        conflict_issues.setdefault(conflict.quantity, []).append(question.ref())
    try:
        return QuantityConflict(
            conflict_id=numbering.next_id("conflict"),
            quantity=conflict.quantity,
            reason=conflict.reason,
            alternatives=alternatives,
            issue_ref=question.ref() if question is not None else None,
        )
    except ValueError as exc:
        raise SubmissionError(
            f"对象 {item_id} 冲突 {conflict.quantity}：{exc}"
        ) from exc
