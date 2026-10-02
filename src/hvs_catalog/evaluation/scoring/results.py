"""Scoring reports: per-paper and micro L0/L1/L2, pairings, and diagnostics.

Zero-denominator rates are None/NA, never one. Score-affecting L2 unknowns/technical
failures make batches provisional, retaining definite counts with null final L2 rates.
correct_empty separately marks complete correct empty rosters, never failed/undelivered
output. Contribution types and author assessments enter diagnostics only, not quality.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from hvs_catalog.records.common import Config


def ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def l1_rates(
    tp: int, fp: int, fn: int, pending: int = 0
) -> tuple[float | None, float | None, float | None]:
    if pending:
        return None, None, None
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = ratio(2 * tp, 2 * tp + fp + fn)
    return precision, recall, f1


def l2_rates(
    correct: int, predicted: int, gold: int
) -> tuple[float | None, float | None, float | None]:
    precision = ratio(correct, predicted)
    recall = ratio(correct, gold)
    f1 = ratio(2 * correct, predicted + gold)
    return precision, recall, f1


class L0Metrics(Config):
    planned: int
    delivered: int
    format_valid: int
    completion_counts: dict[str, int]
    delivered_over_planned: float | None
    valid_over_delivered: float | None
    valid_over_planned: float | None


class L1Metrics(Config):
    tp: int
    fp: int
    fn: int
    pending: int = 0
    precision: float | None
    recall: float | None
    f1: float | None


class L2Metrics(Config):
    correct: int
    predicted: int
    gold: int
    precision: float | None
    recall: float | None
    f1: float | None


class QuantityBreakdown(Config):
    quantity_path: str
    metrics: L2Metrics


class PairingDetail(Config):
    """Diagnostic locator for one pairing attempt; internal IDs locate records, not
    scientific identities.
    """

    layer: Literal["l1", "l2"]
    status: str
    gold_ref: str | None = None
    prediction_ref: str | None = None
    fields: list[str] = Field(default_factory=list)


class Diagnostic(Config):
    code: str
    message: str
    refs: list[str] = Field(default_factory=list)


class PaperResult(Config):
    paper_key: str
    readiness: Literal["ready", "scoped", "blocked"]
    readiness_gaps: list[str] = Field(default_factory=list)
    scoped: bool = False
    scope_note: str | None = None
    l0: L0Metrics
    correct_empty: bool = False
    l1: L1Metrics | None = None
    l2: L2Metrics | None = None
    l2_provisional: bool = False
    quantity_breakdown: list[QuantityBreakdown] = Field(default_factory=list)
    prediction_errors: list[str] = Field(default_factory=list)
    pairings: list[PairingDetail] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    unresolved: list[Diagnostic] = Field(default_factory=list)
    evidence_verification: dict[str, int] = Field(default_factory=dict)


class BatchMicro(Config):
    """Micro aggregation; partial coverage means readiness gaps and incomplete counts, so
    rates cannot be final whole-batch rates and remain null.
    """

    coverage: Literal["full", "partial"] = "full"
    l0: L0Metrics
    l1: L1Metrics | None = None
    l2: L2Metrics | None = None


class JudgmentSummary(Config):
    entry_key: str
    request_kind: str
    field_name: str
    verdict: str | None
    status: str
    reused: bool


class AuxiliarySummary(Config):
    enabled: bool
    protocol_only: bool
    judgments: list[JudgmentSummary] = Field(default_factory=list)
    technical_failures: int = 0


class ScoringReport(Config):
    digests: dict[str, str]
    references: dict[str, dict[str, str]]
    papers: list[PaperResult]
    micro: BatchMicro
    batch_provisional: bool
    readiness_blocked_papers: list[str] = Field(default_factory=list)
    provisional_reasons: list[str] = Field(default_factory=list)
    correct_empty_papers: list[str] = Field(default_factory=list)
    auxiliary: AuxiliarySummary
    notes: list[str] = Field(default_factory=list)
