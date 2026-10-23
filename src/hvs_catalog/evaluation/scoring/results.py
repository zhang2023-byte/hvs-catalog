"""Deterministic counts, rates, record paths and numerical differences."""

from typing import Literal

from pydantic import Field

from ...records.common import Config


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


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


def l1_metrics(tp, fp, fn):
    return L1Metrics(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=ratio(tp, tp + fp),
        recall=ratio(tp, tp + fn),
        f1=ratio(2 * tp, 2 * tp + fp + fn),
    )


def l2_metrics(correct, predicted, gold):
    return L2Metrics(
        correct=correct,
        predicted=predicted,
        gold=gold,
        precision=ratio(correct, predicted),
        recall=ratio(correct, gold),
        f1=ratio(2 * correct, predicted + gold),
    )


class RosterDetails(Config):
    matched_stars: int = 0
    contribution_correct: int = 0
    contribution_accuracy: float | None = None
    missing_identifiers: int = 0
    extra_identifiers: int = 0


class Pairing(Config):
    layer: Literal["star", "identifier", "contribution_type", "value"]
    status: Literal[
        "matched", "mismatch", "missing", "extra", "duplicate", "ambiguous_identity"
    ]
    gold_path: str | None = None
    prediction_path: str | None = None
    quantity: str | None = None
    fields: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)


class Metrics(Config):
    l0: L0Metrics
    l1: L1Metrics
    l2: L2Metrics | None
    roster_details: RosterDetails


class PaperResult(Metrics):
    arxiv_id: str
    version: str
    execution_state: str
    correct_empty: bool
    quantities: dict[str, L2Metrics]
    pairings: list[Pairing]
    errors: list[str]
    evidence_checks: dict[str, int]


class ScoringReport(Config):
    format: Literal["hvs.scoring-report"] = "hvs.scoring-report"
    version: Literal[2] = 2
    input_sha256: str
    scheme: dict
    papers: list[PaperResult]
    micro: Metrics
    notes: list[str]
