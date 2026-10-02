"""Synthetic scoring regressions for value comparison and merge precedence.

Cover one-to-one L2 pools for duplicate definite identities, full restricted numeric
grammar beyond character allowlists, actual structured unit/value/component arguments,
and definite different taking precedence over unknown/technical failures that no longer
affect scores while retaining failure facts. Exercise full score_batch or actual
comparators, reproducing counterexamples before checking combined invariants.
"""

from __future__ import annotations

import copy

import pytest
from scoring_sample_helpers import (
    DIST,
    M1,
    M2,
    RV,
    STD,
    load_bundle,
)
from scoring_sample_helpers import (
    build_single_quantity_bundle as _bundle,
)
from scoring_sample_helpers import (
    make_fragment as _fragment,
)
from scoring_sample_helpers import (
    score_with_protocol_only as _score,
)

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    build_measured_value,
    gold_input_digest,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.layers import content_key
from hvs_catalog.evaluation.scoring.models import (
    GoldObjectEntry,
    GoldPaperEntry,
    GoldQuantityEntry,
    GoldValueEntry,
    PredictionObjectEntry,
    PredictionPaperEntry,
    PredictionQuantityEntry,
    PredictionValueEntry,
)
from hvs_catalog.evaluation.scoring.printed import compare_number

HELI = "heliocentric"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


# ---------------------------------------------------------------------------
# One-to-one L2 record pools for duplicate predictions with definite identities.
# ---------------------------------------------------------------------------


def test_s08_duplicate_pool_order_and_label_invariant():
    """Frozen S08 values 5.0/9.9 retain results under reversed order or renamed internal
    IDs.
    """
    plan, gold, preds, auxiliary = load_bundle("synthetic-s08")
    forward = score_batch(plan, gold, preds, auxiliary=auxiliary)
    reversed_preds = copy.deepcopy(preds)
    reversed_preds[0].objects = list(reversed(reversed_preds[0].objects))
    backward = score_batch(plan, gold, reversed_preds, auxiliary=auxiliary)
    renamed_preds = copy.deepcopy(preds)
    renamed_preds[0].objects[0].quantities[0].values[0].record_id = "z9"
    renamed_preds[0].objects[1].quantities[0].values[0].record_id = "z8"
    renamed = score_batch(plan, gold, renamed_preds, auxiliary=auxiliary)
    for report in (forward, backward, renamed):
        paper = report.papers[0]
        assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
        assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
        assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
        assert paper.l2.f1 == pytest.approx(2 / 3)
        assert report.batch_provisional is False
        statuses = [row.status for row in _l2_rows(paper)]
        assert "matched" in statuses and "extra_value" in statuses


def test_duplicate_pool_complementary_quantities():
    """Duplicate entries with complementary quantities merge into pools where both
    quantities match one-to-one without losing P/G.
    """
    plan, gold, preds = _bundle(
        [
            ("g1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, []),
            ("g2", STD, {"value": "600", "error": "20", "unit": "km s^{-1}"}, []),
        ],
        [("p1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, [])],
        # Use the specialized multi-quantity builder below.
    )
    # _bundle handles one quantity; use explicit two-quantity input here.
    gold[0].objects[0].quantities.append(
        GoldQuantityEntry(
            quantity_path=RV,
            values=[
                GoldValueEntry(
                    record_id="g2",
                    value=build_measured_value(
                        value_id="g2",
                        value="600",
                        error="20",
                        unit="km s⁻¹",
                        condition=STD,
                        representation=HELI,
                    ),
                )
            ],
        )
    )
    del gold[0].objects[0].quantities[0].values[1]
    preds[0].objects[0].quantities.append(
        PredictionQuantityEntry(
            quantity_path=RV,
            values=[
                PredictionValueEntry(
                    record_id="p2",
                    value=build_measured_value(
                        value_id="p2",
                        value="600",
                        error="20",
                        unit="km s⁻¹",
                        condition=STD,
                        representation=HELI,
                    ),
                )
            ],
        )
    )
    dup = PredictionObjectEntry(
        paper_key=preds[0].paper_key,
        label="A-dup",
        identifiers=["SYN-A"],
        quantities=[],
    )
    preds[0].objects.append(dup)
    # Put RV into the duplicate object to simulate complementary quantity reporting.
    preds[0].objects[1].quantities = preds[0].objects[0].quantities[1:]
    preds[0].objects[0].quantities = preds[0].objects[0].quantities[:1]
    plan = plan.model_copy(
        update={
            "gold_ref": plan.gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
            "scopes": [
                scope.model_copy(update={"quantity_paths": [DIST, RV]})
                for scope in plan.scopes
            ],
        }
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (2, 2, 2)
    assert paper.l2.precision == 1.0 and paper.l2.recall == 1.0
    assert report.batch_provisional is False


def test_duplicate_pool_extra_values_consume_gold_once():
    """Extra duplicate values consume Gold once while every prediction enters P."""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
        [
            ("p1", STD, {"value": "5.0", "error": "0.2", "unit": "kpc"}, []),
            ("p2", STD, {"value": "6.0", "error": "0.2", "unit": "kpc"}, []),
        ],
    )
    # Put the second record in the duplicate object entry.
    second = preds[0].objects[0].quantities[0].values.pop()
    dup = PredictionObjectEntry(
        paper_key=preds[0].paper_key,
        label="A-dup",
        identifiers=["SYN-A"],
        quantities=[PredictionQuantityEntry(quantity_path=DIST, values=[second])],
    )
    preds[0].objects.append(dup)
    third = PredictionValueEntry(
        record_id="p3",
        value=build_measured_value(
            value_id="p3",
            value="5.0",
            error="0.2",
            unit="kpc",
            condition=STD,
            representation=HELI,
        ),
    )
    preds[0].objects[1].quantities[0].values.append(third)
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 3, 1)
    assert paper.l2.precision == pytest.approx(1 / 3)
    assert paper.l2.recall == 1.0 and paper.l2.f1 == pytest.approx(1 / 2)
    assert report.batch_provisional is False
    assert sum(1 for row in _l2_rows(paper) if row.status == "matched") == 1


def test_duplicate_pool_condition_different_candidate():
    """Same-identity candidates with different conditions cannot consume Gold twice;
    diagnose differences accurately.
    """
    plan, gold, preds = _bundle(
        [("g1", M1, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
        [("p1", M1, {"value": "5.0", "error": "0.2", "unit": "kpc"}, [])],
    )
    dup_value = PredictionValueEntry(
        record_id="p2",
        value=build_measured_value(
            value_id="p2",
            value="5.0",
            error="0.2",
            unit="kpc",
            condition=M2,
            representation=HELI,
        ),
    )
    preds[0].objects.append(
        PredictionObjectEntry(
            paper_key=preds[0].paper_key,
            label="A-dup",
            identifiers=["SYN-A"],
            quantities=[
                PredictionQuantityEntry(quantity_path=DIST, values=[dup_value])
            ],
        )
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
    assert report.batch_provisional is False
    assert any(row.status == "attribution_mismatch" for row in _l2_rows(paper))


def test_ambiguity_not_merged_into_duplicate_pool():
    """True shared-alias identity ambiguity remains pending outside pools; values cannot
    disambiguate it.
    """
    key = "synthetic-ambiguous-duplicates"
    common_kwargs = {"condition": STD, "representation": HELI}

    def measured(record_id, value):
        return build_measured_value(
            value_id=record_id,
            value=value,
            error="0.2",
            unit="kpc",
            **common_kwargs,
        )

    gold = [
        GoldPaperEntry(
            paper_key=key,
            coverage="complete",
            objects=[
                GoldObjectEntry(
                    label="A",
                    identifiers=["SYN-A", "SYN-SHARED"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g1", value=measured("g1", "5.0")
                                )
                            ],
                        )
                    ],
                ),
                GoldObjectEntry(
                    label="B",
                    identifiers=["SYN-B", "SYN-SHARED"],
                    quantities=[
                        GoldQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                GoldValueEntry(
                                    record_id="g3", value=measured("g3", "10")
                                )
                            ],
                        )
                    ],
                ),
            ],
        )
    ]
    predictions = [
        PredictionPaperEntry(
            paper_key=key,
            delivered=True,
            format_valid=True,
            completion="complete",
            objects=[
                PredictionObjectEntry(
                    paper_key=key,
                    label="A",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p1", value=measured("p1", "5.0")
                                )
                            ],
                        )
                    ],
                ),
                PredictionObjectEntry(
                    paper_key=key,
                    label="A-dup",
                    identifiers=["SYN-A"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p2", value=measured("p2", "9.9")
                                )
                            ],
                        )
                    ],
                ),
                PredictionObjectEntry(
                    paper_key=key,
                    label="P",
                    identifiers=["SYN-SHARED"],
                    quantities=[
                        PredictionQuantityEntry(
                            quantity_path=DIST,
                            values=[
                                PredictionValueEntry(
                                    record_id="p4", value=measured("p4", "5.0")
                                )
                            ],
                        )
                    ],
                ),
            ],
        )
    ]
    plan = load_bundle("synthetic-s08")[0].model_copy(
        update={
            "paper_keys": [key],
            "scopes": [
                load_bundle("synthetic-s08")[0]
                .scopes[0]
                .model_copy(update={"paper_key": key, "quantity_paths": [DIST]})
            ],
            "gold_ref": load_bundle("synthetic-s08")[0].gold_ref.model_copy(
                update={"fingerprint": gold_input_digest(gold)}
            ),
        }
    )
    report = score_batch(plan, gold, predictions, auxiliary=protocol_only_config())
    paper = report.papers[0]
    # Definite-identity pools still match one-to-one: 5.0 in A and A-dup is correct.
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert paper.l1.pending == 2  # Ambiguous P and pending Gold B stay outside pools.
    assert paper.l1.precision is None
    # Count ambiguous values in P/G and retain unresolved rows, without pool matching or missing/extra labels.
    statuses = [row.status for row in _l2_rows(paper)]
    assert statuses.count("unresolved_object_identity") == 2
    assert "matched" in statuses
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 3, 2)
    assert paper.l2.precision is None
    assert report.batch_provisional is True


# ---------------------------------------------------------------------------
# Full restricted numeric grammar: a character allowlist does not establish valid syntax.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("8.2e+", "8.2e3"),  # Incomplete exponent.
        ("8.2e3", "8.2e+"),
        ("10^{", "10"),  # Unmatched parentheses or broken power syntax.
        ("8.2*10^{3", "8.2*10^{3}"),
        (
            "8.2e+",
            "8.2e+",
        ),  # Identical malformed syntax on both sides still cannot establish equivalence.
        ("1.2e", "1.2"),
        ("12h34", "12h34m"),  # Broken sexagesimal chain with a missing marker.
    ],
)
def test_malformed_number_syntax_is_undecidable(gold, prediction):
    assert compare_number(gold, prediction) == "undecidable"


@pytest.mark.parametrize(
    ("gold", "prediction", "expected"),
    [
        ("8.2e3", "8.2e3", "equivalent"),
        ("8.2E-3", "8.2E-3", "equivalent"),
        ("1.5*10^{3}", "1.5*10^{3}", "equivalent"),
        ("8.2e3", "8.2e4", "different"),
        ("1.5*10^{3}", "1.5*10^{-3}", "different"),
    ],
)
def test_complete_number_syntax_stays_determined(gold, prediction, expected):
    assert compare_number(gold, prediction) == expected


def test_incomplete_exponent_not_mismatched_in_score_batch():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e3", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision is None  # Cannot assign a final zero.
    assert report.batch_provisional is True
    assert any(
        row.status == "unresolved_value" and row.fields == ["value"]
        for row in _l2_rows(paper)
    )


def test_identical_malformed_syntax_not_confirmed_equivalent():
    """A key shortcut must not certify malformed syntax as equivalent; identical malformed
    sides remain unknown.
    """
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e+", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_value" for row in _l2_rows(paper))


def test_content_key_rejects_malformed_syntax():
    measured = build_measured_value(
        value_id="k",
        value="8.2e+",
        error="0.3",
        unit="kpc",
        condition=STD,
        representation=HELI,
    )
    assert content_key(measured) is None
    valid = build_measured_value(
        value_id="k",
        value="8.2",
        error="0.3",
        unit="kpc",
        condition=STD,
        representation=HELI,
    )
    assert content_key(valid) is not None


def test_complete_syntax_differences_stay_final():
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2e3", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "8.2e4", "error": "0.3", "unit": "kpc"}, [])],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False
    assert any(row.status == "value_mismatch" for row in _l2_rows(paper))


# ---------------------------------------------------------------------------
# Pass actual compared contents from structured fields.
# ---------------------------------------------------------------------------


def _report_bundle(report_field, gold_pair, pred_pair, fragments=("coverage.unit",)):
    """Structured reported-field variant: gold_pair/pred_pair are (value, unit)."""
    base = {"value": "8.2", "error": "0.3", "unit": "kpc"}
    gold_fragments = [_fragment("gf", f"gold {report_field} text", list(fragments))]
    pred_fragments = [_fragment("pf", f"pred {report_field} text", list(fragments))]
    return _bundle(
        [
            (
                "g1",
                STD,
                {**base, report_field: gold_pair},
                gold_fragments,
            )
        ],
        [("p1", STD, {**base, report_field: pred_pair}, pred_fragments)],
    )


@pytest.mark.parametrize(
    ("report_field", "gold_pair", "pred_pair", "undecided_sub"),
    [
        ("coverage", ("95", "km s^{-1}"), ("95", "km/s"), "unit"),
        ("lower_quantile", ("2.5", "km s^{-1}"), ("2.5", "km/s"), "unit"),
        ("upper_quantile", ("97.5", "m s^{-1}"), ("97.5", "m/s"), "unit"),
        ("coverage", ("95", "kpc"), ("\\mystery{95}", "kpc"), "value"),
        ("lower_quantile", ("2.5", "kpc"), ("\\mystery{2.5}", "kpc"), "value"),
        ("upper_quantile", ("97.5", "kpc"), ("\\mystery{97.5}", "kpc"), "value"),
    ],
)
def test_report_requests_carry_real_field_content(
    report_field, gold_pair, pred_pair, undecided_sub
):
    plan, gold, preds = _report_bundle(
        report_field,
        gold_pair,
        pred_pair,
        fragments=(f"{report_field}.{undecided_sub}",),
    )
    seen = []

    def handler(request):
        seen.append((request.field_name, request.left, request.right))
        return AgentResponse(
            verdict="different",
            rationale="synthetic",
            source_citations=["left:gf"],
        )

    mock = MockAgentAdapter(handler)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert len(seen) == 1
    field_name, left, right = seen[0]
    assert field_name == f"{report_field}.{undecided_sub}"
    # Send actual compared content: .unit supplies units and .value supplies reported values.
    assert left == (gold_pair[1] if undecided_sub == "unit" else gold_pair[0])
    assert right == (pred_pair[1] if undecided_sub == "unit" else pred_pair[0])
    paper = report.papers[0]
    assert (
        paper.l2.precision == 0.0
    )  # Explicit different establishes a definite mismatch.
    assert report.batch_provisional is False


def test_component_path_requests_carry_named_subfield():
    """Named error components supply the actual subfield selected by name and side."""
    base = {"value": "8.2", "unit": "kpc"}
    gold_fragments = [_fragment("gf", "sys err text", ["error_components.sys.error"])]
    pred_fragments = [_fragment("pf", "macro def", ["error_components.sys.error"])]
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {**base, "error_components": [{"name": "sys", "error": "0.3"}]},
                gold_fragments,
            )
        ],
        [
            (
                "p1",
                STD,
                {
                    **base,
                    "error_components": [{"name": "sys", "error": "\\mystery{0.3}"}],
                },
                pred_fragments,
            )
        ],
    )
    seen = []

    def handler(request):
        seen.append((request.field_name, request.left, request.right))
        return AgentResponse(
            verdict="equivalent", rationale="synthetic", source_citations=["left:gf"]
        )

    mock = MockAgentAdapter(handler)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert seen == [("error_components.sys.error", "0.3", "\\mystery{0.3}")]
    paper = report.papers[0]
    assert paper.l2.correct == 1 and paper.l2.precision == 1.0
    assert report.batch_provisional is False


def test_missing_component_subfield_needs_no_agent():
    """A missing component subfield on one side is a definite programmatic difference
    needing no Agent.
    """
    base = {"value": "8.2", "unit": "kpc"}
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {**base, "error_components": [{"name": "sys", "error": "0.3"}]},
                [_fragment("gf", "text", ["error_components"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {
                    **base,
                    "error_components": [{"name": "sys", "lower_error": "0.3"}],
                },
                [_fragment("pf", "text", ["error_components"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


# ---------------------------------------------------------------------------
# Component merge precedence: definite different wins.
# ---------------------------------------------------------------------------


def test_insufficient_then_determined_different_wins():
    """Missing value evidence plus explicitly different error yields a final mismatch, not
    provisional.
    """
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "error only", ["error"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "error only", ["error"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert [c["field_name"] for c in mock.calls] == [
        "error"
    ]  # Call only for evidence-supported fields.
    assert any(
        row.status == "value_mismatch" and row.fields == ["error"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_technical_failure_then_determined_different_wins():
    """Value technical failure plus explicitly different error yields a final mismatch
    while retaining failure facts.
    """
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "both", ["value", "error"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "both", ["value", "error"])],
            )
        ],
    )

    def fail_value_then_different(request):
        if request.field_name == "value":
            raise RuntimeError("simulated timeout")
        return AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )

    mock = MockAgentAdapter(fail_value_then_different)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert any(
        row.status == "value_mismatch" and row.fields == ["error"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False  # The failure no longer affects the score.
    assert report.auxiliary.technical_failures == 1  # Retain failure facts.


def test_reversed_field_combination_determined_different_wins():
    """Missing error evidence plus explicitly different value, in reverse order, yields a
    final mismatch.
    """
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value only", ["value"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [_fragment("pf", "value only", ["value"])],
            )
        ],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="different", rationale="x", source_citations=["left:gf"]
        )
    )
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert [c["field_name"] for c in mock.calls] == ["value"]
    assert any(
        row.status == "value_mismatch" and row.fields == ["value"]
        for row in _l2_rows(paper)
    )
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_all_unknown_stays_provisional():
    """All fields unknown with no definite difference remain provisional."""
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{9.9}", "error": "\\mystery{0.3}", "unit": "kpc"},
                [],
            )
        ],
    )
    report = _score(plan, gold, preds)
    paper = report.papers[0]
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert any(row.status == "unresolved_value" for row in _l2_rows(paper))


def test_program_difference_needs_no_agent():
    """Programmatically established differences need no Agent to overturn them."""
    plan, gold, preds = _bundle(
        [("g1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, [])],
        [("p1", STD, {"value": "9.9", "error": "\\mystery{0.3}", "unit": "kpc"}, [])],
    )
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    report = _score(plan, gold, preds, agent_adapter=mock)
    assert mock.calls == []
    paper = report.papers[0]
    assert paper.l2.precision == 0.0
    assert report.batch_provisional is False


def test_unaffecting_failure_does_not_pollute_batch():
    """Retain failures on candidates irrelevant to final matching without making the batch
    provisional.
    """
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value text", ["value"])],
            )
        ],
        [
            ("p1", STD, {"value": "8.2", "error": "0.3", "unit": "kpc"}, []),
            (
                "p2",
                STD,
                {"value": "\\mystery{8.2}", "error": "0.3", "unit": "kpc"},
                [_fragment("pf", "macro def", ["value"])],
            ),
        ],
    )

    def always_fail(request):
        raise RuntimeError("simulated timeout")

    mock = MockAgentAdapter(always_fail)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5
    assert report.batch_provisional is False
    assert report.auxiliary.technical_failures == 1  # Still retain failure facts.
    statuses = [row.status for row in _l2_rows(paper)]
    assert "matched" in statuses and "unknown_candidate_extra" in statuses


def test_affecting_failure_still_provisional():
    """Score-affecting failures remain provisional; precedence must not hide actual
    unresolved cases.
    """
    plan, gold, preds = _bundle(
        [
            (
                "g1",
                STD,
                {"value": "8.2", "error": "0.3", "unit": "kpc"},
                [_fragment("gf", "value text", ["value"])],
            )
        ],
        [
            (
                "p1",
                STD,
                {"value": "\\mystery{8.2}", "error": "0.3", "unit": "kpc"},
                [_fragment("pf", "macro def", ["value"])],
            )
        ],
    )

    def always_fail(request):
        raise RuntimeError("simulated timeout")

    mock = MockAgentAdapter(always_fail)
    report = score_batch(
        plan, gold, preds, auxiliary=protocol_only_config(), agent_adapter=mock
    )
    paper = report.papers[0]
    assert paper.l2.correct == 0
    assert paper.l2.precision is None
    assert report.batch_provisional is True
    assert report.auxiliary.technical_failures == 1
