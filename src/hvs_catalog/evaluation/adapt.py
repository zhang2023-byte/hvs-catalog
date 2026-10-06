"""Pure in-memory adaptation from existing Gold and extraction outputs to scoring inputs.

adapt_compact_gold_pair/adapt_compact_answer/adapt_annotation map compact v2 official
pairs, answer-only inputs, or v1 annotations to GoldPaperEntry. Preserve printed values,
errors, units, interval interpretations, conditions, and sources verbatim without
rewriting PDF annotation formats. PDF locations/quotes enter context_evidence as
diagnostics, never as verified materials.

Pair adaptation validates in-memory bindings (paper, scope, answer_sha256, evidence-file
declarations), retains bindings for report/plan checks, and reuses the full checker's
file-free pair_preparation_problems. Material/science gaps block readiness. Answer-only
inputs lack metadata validation and official readiness; strict entry points reject them
and direct callers to unofficial structural adaptation.

prediction_from_execution maps saved shared-execution results to PredictionPaperEntry.
Delivery/runtime state and saved records establish output; later failures never erase
rosters. science_completion separately determines scientific completion.

Read no files, call no services, mutate no inputs, and feed no Gold/diagnostics back
into extraction. Missing/blank Gold conditions are preparation gaps, not claims that
conditions were unreported; preserve explicitly filled approved explanations verbatim.
Real Gold access still requires an authorized private scoring workspace; this module
never scans real repositories.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field, model_validator

from ..pipeline import execution_result_refs, get_extraction
from ..records.common import SHA256_PATTERN, Config, RecordRef, sha256_of
from ..records.paper import (
    CoverageReport,
    DirectEvidencePart,
    ErrorComponent,
    MeasuredValue,
    PaperRecord,
    QuantileReport,
)
from ..records.roster import RosterRecord
from ..records.rules import load_rules
from ..records.source import SourcePackageRecord
from ..runtime import get_execution as runtime_get_execution
from ..workspace import Workspace
from .compact import (
    CompactGoldAnswer,
    CompactGoldPair,
    pair_preparation_problems,
    validate_compact_gold_pair,
)
from .errors import GoldIntegrityError
from .models import GoldAnnotation, GoldRulesRef
from .scoring import (
    ConfirmedRange,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
)

__all__ = [
    "GoldAdaptation",
    "GoldAdaptationError",
    "GoldReadinessDecision",
    "GoldReadinessReview",
    "adapt_annotation",
    "adapt_compact_answer",
    "adapt_compact_gold_pair",
    "gold_entries_from_annotation",
    "gold_entries_from_compact_answer",
    "gold_entries_from_compact_pair",
    "gold_readiness_fingerprint",
    "prediction_from_execution",
    "science_completion",
]


class GoldAdaptationError(ValueError):
    """Gold or extraction output cannot be honestly mapped to scoring input, for example
    due to preparation gaps.
    """


class GoldReadinessDecision(Config):
    """A human decision on one exact preparation gap, not a generic approval flag."""

    code: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    outcome: Literal[
        "outside_scoring_scope", "retained_limitation", "defined_reference"
    ]
    basis: str = Field(min_length=1)


class GoldReadinessReview(Config):
    """Scoring-only review bound to the complete current answer and metadata.

    The authorized preparation caller supplies actual human decisions. This is not
    save permission, PDF verification, or a replacement for missing evidence.
    """

    pair_sha256: str = Field(pattern=SHA256_PATTERN)
    decision_source: str = Field(min_length=1)
    decisions: list[GoldReadinessDecision] = Field(min_length=1)
    complete_within_scoring_scope: bool = False

    @model_validator(mode="after")
    def _unique(self) -> GoldReadinessReview:
        keys = [(item.code, item.subject) for item in self.decisions]
        if len(keys) != len(set(keys)):
            raise ValueError("Gold评分准备裁定重复")
        return self


def gold_readiness_fingerprint(pair: CompactGoldPair) -> str:
    """Bind all scientific content, material declarations, and prior review metadata."""
    return sha256_of(
        {
            "answer": pair.answer.model_dump(mode="json"),
            "metadata": pair.metadata.model_dump(mode="json", by_alias=True),
        }
    )


#: Answer-only adaptation permits structural mapping but never establishes official readiness.
ANSWER_ONLY_LIMITATION = (
    "answer-only：未执行 metadata 绑定核对（answer_sha256/范围审定/材料"
    "声明），不构成正式就绪；正式评分请使用经检查的完整配对"
)


def _evidence_texts(evidences: list[Any]) -> list[str]:
    """Preserve evidence locations and optional quotes verbatim in a diagnostic sidecar."""
    texts: list[str] = []
    for evidence in evidences:
        location = str(evidence.location)
        quote = getattr(evidence, "quote", "") or ""
        texts.append(f"{location} — {quote}" if quote else location)
    return texts


def _ref(record_id: str) -> RecordRef:
    # Scoring evidence references locate diagnostics only; Gold PDF evidence is not a workspace record.
    # Derive locator identifiers without impersonating verified record references.
    return RecordRef(record_id=record_id, revision=1)


def _part(part: str, raw: str, evidence_id: str) -> DirectEvidencePart:
    return DirectEvidencePart(
        part=part, evidence_ref=_ref(evidence_id), raw_fragment=raw
    )


def _probability(report: Any, evidence_id: str) -> CoverageReport | None:
    if report is None:
        return None
    return CoverageReport(
        value=report.value,
        unit=report.unit,
        direct_evidence=[_part("value", report.value, evidence_id)],
    )


def _quantile(report: Any, evidence_id: str) -> QuantileReport | None:
    if report is None:
        return None
    return QuantileReport(
        value=report.value,
        unit=report.unit,
        direct_evidence=[_part("value", report.value, evidence_id)],
    )


def _measured_value(value: Any, *, value_id: str) -> MeasuredValue:
    """Map one Gold value to MeasuredValue, preserving printed contents verbatim."""
    condition = value.condition
    if condition is None or not condition.strip():
        raise GoldAdaptationError(
            f"Gold 数值 {value_id} 的条件为空：字段缺失不等于“原文未报告条件”"
            "的科学断言，不能为评分匹配自动补写；请补全条件或按准备缺口处理"
        )
    evidence_id = f"{value_id}:evidence"
    direct: list[DirectEvidencePart] = []
    for part_name in (
        "value",
        "error",
        "lower_error",
        "upper_error",
        "range_lower",
        "range_upper",
    ):
        raw = getattr(value, part_name)
        if raw is not None:
            direct.append(_part(part_name, raw, evidence_id))
    components: list[ErrorComponent] = []
    for index, component in enumerate(value.error_components):
        component_id = f"{value_id}:component:{index}"
        parts = [
            _part(part_name, getattr(component, part_name), component_id)
            for part_name in ("error", "lower_error", "upper_error")
            if getattr(component, part_name) is not None
        ]
        components.append(
            ErrorComponent(
                name=component.name,
                error=component.error,
                lower_error=component.lower_error,
                upper_error=component.upper_error,
                direct_evidence=parts,
            )
        )
    return MeasuredValue(
        value_id=value_id,
        value=value.value,
        error=value.error,
        lower_error=value.lower_error,
        upper_error=value.upper_error,
        unit=value.unit,
        limit_kind=value.limit_kind,
        range_lower=value.range_lower,
        range_upper=value.range_upper,
        lower_inclusive=None,
        upper_inclusive=None,
        interval_kind=value.interval_kind,
        coverage=_probability(value.coverage, f"{evidence_id}:coverage"),
        lower_quantile=_quantile(value.lower_quantile, f"{evidence_id}:lower_quantile"),
        upper_quantile=_quantile(value.upper_quantile, f"{evidence_id}:upper_quantile"),
        error_components=components,
        condition=value.condition,
        frame=None,
        epoch=None,
        coordinate_format=value.coordinate_format,
        representation=value.representation,
        purpose=None,
        source=value.source,
        source_note=value.source_note,
        author_usage=None,
        direct_evidence=direct,
        context_evidence_refs=[],
    )


def _gold_value_entry(value: Any, *, record_id: str) -> GoldValueEntry:
    return GoldValueEntry(
        record_id=record_id,
        value=_measured_value(value, value_id=record_id),
        context_evidence=_evidence_texts([*value.evidence, *value.context_evidence]),
    )


def _boundness_status(boundness: Any) -> str | None:
    status = getattr(boundness, "value", None)
    if status is None:
        status = getattr(boundness, "status", None)
    return status


def _contribution_type(contribution: Any) -> str | None:
    kind = getattr(contribution, "contribution_type", None)
    if kind is None:
        return None
    return str(getattr(kind, "value", kind))


def _objects_from_contributions(contributions: list[Any]) -> list[GoldObjectEntry]:
    objects: list[GoldObjectEntry] = []
    for contribution in contributions:
        contribution_id = contribution.contribution_id
        groups: dict[str, list[GoldValueEntry]] = {}
        for group in contribution.quantities:
            entries = groups.setdefault(group.quantity, [])
            for index, item in enumerate(group.values):
                entries.append(
                    _gold_value_entry(
                        item, record_id=f"{contribution_id}:{group.quantity}:{index}"
                    )
                )
        for conflict in contribution.quantity_conflicts:
            entries = groups.setdefault(conflict.quantity, [])
            for index, alternative in enumerate(conflict.alternatives):
                entries.append(
                    _gold_value_entry(
                        alternative,
                        record_id=f"{contribution_id}:{conflict.conflict_id}:{index}",
                    )
                )
        objects.append(
            GoldObjectEntry(
                label=contribution_id,
                identifiers=[
                    identifier.value for identifier in contribution.identifiers
                ],
                contribution_type=_contribution_type(contribution),
                author_judgment=_boundness_status(contribution.paper_boundness),
                quantities=[
                    GoldQuantityEntry(quantity_path=path, values=values)
                    for path, values in groups.items()
                ],
            )
        )
    return objects


def _confirmed_for(
    paper_key: str, declared: dict[str, list[ConfirmedRange]] | None
) -> list[ConfirmedRange]:
    if not declared:
        return []
    return list(declared.get(paper_key, []))


def _partial_needs_ranges(
    paper_key: str, declared: dict[str, list[ConfirmedRange]] | None
) -> list[ConfirmedRange]:
    ranges = _confirmed_for(paper_key, declared)
    if not ranges:
        raise GoldAdaptationError(
            f"论文 {paper_key} 的 Gold 只声明部分覆盖，但没有显式给出已确认"
            "完整的封闭子范围；范围说明是自由文本，不能由适配程序推导，"
            "请由获准的评分准备方按 Gold 范围说明显式声明"
        )
    return ranges


@dataclass(frozen=True)
class GoldAdaptation:
    """One Gold adaptation result: entry, bindings, and readiness.

    binding retains pair-checkable paper/rules/material/answer_sha256/coverage/scope
    approvals; answer-only input has None. formal_readiness is true only with valid
    bindings and no scoring-readiness gaps, and always false for answer-only input.
    Nonempty readiness_gaps block strict readiness. limitations describe readiness
    guarantees without blocking structural mapping.
    """

    paper_key: str
    entries: list[GoldPaperEntry]
    binding: dict | None = None
    formal_readiness: bool = False
    readiness_gaps: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


def _pair_binding(pair: CompactGoldPair) -> dict:
    metadata = pair.metadata
    revision = metadata.revision
    return {
        "paper_key": pair.answer.arxiv_id,
        "paper_version": metadata.paper.version,
        "schema": {
            "name": metadata.schema_tag.name,
            "version": metadata.schema_tag.version,
        },
        "rules": {"version": metadata.rules.version, "sha256": metadata.rules.sha256},
        "files": [
            {"file_id": item.file_id, "name": item.name, "sha256": item.sha256}
            for item in metadata.files
        ],
        "coverage": pair.answer.coverage.value,
        "scope_review": metadata.review.scope.value,
        "answer_sha256": metadata.review.answer_sha256,
        "revision": (
            {"revised_by": revision.revised_by, "revised_at": revision.revised_at}
            if revision is not None
            else None
        ),
    }


def _rules_readiness_gaps(reference: GoldRulesRef) -> list[str]:
    """Historical approvals remain readable but do not approve new scientific rules."""
    rules = load_rules()
    if (reference.version, reference.sha256) != (rules.version, rules.sha256):
        return [
            (
                f"科学规则版本不符：Gold 保留 {reference.version}/{reference.sha256[:12]}，"
                f"当前为 {rules.version}/{rules.sha256[:12]}；当前规则下评分须重新审定"
            )
        ]
    return []


def _preparation_gaps(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None,
    scoring_review: GoldReadinessReview | None = None,
) -> list[str]:
    """Map shared in-memory preparation checks to categorized readiness gaps.

    After validate_compact_gold_pair binding checks, reuse the full checker's file-free
    pair_preparation_problems without changing categories, codes, subjects, or messages.
    Resolve coverage through an approved closed subset or a content-bound human
    scoring-scope decision. Other accepted material/science gaps require their own
    exact decisions; missing evidence and undefined references cannot be waived.
    In-memory checks do not claim actual PDF/material verification; see GoldAdaptation
    for formal_readiness guarantees.
    """
    ranges = _confirmed_for(pair.answer.arxiv_id, confirmed_ranges)
    gaps: list[str] = []
    problems = pair_preparation_problems(pair)
    decisions = {}
    if scoring_review is not None:
        scoring_review = GoldReadinessReview.model_validate(
            scoring_review.model_dump(mode="json")
        )
        if scoring_review.pair_sha256 != gold_readiness_fingerprint(pair):
            raise GoldAdaptationError("评分准备裁定与当前完整Gold/元数据指纹不符")
        available = {(item.code, item.subject) for item in problems}
        for decision in scoring_review.decisions:
            key = (decision.code, decision.subject)
            if key not in available:
                raise GoldAdaptationError("评分准备裁定引用了不存在的缺口")
            if decision.code not in {
                "partial_coverage",
                "material_differences",
            } and not decision.code.startswith("unresolved_"):
                raise GoldAdaptationError(
                    "缺证据、缺条件或未结构化科学结果须修复Gold，不能豁免"
                )
            if decision.code == "partial_coverage":
                if not scoring_review.complete_within_scoring_scope:
                    raise GoldAdaptationError("部分覆盖裁定须明确确认本轮评分范围完整")
                if decision.outcome != "outside_scoring_scope":
                    raise GoldAdaptationError(
                        "部分覆盖缺口只能以明确的评分范围裁定处理"
                    )
            if (
                decision.code.startswith("unresolved_")
                and decision.outcome == "retained_limitation"
            ):
                raise GoldAdaptationError("科学未决项须确定参考答案或明确在评分范围外")
            decisions[key] = decision
    for problem in problems:
        if problem.code == "partial_coverage" and ranges:
            continue
        if (problem.code, problem.subject) in decisions:
            continue
        label = (
            "科学待决缺口" if problem.category == "science_pending" else "材料准备缺口"
        )
        gaps.append(
            f"{label} {problem.category}/{problem.code}（{problem.subject}）："
            f"{problem.message}"
        )
    return gaps


def _answer_only_gaps(answer: CompactGoldAnswer) -> list[str]:
    """Identify answer-only unresolved scientific items in memory; absent bindings
    represent material-verification gaps.
    """
    return [
        f"科学待决缺口 science_pending/unresolved_{index + 1}"
        f"（{item.subject}）：{item.value}"
        for index, item in enumerate(answer.unresolved)
    ]


def _entries_from_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None,
) -> list[GoldPaperEntry]:
    coverage = answer.coverage.value
    return [
        GoldPaperEntry(
            paper_key=answer.arxiv_id,
            coverage=coverage,
            confirmed_ranges=(
                _partial_needs_ranges(answer.arxiv_id, confirmed_ranges)
                if coverage == "partial"
                else []
            ),
            objects=_objects_from_contributions(answer.contributions),
        )
    ]


def adapt_compact_gold_pair(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
    scoring_review: GoldReadinessReview | None = None,
) -> GoldAdaptation:
    """Adapt compact v2 official pairs to scoring Gold inputs and bindings in memory.

    Validate paper/scope/answer_sha256/evidence-file bindings first; reject failures and
    retain bindings for reports/plans. Shared file-free preparation checks populate
    readiness_gaps for missing sources/evidence/conditions and unresolved science,
    unstructured conflicts, or documented material differences. Complete coverage never
    hides gaps. Approved structured conflicts map normally; partial coverage requires an
    explicit closed subset. Read no actual PDFs and claim no completed material/science
    review.
    """
    try:
        validate_compact_gold_pair(pair)
    except GoldIntegrityError as exc:
        raise GoldAdaptationError(
            f"compact Gold 配对绑定校验失败，不能适配：{exc}"
        ) from exc
    gaps = _preparation_gaps(
        pair, confirmed_ranges=confirmed_ranges, scoring_review=scoring_review
    )
    gaps.extend(_rules_readiness_gaps(pair.metadata.rules))
    scoring_complete = (
        scoring_review is not None
        and scoring_review.complete_within_scoring_scope
        and any(item.code == "partial_coverage" for item in scoring_review.decisions)
    )
    entries = (
        [
            GoldPaperEntry(
                paper_key=pair.answer.arxiv_id,
                coverage="complete",
                objects=_objects_from_contributions(pair.answer.contributions),
            )
        ]
        if scoring_complete
        else _entries_from_answer(pair.answer, confirmed_ranges=confirmed_ranges)
    )
    binding = _pair_binding(pair)
    limitations = []
    if scoring_review is not None:
        binding["scoring_review"] = scoring_review.model_dump(mode="json")
        limitations = [item.basis for item in scoring_review.decisions]
    return GoldAdaptation(
        paper_key=pair.answer.arxiv_id,
        entries=entries,
        binding=binding,
        formal_readiness=not gaps,
        readiness_gaps=gaps,
        limitations=limitations,
    )


def adapt_compact_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> GoldAdaptation:
    """Adapt compact v2 answer-only inputs to scoring Gold.

    Without metadata, do not check answer_sha256, scope approval, or material
    declarations. formal_readiness stays false with explicit limitations. This supports
    callers performing separate in-memory checks; unresolved scientific items remain
    preparation gaps.
    """
    gaps = _answer_only_gaps(answer)
    return GoldAdaptation(
        paper_key=answer.arxiv_id,
        entries=_entries_from_answer(answer, confirmed_ranges=confirmed_ranges),
        binding=None,
        formal_readiness=False,
        readiness_gaps=gaps,
        limitations=[ANSWER_ONLY_LIMITATION],
    )


def adapt_annotation(
    annotation: GoldAnnotation,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> GoldAdaptation:
    """Adapt self-bound v1 annotations to scoring Gold in memory.

    v1 has no independent coverage declaration: accept only official answers without
    pending items and map complete coverage. Reject drafts/pending annotations rather
    than assuming completeness. Bindings come from the annotation's schema version,
    rules, and material declarations.
    """
    if annotation.kind != "answer":
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 是草稿（kind=draft），不能当作"
            "评分 Gold；请先完成审定与保存"
        )
    if annotation.pending:
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 带有 {len(annotation.pending)} 项"
            "未决待办，覆盖完整性未知，不能按完整 Gold 评分"
        )
    paper_key = annotation.paper.arxiv_id
    if not paper_key:
        raise GoldAdaptationError(
            f"文档 {annotation.document_id} 缺少 arXiv 编号，无法作为评分论文标识"
        )
    binding = {
        "paper_key": paper_key,
        "document_id": annotation.document_id,
        "schema": {
            "name": annotation.schema_tag.name,
            "version": annotation.schema_tag.version,
        },
        "rules": {
            "version": annotation.rules.version,
            "sha256": annotation.rules.sha256,
        },
        "files": [
            {"file_id": item.file_id, "name": item.name, "sha256": item.sha256}
            for item in annotation.files
        ],
        "status": annotation.status,
    }
    return GoldAdaptation(
        paper_key=paper_key,
        entries=[
            GoldPaperEntry(
                paper_key=paper_key,
                coverage="complete",
                confirmed_ranges=[],
                objects=_objects_from_contributions(annotation.contributions),
            )
        ],
        binding=binding,
        formal_readiness=not _rules_readiness_gaps(annotation.rules),
        readiness_gaps=_rules_readiness_gaps(annotation.rules),
    )


def _require_ready(adaptation: GoldAdaptation) -> list[GoldPaperEntry]:
    """Strict readiness gate: require both valid bindings and preparation readiness."""
    if adaptation.binding is None:
        raise GoldAdaptationError(
            "Gold 输入缺少可核对的绑定（answer-only 未执行 metadata 绑定"
            "核对），不能作为就绪评分输入；如需非正式结构映射请使用 "
            "adapt_compact_answer（返回 binding=None、formal_readiness="
            "False 与 limitations 的诊断结构），正式评分请使用经检查的"
            "完整配对 gold_entries_from_compact_pair"
        )
    if adaptation.readiness_gaps or not adaptation.formal_readiness:
        raise GoldAdaptationError(
            "Gold 存在准备缺口，不能作为就绪评分输入："
            + "；".join(adaptation.readiness_gaps)
            + ("；就绪状态为 False" if not adaptation.readiness_gaps else "")
        )
    return adaptation.entries


def gold_entries_from_compact_pair(
    pair: CompactGoldPair,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
    scoring_review: GoldReadinessReview | None = None,
) -> list[GoldPaperEntry]:
    """Adapt compact v2 official pairs to ready scoring Gold through the strict wrapper.

    Validate pair bindings first; reject preparation gaps without applicable
    content-bound human scoring decisions. The review never replaces missing
    evidence or material verification.
    """
    return _require_ready(
        adapt_compact_gold_pair(
            pair, confirmed_ranges=confirmed_ranges, scoring_review=scoring_review
        )
    )


def gold_entries_from_compact_answer(
    answer: CompactGoldAnswer,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> list[GoldPaperEntry]:
    """Reject compact v2 answer-only inputs as ready scoring Gold.

    Missing metadata prevents answer_sha256/scope/material binding checks, so structural
    mapping cannot establish official readiness. Reject programmatically rather than
    merely warning. For diagnostic structure, use adapt_compact_answer, returning
    binding=None, formal_readiness=False, and limitations. confirmed_ranges remains for
    legacy signature compatibility and cannot change rejection.
    """
    del confirmed_ranges
    raise GoldAdaptationError(
        "answer-only 输入缺少 metadata 绑定核对，严格入口拒绝返回可当"
        " ready 的裸条目；请改用 adapt_compact_answer 获取带 "
        "binding=None/formal_readiness=False/limitations 的非正式结构，"
        "或使用经检查的完整配对 gold_entries_from_compact_pair"
    )


def gold_entries_from_annotation(
    annotation: GoldAnnotation,
    *,
    confirmed_ranges: dict[str, list[ConfirmedRange]] | None = None,
) -> list[GoldPaperEntry]:
    """Adapt v1 annotation answers to ready scoring Gold through the strict wrapper.

    Use self-contained schema/rules/material bindings and the same readiness gate. Valid
    official bound answers remain supported; adaptation already rejects drafts/pending
    items.
    """
    del (
        confirmed_ranges
    )  # v1 maps complete coverage and has no partial-coverage declaration.
    return _require_ready(adapt_annotation(annotation))


# ---- Extraction outputs to scoring predictions ----


def science_completion(status: dict, roster, paper) -> tuple[str, list[str]]:
    """Scientific completion independently of runtime graph termination.

    Return pending for open or answered-but-unresolved scientific questions, unresolved
    roster entries/groups, or unresolved overall author assessments; retain
    errors/failures separately. Otherwise partial applies to runtime
    errors/nontermination, missing values, incomplete/gapped roster coverage,
    material-only unfinished author assessments (status=None with gap_refs only), or
    incomplete quantity coverage. Only truly complete results without unresolved items
    are complete. With no valid output, return failed for failures and unfinished
    otherwise. Excluded entries and resolved questions do not block completion. Accept
    read-only views or structurally equivalent duck-typed objects without modifying
    them.
    """
    reasons: list[str] = []
    questions = status.get("questions", [])
    open_count = sum(1 for item in questions if item.get("status") == "open")
    unresolved_answered = sum(
        1
        for item in questions
        if item.get("status") == "answered"
        and item.get("answered_outcome") == "unresolved"
    )
    pending_items = []
    unresolved_groups = []
    coverage = None
    if roster is not None:
        pending_items = [
            entry
            for entry in roster.data.entries
            if getattr(entry, "disposition", None) == "pending"
        ]
        unresolved_groups = list(getattr(roster.data, "unresolved_groups", None) or [])
        coverage = getattr(roster.data, "coverage", None)
    boundness_pending = False
    boundness_gap_only = False
    if paper is not None:
        for contribution in paper.data.object_contributions:
            boundness = contribution.paper_boundness
            if boundness.status is None:
                if boundness.issue_refs:
                    boundness_pending = True
                elif getattr(boundness, "gap_refs", None):
                    boundness_gap_only = True
    if (
        open_count
        or unresolved_answered
        or pending_items
        or unresolved_groups
        or boundness_pending
    ):
        if open_count:
            reasons.append(f"开放科学问题 {open_count} 项")
        if unresolved_answered:
            reasons.append(f"已答复仍未决的科学问题 {unresolved_answered} 项")
        if pending_items:
            reasons.append(
                "名单未决条目 " + ", ".join(entry.item_id for entry in pending_items)
            )
        if unresolved_groups:
            reasons.append(
                "存在未列清的原文范围 "
                + ", ".join(
                    getattr(group, "group_id", str(index))
                    for index, group in enumerate(unresolved_groups)
                )
            )
        if boundness_pending:
            reasons.append("存在未决的作者总体判断")
        return "pending", reasons
    has_output = paper is not None or roster is not None
    if not has_output:
        return ("failed" if status.get("state") == "failed" else "unfinished"), reasons
    if status.get("state") != "finished":
        # Current failures/nontermination remain facts. Historical errors after recovery to finished remain
        # in report failure diagnostics but no longer block scientific complete under the established ruling;
        # current valid results, question state, and coverage checks prove completeness.
        if status.get("errors"):
            reasons.append(f"运行错误 {len(status['errors'])} 项")
        reasons.append(f"运行图状态为 {status.get('state')!r}，未到终止")
    if coverage is not None:
        state = getattr(coverage, "state", None)
        if state != "complete":
            reasons.append(f"名单覆盖状态为 {state!r}，材料范围未全部检查")
        gap_refs = getattr(coverage, "gap_refs", None) or []
        if gap_refs:
            reasons.append(f"名单覆盖存在未处理材料缺口 {len(gap_refs)} 项")
    if boundness_gap_only:
        reasons.append("作者总体判断因材料缺口未完成（无已决科学问题）")
    if paper is None:
        reasons.append("已保存名单但数值结果未产出")
    else:
        for contribution in paper.data.object_contributions:
            for item in contribution.quantity_coverage:
                if item.state != "complete":
                    reasons.append(f"物理量 {item.quantity} 完成状态为 {item.state}")
    if reasons:
        return "partial", reasons
    return "complete", []


def _l0_from_status(status: dict, roster, paper) -> dict:
    """Map L0 delivery from actual valid records and completion from science state.

    Delivered but scientifically incomplete output is partial because the approved
    scoring model cannot mark delivered papers pending; partial results are an honest
    lower bound. Reports express pending science separately through science_completion.
    Later failures never erase saved rosters; failures without valid results remain
    failed, never successful empty rosters.
    """
    errors = list(status["errors"])
    has_output = paper is not None or roster is not None
    science_state, _ = science_completion(status, roster, paper)
    if not has_output:
        return {
            "delivered": False,
            "format_valid": False,
            "completion": ("failed" if status["state"] == "failed" else "unfinished"),
            "errors": errors,
        }
    return {
        "delivered": True,
        "format_valid": True,
        "completion": "complete" if science_state == "complete" else "partial",
        "errors": errors,
    }


def _objects_from_paper(paper: PaperRecord) -> list[PredictionObjectEntry]:
    objects: list[PredictionObjectEntry] = []
    for contribution in paper.data.object_contributions:
        groups = {
            group.quantity: list(group.values) for group in contribution.quantities
        }
        for conflict in contribution.quantity_conflicts:
            groups.setdefault(conflict.quantity, []).extend(conflict.alternatives)
        quantities = [
            PredictionQuantityEntry(
                quantity_path=path,
                values=[
                    PredictionValueEntry(record_id=value.value_id, value=value)
                    for value in values
                ],
            )
            for path, values in groups.items()
        ]
        objects.append(
            PredictionObjectEntry(
                paper_key=paper.data.paper_key,
                label=contribution.item_id,
                identifiers=[
                    identifier.value for identifier in contribution.identifiers
                ],
                contribution_type=contribution.contribution_type.value,
                author_judgment=contribution.paper_boundness.status,
                quantities=quantities,
            )
        )
    return objects


def _objects_from_roster(roster: RosterRecord) -> list[PredictionObjectEntry]:
    """Without a paper record, use included roster entries as object predictions without
    values.
    """
    return [
        PredictionObjectEntry(
            paper_key=roster.data.paper_key,
            label=entry.item_id,
            identifiers=[identifier.value for identifier in entry.identifiers],
            contribution_type=entry.contribution_id,
            author_judgment=None,
            quantities=[],
        )
        for entry in roster.data.entries
        if entry.disposition == "included"
    ]


def prediction_from_execution(
    space: Workspace, execution_id: str
) -> PredictionPaperEntry:
    """Map one shared extraction execution to scoring predictions, with L0 state derived
    from the run manifest.

    Reuse MeasuredValue records directly. Exclude unresolved/excluded roster entries
    from predicted objects: unresolved entries are not extras, and exclusions are not
    omissions.
    """
    status = get_extraction(space, execution_id)
    paper: PaperRecord | None = None
    roster: RosterRecord | None = None
    for ref in execution_result_refs(space, execution_id):
        record = space.store().read_record_current(ref.record_id)
        if isinstance(record, PaperRecord) and paper is None:
            paper = record
        elif isinstance(record, RosterRecord) and roster is None:
            roster = record
    l0 = _l0_from_status(status, roster, paper)
    paper_key = _paper_key_from(space, execution_id, paper, roster)
    if not l0["delivered"] or not l0["format_valid"]:
        return PredictionPaperEntry(
            paper_key=paper_key,
            delivered=False,
            format_valid=False,
            completion=l0["completion"],
            errors=l0["errors"],
            objects=[],
        )
    objects = (
        _objects_from_paper(paper)
        if paper is not None
        else (_objects_from_roster(roster) if roster is not None else [])
    )
    return PredictionPaperEntry(
        paper_key=paper_key,
        delivered=True,
        format_valid=True,
        completion=l0["completion"],
        errors=l0["errors"],
        objects=objects,
        execution_state=status["state"],
        science_state=science_completion(status, roster, paper)[0],
        pending_questions=sum(
            item.get("status") == "open" for item in status.get("questions", [])
        ),
    )


def _paper_key_from(
    space: Workspace,
    execution_id: str,
    paper: PaperRecord | None,
    roster: RosterRecord | None,
) -> str:
    if paper is not None:
        return paper.data.paper_key
    if roster is not None:
        return roster.data.paper_key
    # With no output records, read paper identity from the frozen source package to retain the denominator.
    handle = runtime_get_execution(space, execution_id)
    for ref in handle.input_snapshot.source_refs:
        record = space.store().read_record(ref)
        if isinstance(record, SourcePackageRecord):
            return record.data.paper_key
    raise GoldAdaptationError(
        f"执行 {execution_id} 没有任何可定位论文的名单或论文记录，无法确定"
        "预测的论文标识"
    )
