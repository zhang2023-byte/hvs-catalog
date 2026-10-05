"""Scoring-facing Gold v2 answer and its small, bound metadata companion.

The v1 annotation is a migration/review working format.  Conversion is explicit:
the caller supplies documented human review and a disposition for every v1
pending item.  Neither conversion nor serialization constitutes save approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from ..records.common import Config
from ..records.rules import author_state_ids
from .errors import GoldFormatError, GoldIntegrityError

if (
    TYPE_CHECKING
):  # Type annotations only; import inside functions at runtime to avoid cycles.
    from .checking import CheckProblem
from .models import (
    GOLD_COORDINATE_FORMATS,
    GOLD_INTERVAL_KINDS,
    GOLD_LIMIT_KINDS,
    GOLD_REPRESENTATIONS,
    GoldAnnotation,
    GoldConflict,
    GoldContribution,
    GoldErrorComponent,
    GoldEvidence,
    GoldExclusion,
    GoldPdfFile,
    GoldProbability,
    GoldQuantityGroup,
    GoldRulesRef,
    GoldValue,
)

COMPACT_SCHEMA_NAME = "evaluation.hvs_contribution_gold"
COMPACT_METADATA_SCHEMA_NAME = "evaluation.hvs_contribution_gold_metadata"
COMPACT_SCHEMA_VERSION = 2
COMPACT_METADATA_SCHEMA_VERSION = 3
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}$")


class CompactSchemaTag(Config):
    name: str
    version: int

    @model_validator(mode="after")
    def supported(self) -> CompactSchemaTag:
        if self.name not in {COMPACT_SCHEMA_NAME, COMPACT_METADATA_SCHEMA_NAME}:
            raise GoldFormatError(f"Unknown compact Gold schema: {self.name}")
        expected = (
            COMPACT_METADATA_SCHEMA_VERSION
            if self.name == COMPACT_METADATA_SCHEMA_NAME
            else COMPACT_SCHEMA_VERSION
        )
        if self.version != expected:
            raise GoldFormatError(f"Unknown compact Gold version: {self.version}")
        return self


class CompactClaim(Config):
    value: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(default_factory=list)


class CompactIdentifier(Config):
    value: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(default_factory=list)


class CompactBoundness(Config):
    value: str | None
    evidence: list[GoldEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def known_value(self) -> CompactBoundness:
        if self.value is not None and self.value not in author_state_ids():
            raise GoldFormatError(f"Unknown boundness value: {self.value}")
        return self


class CompactErrorComponent(Config):
    name: str = Field(min_length=1)
    error: str | None = None
    lower_error: str | None = None
    upper_error: str | None = None
    evidence: list[GoldEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactErrorComponent:
        GoldErrorComponent.model_validate(self.model_dump(mode="python"))
        return self


class CompactValue(Config):
    """The v1 numeric contract, with value first and its PDF evidence last."""

    value: str | None
    unit: str | None = None
    error: str | None = None
    lower_error: str | None = None
    upper_error: str | None = None
    limit_kind: Literal[GOLD_LIMIT_KINDS] = "none"  # type: ignore[valid-type]
    range_lower: str | None = None
    range_upper: str | None = None
    coordinate_format: Literal[GOLD_COORDINATE_FORMATS] | None = None  # type: ignore[valid-type]
    condition: str | None = None
    interval_kind: Literal[GOLD_INTERVAL_KINDS] | None = None  # type: ignore[valid-type]
    coverage: GoldProbability | None = None
    lower_quantile: GoldProbability | None = None
    upper_quantile: GoldProbability | None = None
    error_components: list[CompactErrorComponent] = Field(default_factory=list)
    representation: Literal[GOLD_REPRESENTATIONS] | None = None  # type: ignore[valid-type]
    source: Literal["this_paper", "prior_work", "unclear"]
    source_note: str | None = None
    context_evidence: list[GoldEvidence] = Field(default_factory=list)
    evidence: list[GoldEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactValue:
        GoldValue.model_validate(self.model_dump(mode="python"))
        return self


class CompactQuantityGroup(Config):
    quantity: str
    values: list[CompactValue] = Field(min_length=1)

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactQuantityGroup:
        GoldQuantityGroup.model_validate(self.model_dump(mode="python"))
        return self


class CompactConflict(Config):
    conflict_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    reason: CompactClaim
    alternatives: list[CompactValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactConflict:
        GoldConflict.model_validate(
            {
                "conflict_id": self.conflict_id,
                "quantity": self.quantity,
                "reason": self.reason.value,
                "evidence": [
                    item.model_dump(mode="python") for item in self.reason.evidence
                ],
                "alternatives": [
                    item.model_dump(mode="python") for item in self.alternatives
                ],
            }
        )
        return self


class CompactExclusion(Config):
    exclusion_id: str = Field(min_length=1)
    reason: CompactClaim

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactExclusion:
        GoldExclusion.model_validate(
            {
                "exclusion_id": self.exclusion_id,
                "reason": self.reason.value,
                "evidence": [
                    item.model_dump(mode="python") for item in self.reason.evidence
                ],
            }
        )
        return self


class CompactContribution(Config):
    contribution_id: str = Field(pattern=r"^\d{4}\.\d{4,5}:cand\d{2,}$")
    identifiers: list[CompactIdentifier] = Field(min_length=1)
    contribution_type: CompactClaim
    contribution_summary: CompactClaim
    paper_boundness: CompactBoundness
    quantities: list[CompactQuantityGroup] = Field(default_factory=list)
    quantity_conflicts: list[CompactConflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def scientific_shape(self) -> CompactContribution:
        if self.contribution_type.value not in {"candidates_found", "follow_up"}:
            raise GoldFormatError("Unknown contribution type")
        values = [item.value for item in self.identifiers]
        if len(values) != len(set(values)):
            raise GoldFormatError("Duplicate identifier within contribution")
        quantities = [item.quantity for item in self.quantities]
        if len(quantities) != len(set(quantities)):
            raise GoldFormatError("Duplicate quantity group within contribution")
        GoldContribution.model_validate(
            {
                "contribution_id": self.contribution_id,
                "identifiers": [
                    item.model_dump(mode="python") for item in self.identifiers
                ],
                "contribution_type": self.contribution_type.value,
                "contribution_summary": self.contribution_summary.value,
                "contribution_evidence": [
                    item.model_dump(mode="python")
                    for item in self.contribution_summary.evidence
                ],
                "paper_boundness": {
                    "status": self.paper_boundness.value,
                    "evidence": [
                        item.model_dump(mode="python")
                        for item in self.paper_boundness.evidence
                    ],
                },
                "quantities": [
                    item.model_dump(mode="python") for item in self.quantities
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
                    for item in self.quantity_conflicts
                ],
            }
        )
        return self


class CompactScopeLimitation(Config):
    subject: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(default_factory=list)


class CompactCoverage(Config):
    value: Literal["complete", "partial"]
    limitations: list[CompactScopeLimitation] = Field(default_factory=list)

    @model_validator(mode="after")
    def partial_requires_limitation(self) -> CompactCoverage:
        if self.value == "partial" and not self.limitations:
            raise GoldFormatError("Partial coverage requires named limitations")
        if self.value == "complete" and self.limitations:
            raise GoldFormatError("Complete coverage cannot contain limitations")
        return self


class CompactUnresolved(Config):
    subject: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(default_factory=list)


class CompactGoldAnswer(Config):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    status: Literal["contributions_found", "no_contributions"]
    coverage: CompactCoverage
    contributions: list[CompactContribution]
    reviewed_exclusions: list[CompactExclusion] = Field(default_factory=list)
    unresolved: list[CompactUnresolved] = Field(default_factory=list)

    @model_validator(mode="after")
    def answer_shape(self) -> CompactGoldAnswer:
        if self.status == "no_contributions" and self.contributions:
            raise GoldFormatError("no_contributions cannot list contributions")
        if self.status == "contributions_found" and not self.contributions:
            raise GoldFormatError("contributions_found needs contributions")
        ids = [item.contribution_id for item in self.contributions]
        if len(ids) != len(set(ids)):
            raise GoldFormatError("Duplicate contribution ID")
        if any(
            not identifier.startswith(f"{self.arxiv_id}:cand") for identifier in ids
        ):
            raise GoldFormatError("Contribution ID must use this arXiv ID")
        conflict_ids = [
            c.conflict_id
            for item in self.contributions
            for c in item.quantity_conflicts
        ]
        if len(conflict_ids) != len(set(conflict_ids)):
            raise GoldFormatError("Duplicate conflict ID")
        exclusion_ids = [item.exclusion_id for item in self.reviewed_exclusions]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise GoldFormatError("Duplicate exclusion ID")
        owner: dict[str, str] = {}
        for item in self.contributions:
            for identifier in item.identifiers:
                if (
                    identifier.value in owner
                    and owner[identifier.value] != item.contribution_id
                ):
                    raise GoldFormatError(
                        f"Identifier appears in multiple contributions: {identifier.value}"
                    )
                owner[identifier.value] = item.contribution_id
        return self


class CompactPaper(Config):
    arxiv_id: str = Field(pattern=r"^\d{4}\.\d{4,5}$")
    title: str = Field(min_length=1)
    version: str = Field(min_length=1)


class CompactAnnotationActors(Config):
    original_annotator: str = Field(min_length=1)
    original_annotated_at: str | None = None
    original_models: list[str] | None = None
    reviewer: str = Field(min_length=1)
    review_models: list[str] | None = None


class CompactReviewConfirmation(Config):
    value: Literal["checked", "complete", "partial"]
    basis: str = Field(min_length=1)
    record_sha256: str = Field(pattern=_SHA256_RE)
    reviewed_at: str | None = None
    differences: list[str] = Field(default_factory=list)


class CompactReview(Config):
    material: CompactReviewConfirmation
    scope: CompactReviewConfirmation
    answer_sha256: str = Field(pattern=_SHA256_RE)

    @model_validator(mode="after")
    def confirmation_types(self) -> CompactReview:
        if self.material.value != "checked" or self.scope.value not in {
            "complete",
            "partial",
        }:
            raise GoldFormatError(
                "Material/scope review values do not match their roles"
            )
        return self


class CompactRevision(Config):
    """Latest local correction; earlier revisions are retained by Git."""

    revised_by: str = Field(min_length=1)
    decided_by: str = Field(
        min_length=1,
        description="Human who authorized this correction; distinct from its preparer.",
    )
    revised_at: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    previous_answer_sha256: str = Field(pattern=_SHA256_RE)


class CompactGoldMetadata(Config):
    schema_tag: CompactSchemaTag = Field(alias="schema")
    paper: CompactPaper
    rules: GoldRulesRef
    files: list[GoldPdfFile] = Field(min_length=1)
    annotation: CompactAnnotationActors
    review: CompactReview
    revision: CompactRevision | None = None

    @model_validator(mode="after")
    def metadata_shape(self) -> CompactGoldMetadata:
        if self.schema_tag.name != COMPACT_METADATA_SCHEMA_NAME:
            raise GoldFormatError("Expected compact metadata schema")
        if len({item.file_id for item in self.files}) != len(self.files):
            raise GoldFormatError("Duplicate PDF file ID")
        if any(item.sha256 is None for item in self.files):
            raise GoldFormatError("Every final PDF file needs a fixed SHA-256")
        return self


@dataclass(frozen=True)
class CompactGoldPair:
    answer: CompactGoldAnswer
    metadata: CompactGoldMetadata
    contribution_id_map: dict[str, str] = field(default_factory=dict)


def _prune(value: object, *, key: str | None = None) -> object:
    """Drop optional null/empty members without erasing meaningful zero results."""
    if isinstance(value, list):
        return [_prune(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, object] = {}
    for name, item in value.items():
        if item is None and name != "value":
            continue
        if item == [] and name != "contributions":
            continue
        if item == "" and name == "quote":
            continue
        if item == "none" and name == "limit_kind":
            continue
        if item == {}:
            continue
        result[name] = _prune(item, key=name)
    return result


def compact_answer_to_json(answer: CompactGoldAnswer) -> str:
    return (
        json.dumps(_prune(answer.model_dump(mode="json")), ensure_ascii=False, indent=2)
        + "\n"
    )


def compact_metadata_to_json(metadata: CompactGoldMetadata) -> str:
    return (
        json.dumps(
            _prune(metadata.model_dump(mode="json", by_alias=True)),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_compact_gold_pair(pair: CompactGoldPair) -> None:
    answer = pair.answer
    metadata = pair.metadata
    if answer.arxiv_id != metadata.paper.arxiv_id:
        raise GoldIntegrityError("Answer and metadata arXiv IDs differ")
    if answer.coverage.value != metadata.review.scope.value:
        raise GoldIntegrityError("Answer coverage differs from documented scope review")
    if (
        _sha256(compact_answer_to_json(answer).encode("utf-8"))
        != metadata.review.answer_sha256
    ):
        raise GoldIntegrityError("Metadata is not bound to this answer content")
    known = {item.file_id for item in metadata.files}
    from .models import iter_gold_evidence

    unknown = sorted(
        {e.file_id for _, e in iter_gold_evidence(answer) if e.file_id not in known}
    )
    if unknown:
        raise GoldIntegrityError(f"Evidence references unknown PDF file IDs: {unknown}")


def pair_preparation_problems(pair: CompactGoldPair) -> list[CheckProblem]:
    """Pair-only preparation gaps that need no material files.

    Shared by :func:`check_compact_gold_pair` (merged with PDF hash,
    page and quote verification) and by in-memory scoring adaptation, which
    must not read PDFs.  Callers still run :func:`validate_compact_gold_pair`
    first: the structural binding checks are a precondition, not a
    replacement.  The problem set, categories, codes, subjects and messages
    match the corresponding blocks of the full checker.
    """
    from .checking import CheckProblem
    from .models import iter_gold_evidence

    validate_compact_gold_pair(pair)
    problems: list[CheckProblem] = []
    for subject, evidence in iter_gold_evidence(pair.answer):
        if evidence.file_id is None:
            problems.append(
                CheckProblem(
                    category="material",
                    code="evidence_unbound",
                    subject=subject,
                    message="Evidence must reference a fixed PDF",
                )
            )
    for contribution in pair.answer.contributions:
        subject = f"contributions/{contribution.contribution_id}"
        for name, claim in (
            ("contribution_type", contribution.contribution_type),
            ("contribution_summary", contribution.contribution_summary),
        ):
            if not claim.evidence:
                problems.append(
                    CheckProblem(
                        category="material",
                        code="missing_contribution_evidence",
                        subject=f"{subject}/{name}",
                        message="Scientific claim has no PDF evidence",
                    )
                )
        if contribution.paper_boundness.value is None:
            problems.append(
                CheckProblem(
                    category="science_pending",
                    code="boundness_unknown",
                    subject=f"{subject}/paper_boundness",
                    message="Paper boundness remains unresolved",
                )
            )
        elif (
            contribution.paper_boundness.value != "no_overall_conclusion"
            and not contribution.paper_boundness.evidence
        ):
            problems.append(
                CheckProblem(
                    category="material",
                    code="missing_boundness_evidence",
                    subject=f"{subject}/paper_boundness",
                    message="Boundness claim has no PDF evidence",
                )
            )
        for identifier in contribution.identifiers:
            if not identifier.evidence:
                problems.append(
                    CheckProblem(
                        category="material",
                        code="missing_identifier_evidence",
                        subject=f"{subject}/identifiers/{identifier.value}",
                        message="Identifier has no PDF evidence",
                    )
                )
        for group in contribution.quantities:
            for index, value in enumerate(group.values):
                if not value.evidence:
                    problems.append(
                        CheckProblem(
                            category="material",
                            code="missing_value_evidence",
                            subject=f"{subject}/quantities/{group.quantity}/{index}",
                            message="Quantity has no PDF evidence",
                        )
                    )
                if value.condition is None or not value.condition.strip():
                    problems.append(
                        CheckProblem(
                            category="material",
                            code="missing_condition",
                            subject=f"{subject}/quantities/{group.quantity}/{index}",
                            message="Quantity condition is missing",
                        )
                    )
        for conflict in contribution.quantity_conflicts:
            if not conflict.alternatives:
                problems.append(
                    CheckProblem(
                        category="science_pending",
                        code="conflict_structure_unknown",
                        subject=f"{subject}/quantity_conflicts/{conflict.conflict_id}",
                        message="Conflict alternatives have not been structured",
                    )
                )
            for index, alternative in enumerate(conflict.alternatives):
                if not alternative.evidence:
                    problems.append(
                        CheckProblem(
                            category="material",
                            code="missing_value_evidence",
                            subject=f"{subject}/quantity_conflicts/{conflict.conflict_id}/alternatives/{index}",
                            message="Conflict alternative has no PDF evidence",
                        )
                    )
    if pair.answer.coverage.value == "partial":
        problems.append(
            CheckProblem(
                category="science_pending",
                code="partial_coverage",
                subject="coverage",
                message="The accepted reference scope is partial",
            )
        )
    if pair.metadata.review.material.differences:
        problems.append(
            CheckProblem(
                category="science_pending",
                code="material_differences",
                subject="metadata/review/material/differences",
                message="Documented PDF differences must be covered by the applicable human review or decision",
            )
        )
    for index, item in enumerate(pair.answer.unresolved):
        problems.append(
            CheckProblem(
                category="science_pending",
                code=f"unresolved_{index + 1}",
                subject=item.subject,
                message=item.value,
            )
        )
    return problems


def check_compact_gold_pair(pair: CompactGoldPair, *, material_root: str | Path):
    """Check the exact v2 pair that prepare/save will publish."""
    from ..workspace.layout import resolve_within
    from .checking import (
        CheckProblem,
        FileVerification,
        GoldCheckReport,
        QuoteVerification,
    )
    from .models import iter_gold_evidence
    from .pdfcheck import analyze_pdf, quote_result

    validate_compact_gold_pair(pair)
    root = Path(material_root)
    problems: list[CheckProblem] = []
    files: list[FileVerification] = []
    quotes: list[QuoteVerification] = []
    analysis_by_id = {}
    for item in pair.metadata.files:
        path = resolve_within(root, item.name)
        data = path.read_bytes() if path.is_file() else None
        digest = _sha256(data) if data is not None else None
        analysis = analyze_pdf(data) if data is not None else None
        analysis_by_id[item.file_id] = analysis
        if data is None:
            problems.append(
                CheckProblem(
                    category="material",
                    code="file_missing",
                    subject=item.file_id,
                    message=f"PDF file missing: {item.name}",
                )
            )
        elif digest != item.sha256:
            problems.append(
                CheckProblem(
                    category="material",
                    code="file_hash_mismatch",
                    subject=item.file_id,
                    message=f"PDF hash mismatch: {item.name}",
                )
            )
        elif analysis is None or not analysis.openable:
            problems.append(
                CheckProblem(
                    category="material",
                    code="file_not_pdf",
                    subject=item.file_id,
                    message=f"PDF cannot be opened: {item.name}",
                )
            )
        files.append(
            FileVerification(
                file_id=item.file_id,
                name=item.name,
                sha256=item.sha256,
                exists=data is not None,
                hash_matches=None if data is None else digest == item.sha256,
                openable=None if analysis is None else analysis.openable,
                page_count=None
                if analysis is None or not analysis.openable
                else analysis.page_count,
                notes=[] if analysis is None else list(analysis.notes),
            )
        )
    for subject, evidence in iter_gold_evidence(pair.answer):
        analysis = analysis_by_id.get(evidence.file_id)
        # pair_preparation_problems uniformly reports missing evidence.file_id.
        if evidence.file_id is not None and (
            evidence.page is not None
            and analysis is not None
            and analysis.openable
            and evidence.page > analysis.page_count
        ):
            problems.append(
                CheckProblem(
                    category="material",
                    code="page_out_of_range",
                    subject=subject,
                    message=f"PDF page {evidence.page} is out of range",
                )
            )
        if analysis is not None and analysis.openable:
            result, note = quote_result(
                analysis.page_texts, page=evidence.page, quote=evidence.quote
            )
        else:
            result, note = "not_extractable", "PDF missing or unreadable"
        quotes.append(
            QuoteVerification(
                subject=subject,
                page=evidence.page,
                quote=evidence.quote,
                result=result,
                note=note,
            )
        )
    # In-memory structural/material/science preparation checks share logic with file verification.
    # Combined cases may change list order compared with inline block checks, but preserve the issue multiset,
    # including multiplicity, and every category/code/subject/message.
    problems.extend(pair_preparation_problems(pair))
    return GoldCheckReport(
        document_sha256=_sha256(compact_answer_to_json(pair.answer).encode("utf-8")),
        kind="answer",
        problems=problems,
        files=files,
        quotes=quotes,
        scope=[
            "Typed compact answer and metadata binding",
            "Fixed PDF hashes and quote locations",
            "Scientific validity remains subject to human review",
        ],
    )


def compact_gold_annotation(
    draft: GoldAnnotation,
    *,
    title: str,
    original_annotator: str,
    reviewer: str,
    material_review: CompactReviewConfirmation | dict,
    scope_review: CompactReviewConfirmation | dict,
    scope_limitations: list[CompactScopeLimitation | dict] | None = None,
    unresolved: list[CompactUnresolved | dict] | None = None,
    original_models: list[str] | None = None,
    review_models: list[str] | None = None,
    original_annotated_at: str | None = None,
    handled_pending_ids: list[str] | None = None,
) -> CompactGoldPair:
    """Convert frozen v1 content to v2 without manufacturing human review.

    A caller must account for every v1 pending item.  The separate conversion
    report should explain each disposition; the formal pair contains only the
    remaining scoring truth and documented review.
    """
    if not draft.paper.arxiv_id or not _ARXIV_RE.fullmatch(draft.paper.arxiv_id):
        raise GoldFormatError(
            "A modern arXiv ID is required for compact contribution IDs"
        )
    if not draft.paper.version:
        raise GoldFormatError("Paper version is required")
    if not draft.files or any(item.sha256 is None for item in draft.files):
        raise GoldFormatError("Fixed PDF files and hashes are required")
    pending = {item.item_id for item in draft.pending}
    handled = set(handled_pending_ids or [])
    if pending != handled:
        raise GoldFormatError(
            f"Every v1 pending ID needs a recorded disposition: missing={sorted(pending - handled)}, extra={sorted(handled - pending)}"
        )
    material = CompactReviewConfirmation.model_validate(material_review)
    scope = CompactReviewConfirmation.model_validate(scope_review)
    if material.value != "checked" or scope.value not in {"complete", "partial"}:
        raise GoldFormatError("Explicit material and scope confirmations are required")
    if draft.material_correspondence.differences != material.differences:
        raise GoldFormatError("Documented PDF differences must be preserved")
    limitations = [
        CompactScopeLimitation.model_validate(item)
        for item in (scope_limitations or [])
    ]
    if scope.value == "partial" and not limitations:
        raise GoldFormatError("Partial coverage requires named limitations")
    unresolved_items = [
        CompactUnresolved.model_validate(item) for item in (unresolved or [])
    ]
    id_map: dict[str, str] = {}
    contributions = []
    for index, old in enumerate(draft.contributions, 1):
        new_id = f"{draft.paper.arxiv_id}:cand{index:02d}"
        id_map[old.contribution_id] = new_id
        contributions.append(
            CompactContribution.model_validate(
                {
                    "contribution_id": new_id,
                    "identifiers": [
                        item.model_dump(mode="python") for item in old.identifiers
                    ],
                    "contribution_type": {
                        "value": old.contribution_type,
                        "evidence": [
                            e.model_dump(mode="python")
                            for e in old.contribution_evidence
                        ],
                    },
                    "contribution_summary": {
                        "value": old.contribution_summary,
                        "evidence": [
                            e.model_dump(mode="python")
                            for e in old.contribution_evidence
                        ],
                    },
                    "paper_boundness": {
                        "value": old.paper_boundness.status,
                        "evidence": [
                            e.model_dump(mode="python")
                            for e in old.paper_boundness.evidence
                        ],
                    },
                    "quantities": [
                        item.model_dump(mode="python") for item in old.quantities
                    ],
                    "quantity_conflicts": [
                        {
                            "conflict_id": item.conflict_id,
                            "quantity": item.quantity,
                            "reason": {
                                "value": item.reason,
                                "evidence": [
                                    e.model_dump(mode="python") for e in item.evidence
                                ],
                            },
                            "alternatives": [
                                v.model_dump(mode="python") for v in item.alternatives
                            ],
                        }
                        for item in old.quantity_conflicts
                    ],
                }
            )
        )
    answer = CompactGoldAnswer(
        arxiv_id=draft.paper.arxiv_id,
        status=draft.status,
        coverage=CompactCoverage(value=scope.value, limitations=limitations),
        contributions=contributions,
        reviewed_exclusions=[
            CompactExclusion.model_validate(
                {
                    "exclusion_id": item.exclusion_id,
                    "reason": {
                        "value": item.reason,
                        "evidence": [
                            e.model_dump(mode="python") for e in item.evidence
                        ],
                    },
                }
            )
            for item in draft.reviewed_exclusions
        ],
        unresolved=unresolved_items,
    )
    answer_sha = _sha256(compact_answer_to_json(answer).encode("utf-8"))
    metadata = CompactGoldMetadata.model_validate(
        {
            "schema": {
                "name": COMPACT_METADATA_SCHEMA_NAME,
                "version": COMPACT_METADATA_SCHEMA_VERSION,
            },
            "paper": {
                "arxiv_id": draft.paper.arxiv_id,
                "title": title,
                "version": draft.paper.version,
            },
            "rules": draft.rules.model_dump(mode="python"),
            "files": [item.model_dump(mode="python") for item in draft.files],
            "annotation": {
                "original_annotator": original_annotator,
                "original_annotated_at": original_annotated_at,
                "original_models": original_models or None,
                "reviewer": reviewer,
                "review_models": review_models or None,
            },
            "review": {
                "material": material.model_dump(mode="python"),
                "scope": scope.model_dump(mode="python"),
                "answer_sha256": answer_sha,
            },
        }
    )
    pair = CompactGoldPair(answer=answer, metadata=metadata, contribution_id_map=id_map)
    validate_compact_gold_pair(pair)
    return pair


def read_compact_gold_pair(path: str | Path) -> CompactGoldPair:
    path = Path(path)
    directory = path if path.is_dir() else path.parent
    answer_path = directory / "answer.json"
    metadata_path = directory / "metadata.json"
    try:
        answer_bytes = answer_path.read_bytes()
        metadata_bytes = metadata_path.read_bytes()
        answer = CompactGoldAnswer.model_validate(json.loads(answer_bytes))
        metadata = CompactGoldMetadata.model_validate(json.loads(metadata_bytes))
    except (OSError, ValueError) as exc:
        raise GoldFormatError(
            f"Invalid compact Gold pair in {directory}: {exc}"
        ) from exc
    pair = CompactGoldPair(answer=answer, metadata=metadata)
    validate_compact_gold_pair(pair)
    if _sha256(answer_bytes) != metadata.review.answer_sha256:
        raise GoldIntegrityError("On-disk answer bytes do not match metadata")
    return pair


def write_compact_gold_pair(
    pair: CompactGoldPair, directory: str | Path
) -> tuple[Path, Path]:
    validate_compact_gold_pair(pair)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("answer.json", compact_answer_to_json(pair.answer)),
        ("metadata.json", compact_metadata_to_json(pair.metadata)),
    ):
        path = directory / name
        temporary = directory / f".{name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    answer_path = directory / "answer.json"
    read_compact_gold_pair(answer_path)
    return answer_path, directory / "metadata.json"
