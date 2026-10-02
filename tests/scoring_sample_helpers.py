"""Shared loading and construction helpers for frozen scoring examples and regression
tests.

Frozen inputs under ``tests/fixtures/scoring/inputs/`` use descriptive filenames, with
manually calculated expectations in ``expected/expected.json``. Index by each file's
``sample_id``, without inferring identifiers from filenames.

Regression helpers provide minimal batch inputs for one object and quantity, evidence
fragments, and scoring entry points, avoiding imports between test modules. All
materials are fictional; never derive expectations by running the scorer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hvs_catalog.evaluation.scoring import (
    AuxiliaryScoringConfig,
    EvidenceFragment,
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
    ScoringPlan,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scoring"
INPUTS = FIXTURES / "inputs"
EXPECTED = json.loads((FIXTURES / "expected" / "expected.json").read_text("utf-8"))

# Filenames describe input roles; sample identifiers come from sample_id inside each file.
_BUNDLE_FILES = {
    json.loads(path.read_text("utf-8"))["sample_id"]: path
    for path in sorted(INPUTS.glob("*.json"))
}
SAMPLE_IDS = sorted(_BUNDLE_FILES)


def sample_path(sample_id: str) -> Path:
    return _BUNDLE_FILES[sample_id]


def load_bundle(sample_id: str):
    data = json.loads(sample_path(sample_id).read_text("utf-8"))
    return (
        ScoringPlan.model_validate(data["plan"]),
        [GoldPaperEntry.model_validate(entry) for entry in data["gold"]],
        [PredictionPaperEntry.model_validate(entry) for entry in data["predictions"]],
        AuxiliaryScoringConfig.model_validate(data["auxiliary"]),
    )


def score_sample(sample_id: str):
    plan, gold, predictions, auxiliary = load_bundle(sample_id)
    return score_batch(plan, gold, predictions, auxiliary=auxiliary)


# ---- Compare expected score strings with report rows ----


def parse_rate(value):
    """Parse expected score strings (1/3, 0, 1, NA, and null) into comparable values."""
    if value is None or value == "NA":
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return pytest.approx(int(numerator) / int(denominator))
    return pytest.approx(float(value))


def rows_of(paper):
    return sorted(
        (
            row.layer,
            row.status,
            row.gold_ref or "",
            row.prediction_ref or "",
            ",".join(row.fields),
        )
        for row in paper.pairings
    )


def expected_rows(expected_paper):
    return sorted(
        (layer, status, gold_ref or "", prediction_ref or "", fields)
        for layer, status, gold_ref, prediction_ref, fields in expected_paper[
            "pairings"
        ]
    )


def check_l0(actual, expected):
    assert actual.planned == expected["planned"]
    assert actual.delivered == expected["delivered"]
    assert actual.format_valid == expected["format_valid"]
    assert actual.completion_counts == expected["completion"]
    assert actual.delivered_over_planned == parse_rate(
        expected["delivered_over_planned"]
    )
    assert actual.valid_over_delivered == parse_rate(expected["valid_over_delivered"])
    assert actual.valid_over_planned == parse_rate(expected["valid_over_planned"])


def check_metric(actual, expected, count_keys):
    if actual is None or expected is None:
        assert actual is None and expected is None
        return
    for key in count_keys:
        assert getattr(actual, key) == expected[key], key
    for rate in ("precision", "recall", "f1"):
        assert getattr(actual, rate) == parse_rate(expected[rate]), rate


# ---- Shared synthetic regression builders: one object and quantity ----

DIST = "observed_phase_space.distance"
RV = "observed_phase_space.radial_velocity"
STD = "as reported in the paper"
HELI = "heliocentric"
M1 = "under Galactic potential M1"
M2 = "under Galactic potential M2"
M1_REWRITE = "under Galactic potential M1 (same adopted model)"
MATERIALS_NAME = "synthetic-materials"
MATERIALS_FP = hashlib.sha256(b"synthetic-materials").hexdigest()

#: Fictional paper key shared by regression builders, distinct from frozen example keys.
REGRESSION_PAPER_KEY = "synthetic-regression"


def make_value(value_id, **kwargs):
    kwargs.setdefault("condition", STD)
    kwargs.setdefault("representation", HELI)
    return build_measured_value(value_id=value_id, **kwargs)


def make_fragment(fragment_id, text, supports):
    return EvidenceFragment(
        fragment_id=fragment_id,
        text=text,
        location=f"synthetic-paper.tex#{fragment_id}",
        material_name=MATERIALS_NAME,
        material_fingerprint=MATERIALS_FP,
        supports_fields=list(supports),
    )


def build_single_quantity_bundle(gold_values, pred_values):
    """Minimal batch input for one object and quantity. gold_values/pred_values contain
    (record_id, condition, MeasuredValue parameters, fragment list) tuples.
    """

    def gold_entries():
        return [
            GoldValueEntry(
                record_id=record_id,
                value=make_value(record_id, condition=condition, **params),
                evidence_fragments=list(fragments),
            )
            for record_id, condition, params, fragments in gold_values
        ]

    def pred_entries():
        return [
            PredictionValueEntry(
                record_id=record_id,
                value=make_value(record_id, condition=condition, **params),
                evidence_fragments=list(fragments),
            )
            for record_id, condition, params, fragments in pred_values
        ]

    gold = [
        GoldPaperEntry(
            paper_key=REGRESSION_PAPER_KEY,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        GoldQuantityEntry(quantity_path=DIST, values=gold_entries())
                    ],
                )
            ],
        )
    ]
    predictions = [
        PredictionPaperEntry(
            paper_key=REGRESSION_PAPER_KEY,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key=REGRESSION_PAPER_KEY,
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST, values=pred_entries()
                        )
                    ],
                )
            ],
        )
    ]
    plan = load_bundle("synthetic-s06b8")[0]
    plan = plan.model_copy(
        update={
            "paper_keys": [REGRESSION_PAPER_KEY],
            "scopes": [
                plan.scopes[0].model_copy(
                    update={
                        "paper_key": REGRESSION_PAPER_KEY,
                        "quantity_paths": [DIST],
                    }
                )
            ],
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
            "materials_ref": plan.materials_ref.model_copy(
                update={"name": MATERIALS_NAME, "fingerprint": MATERIALS_FP}
            ),
        }
    )
    return plan, gold, predictions


def score_with_protocol_only(plan, gold, preds, **kwargs):
    return score_batch(plan, gold, preds, auxiliary=protocol_only_config(), **kwargs)
