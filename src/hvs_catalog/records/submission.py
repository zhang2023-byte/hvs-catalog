"""Step-specific simplified science submissions with inline evidence.

Programs fill omitted optional values with null/empty lists; required lists stay
explicit even if empty. Programs assign IDs/evidence/references/runtime details; see
generated documentation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Config
from .paper import (
    ContributionType,
    CoverageState,
    IntervalKind,
    LimitKind,
    ValueSource,
)
from .questions import QuestionCategory
from .rules import AuthorStatus, is_quantity_path
from .source import Locator


class SubmissionError(ValueError):
    """Submission violates its format or contradicts supplied inputs."""


class InlineEvidencePart(Config):
    part: str = Field(min_length=1)
    raw_fragment: str = Field(
        min_length=1,
        description="The printed fragment of this component; must equal the filled "
        "numeric string of that component.",
    )


class InlineEvidence(Config):
    source: InlineSource
    parts: list[InlineEvidencePart] = Field(min_length=0)
    context: bool = False


class InlineSource(Config):
    file_id: str = Field(min_length=1)
    locator: Locator
    excerpt: str = Field(min_length=1)


class InlineQuestion(Config):
    category: QuestionCategory
    prompt: str = Field(min_length=1)
    evidence: list[InlineEvidence] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Roster extraction.
# ---------------------------------------------------------------------------


class InlineIdentifier(Config):
    value: str = Field(min_length=1)
    evidence: list[InlineEvidence] = Field(min_length=1)


class BoundnessSubmission(Config):
    status: AuthorStatus | None = None
    evidence: list[InlineEvidence] = Field(default_factory=list)
    question: InlineQuestion | None = None
    gap_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_or_reason(self) -> BoundnessSubmission:
        if self.status is None and self.question is None and not self.gap_ids:
            raise SubmissionError("尚不能判断作者状态时必须提交问题或引用材料缺口")
        needs_evidence = (
            self.status is not None and self.status != "no_overall_conclusion"
        )
        if needs_evidence and not self.evidence:
            raise SubmissionError("明确的作者判断必须就地给出原文证据")
        return self


class ConditionalAssessmentSubmission(Config):
    condition: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence: list[InlineEvidence] = Field(min_length=1)


class ContributionFieldsSubmission(Config):
    summary: SummarySubmission
    contribution_type: TypeSubmission
    boundness: BoundnessSubmission
    conditional_assessments: list[ConditionalAssessmentSubmission]


class SummarySubmission(Config):
    text: str = Field(min_length=1)
    evidence: list[InlineEvidence] = Field(min_length=1)


class TypeSubmission(Config):
    value: ContributionType
    evidence: list[InlineEvidence] = Field(min_length=1)


class RosterEntrySubmission(Config):
    identifiers: list[InlineIdentifier] = Field(min_length=1)
    disposition: Literal["included", "excluded", "pending"]
    evidence: list[InlineEvidence] = Field(default_factory=list)
    question: InlineQuestion | None = None
    contribution: ContributionFieldsSubmission | None = None
    gap_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _disposition_consistent(self) -> RosterEntrySubmission:
        if self.disposition == "included" and self.contribution is None:
            raise SubmissionError("收录条目必须填写贡献字段")
        if (
            self.disposition in {"excluded", "pending"}
            and self.contribution is not None
        ):
            raise SubmissionError("排除或待决条目不能填写贡献字段")
        if self.disposition in {"included", "excluded"} and not self.evidence:
            raise SubmissionError("收录或排除条目必须给出依据")
        if self.disposition == "pending" and self.question is None and not self.gap_ids:
            raise SubmissionError("待决条目必须提交问题或引用材料缺口")
        return self


class UnresolvedGroupSubmission(Config):
    original_range: str = Field(min_length=1)
    evidence: list[InlineEvidence] = Field(min_length=1)
    member_identifier_values: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class RosterCoverageSubmission(Config):
    state: CoverageState
    checked_file_ids: list[str] = Field(min_length=0)
    gap_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent_state(self) -> RosterCoverageSubmission:
        if self.state in {"partial", "complete"} and not self.checked_file_ids:
            raise SubmissionError("声明已检查或部分检查时必须列出已检查文件")
        if self.state == "not_checked" and self.checked_file_ids:
            raise SubmissionError("未检查的名单不能声明已检查文件")
        return self


class RosterSubmission(Config):
    coverage: RosterCoverageSubmission
    entries: list[RosterEntrySubmission]
    unresolved_groups: list[UnresolvedGroupSubmission]
    model_judgments: list[str] = Field(
        default_factory=list,
        description="Brief evidence-based interpretations when the active policy calls for model judgment.",
    )


# ---------------------------------------------------------------------------
# Quantity extraction.
# ---------------------------------------------------------------------------


class ReportedNumberSubmission(Config):
    value: str = Field(min_length=1)
    unit: str | None = None
    evidence: list[InlineEvidence] = Field(min_length=1)


class ErrorComponentSubmission(Config):
    name: str = Field(min_length=1)
    error: str | None = None
    lower_error: str | None = None
    upper_error: str | None = None
    evidence: list[InlineEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_error_form(self) -> ErrorComponentSubmission:
        if self.error is not None and (
            self.lower_error is not None or self.upper_error is not None
        ):
            raise SubmissionError("误差分量不能同时使用对称和非对称误差")
        if self.error is None and self.lower_error is None and self.upper_error is None:
            raise SubmissionError("误差分量必须提供误差数值")
        return self


class ValueSubmission(Config):
    """Values filled in applicable forms; programs complete omitted optional fields with
    null/empty lists.
    """

    value: str | None = None
    error: str | None = None
    lower_error: str | None = None
    upper_error: str | None = None
    unit: str | None = None
    limit_kind: LimitKind
    range_lower: str | None = None
    range_upper: str | None = None
    lower_inclusive: bool | None = None
    upper_inclusive: bool | None = None
    interval_kind: IntervalKind | None = None
    coverage: ReportedNumberSubmission | None = None
    lower_quantile: ReportedNumberSubmission | None = None
    upper_quantile: ReportedNumberSubmission | None = None
    error_components: list[ErrorComponentSubmission] = Field(default_factory=list)
    condition: str = Field(min_length=1)
    frame: str | None = None
    epoch: str | None = None
    coordinate_format: str | None = None
    representation: str | None = None
    purpose: str | None = None
    source: ValueSource
    source_note: str | None = None
    author_usage: str | None = None
    evidence: list[InlineEvidence] = Field(min_length=1)


class QuantityGroupSubmission(Config):
    quantity: str = Field(min_length=1)
    values: list[ValueSubmission] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_empty_group(self) -> QuantityGroupSubmission:
        if not is_quantity_path(self.quantity):
            raise SubmissionError(f"未知的物理量编码：{self.quantity}")
        return self


class ConflictSubmission(Config):
    quantity: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    alternatives: list[ValueSubmission] = Field(min_length=2)


class CoverageDeclarationBody(Config):
    state: CoverageState
    checked_file_ids: list[str]
    gap_ids: list[str] = Field(min_length=0)
    issue_ids: list[str] = Field(min_length=0)


class CoverageExceptionSubmission(CoverageDeclarationBody):
    quantity: str = Field(min_length=1)


class CoverageDeclaration(Config):
    scope: Literal["all_requested"]
    common: CoverageDeclarationBody
    exceptions: list[CoverageExceptionSubmission]


class PrecedentUseSubmission(Config):
    precedent_id: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ObjectQuantitySubmission(Config):
    object_id: str = Field(min_length=1)
    quantities: list[QuantityGroupSubmission]
    quantity_conflicts: list[ConflictSubmission]
    coverage: CoverageDeclaration
    precedent_uses: list[PrecedentUseSubmission] = Field(default_factory=list)


class ObjectQuestionSubmission(InlineQuestion):
    object_id: str = Field(min_length=1)
    quantity: str | None = None

    @model_validator(mode="after")
    def _known_quantity(self) -> ObjectQuestionSubmission:
        if self.quantity is not None and not is_quantity_path(self.quantity):
            raise SubmissionError(f"未知的物理量编码：{self.quantity}")
        return self


class QuantitySubmission(Config):
    objects: list[ObjectQuantitySubmission]
    questions: list[ObjectQuestionSubmission]
    model_judgments: list[str] = Field(default_factory=list)
