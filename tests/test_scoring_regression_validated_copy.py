"""Synthetic scoring regression: propagate revalidated copies through all later processing.

Previously _revalidate_inputs discarded rebuilt copies and score_batch used mutated
originals; bytes-valued scope changed correct C/P/G=1/2/1 to falsely ready 0/0/0.
Indexing, digests, Agent requests, scores, and reports now share validated copies
without modifying callers. Exercise the full score_batch entry point.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scoring_sample_helpers import load_bundle

from hvs_catalog.evaluation.scoring import (
    AgentResponse,
    MockAgentAdapter,
    protocol_only_config,
    score_batch,
)
from hvs_catalog.evaluation.scoring.errors import ScoringInputError

S08 = "synthetic-s08"
DIST = "observed_phase_space.distance"


def _l2_rows(paper):
    return [row for row in paper.pairings if row.layer == "l2"]


def test_bytes_scope_scores_correctly_via_validated_copy():
    """Mutate constructed scope paths to UTF-8 bytes; score normalized validated text.

    JSON implicitly restores original strings, so revalidation passes. Previously bytes
    in effective_paths changed C/P/G from 1/2/1 to ready 0/0/0. Using validated copies
    preserves correct 1/2/1.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    plan.scopes[0].quantity_paths[0] = DIST.encode()
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert paper.readiness == "ready"
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert paper.l2.precision == 0.5 and paper.l2.recall == 1.0
    assert paper.l2.f1 == pytest.approx(2 / 3)
    assert report.batch_provisional is False
    statuses = [row.status for row in _l2_rows(paper)]
    assert statuses == [
        "matched",
        "extra_value",
    ]  # Do not wrongly classify out_of_scope_value.
    # Do not write back to caller objects.
    assert isinstance(plan.scopes[0].quantity_paths[0], bytes)


def test_bytes_scope_appended_duplicate_path_rejected_before_side_effects(
    tmp_path: Path,
):
    """Reject appended bytes paths that normalize to existing paths, with zero side
    effects.
    """
    plan, gold, preds, _auxiliary = load_bundle(S08)
    store_directory = tmp_path / "frozen"
    mock = MockAgentAdapter(
        lambda request: AgentResponse(
            verdict="equivalent", rationale="x", source_citations=["left:g1"]
        )
    )
    plan.scopes[0].quantity_paths.append(
        DIST.encode()
    )  # Duplicate after normalization.
    with pytest.raises(ScoringInputError):
        score_batch(
            plan,
            gold,
            preds,
            auxiliary=protocol_only_config(store_directory=str(store_directory)),
            agent_adapter=mock,
        )
    assert mock.calls == []
    assert not list(store_directory.glob("*")) if store_directory.exists() else True
    # Do not write back to caller objects.
    assert isinstance(plan.scopes[0].quantity_paths[1], bytes)


def test_prediction_identifier_bytes_flows_through_copy():
    """Mutated prediction identifier-list bytes normalize in the copy and score correctly."""
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].identifiers[0] = (
        b"SYN-A"  # List-element assignment bypasses validation.
    )
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False
    # Do not write back to caller objects.
    assert isinstance(preds[0].objects[1].identifiers[0], bytes)


def test_gold_identifier_bytes_flows_through_copy():
    """Mutated Gold identifier-list bytes normalize in the copy and score correctly."""
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers[0] = b"SYN-A"
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
    assert report.batch_provisional is False
    assert isinstance(gold[0].objects[0].identifiers[0], bytes)


def test_gold_identifier_bytes_appended_duplicate_rejected():
    """Reject appended Gold bytes identifiers that violate object-local uniqueness after
    normalization.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    gold[0].objects[0].identifiers.append(b"SYN-A")
    with pytest.raises(ScoringInputError):
        score_batch(plan, gold, preds, auxiliary=auxiliary)
    assert isinstance(gold[0].objects[0].identifiers[1], bytes)


def test_validated_copies_used_for_digests_and_agent_context(tmp_path: Path):
    """Fingerprints and Agent context/requests use the same validated contents; frozen
    reuse still works.
    """
    plan, gold, preds, _auxiliary = load_bundle("synthetic-s07u1es")
    store_directory = str(tmp_path / "frozen")
    # Normalize post-construction mutation so the copy matches the frozen key and reuses correctly.
    plan.scopes[0].quantity_paths[0] = plan.scopes[0].quantity_paths[0].encode()

    def make_adapter():
        return MockAgentAdapter(
            lambda request: AgentResponse(
                verdict="equivalent",
                rationale="x",
                source_citations=["left:table-1"],
            )
        )

    first_adapter = make_adapter()
    first = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=first_adapter,
    )
    assert len(first_adapter.calls) == 1
    assert first.papers[0].l2.correct == 1  # The validated copy scores correctly.
    second_adapter = make_adapter()
    second = score_batch(
        plan,
        gold,
        preds,
        auxiliary=protocol_only_config(store_directory=store_directory),
        agent_adapter=second_adapter,
    )
    assert second_adapter.calls == []
    assert second.auxiliary.judgments[0].reused is True
    assert len(list((tmp_path / "frozen").glob("*.json"))) == 1


def test_caller_inputs_untouched_after_successful_scoring():
    """Successful scoring, including mutated input, leaves caller contents completely
    unchanged.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    plan.scopes[0].quantity_paths[0] = DIST.encode()
    preds[0].objects[1].identifiers[0] = b"SYN-A"
    snapshot = (
        [type(item).__name__ for item in plan.scopes[0].quantity_paths],
        [type(name).__name__ for name in preds[0].objects[1].identifiers],
    )
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    assert report.papers[0].l2.correct == 1
    assert snapshot == (
        [type(item).__name__ for item in plan.scopes[0].quantity_paths],
        [type(name).__name__ for name in preds[0].objects[1].identifiers],
    )


def test_duplicate_record_id_rejection_and_pool_semantics_unaffected():
    """Retain contracts: reject duplicate record_ids while scoring valid duplicate values
    consistently.
    """
    plan, gold, preds, auxiliary = load_bundle(S08)
    preds[0].objects[1].quantities[0].values[0].record_id = "p1"
    with pytest.raises(ScoringInputError):
        score_batch(plan, gold, preds, auxiliary=auxiliary)

    plan, gold, preds, auxiliary = load_bundle(S08)
    reversed_objs = list(reversed(preds[0].objects))
    preds[0].objects = reversed_objs
    report = score_batch(plan, gold, preds, auxiliary=auxiliary)
    paper = report.papers[0]
    assert (paper.l1.tp, paper.l1.fp, paper.l1.fn) == (1, 1, 0)
    assert (paper.l2.correct, paper.l2.predicted, paper.l2.gold) == (1, 2, 1)
