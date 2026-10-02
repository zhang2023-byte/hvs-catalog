"""Shared extraction entry point: synthetic candidate_review method (C02/C03).

Discover candidates, judge each, then share value/finalization steps. Retain
included/excluded/unresolved dispositions. Excluded objects have no contributions;
unresolved objects are not automatically excluded. Expert inclusion decisions let them
proceed to values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hvs_catalog.pipeline import (
    PAPER_C,
    ExtractionRequest,
    StepModel,
    get_extraction,
    import_mock_source,
    pending_questions,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.questions import DecisionRecord
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.workspace import initialize_workspace

FIXTURES = Path(__file__).parent / "fixtures" / "pipeline"
MATERIALS = json.loads((FIXTURES / "materials.json").read_text(encoding="utf-8"))
EXPECTED_ENTRIES = {
    entry["item_id"]: entry["disposition"]
    for entry in MATERIALS["papers"]["FIC-CANDIDATE-C"]["roster_entries"]
}


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _start(space, source, *, request_key, mode="daily", expert=True, models=None):
    request = ExtractionRequest(
        mode=mode,
        method="candidate_review",
        source_refs=[source],
        models=models or [StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _record(space, record_id):
    revision = space.store().current_revision(record_id)
    assert revision is not None
    record = space.store().read_record(
        RecordRef(record_id=record_id, revision=revision)
    )
    assert record is not None
    return record


def test_discovery_and_per_candidate_review_calls(space, service):
    """One discovery call and one call per candidate; fixed dispositions match fixtures."""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-calls", expert=False)
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    assert service.calls_for_step("discover_candidates") == 1
    assert service.calls_for_step("review_candidates") == 3
    status = get_extraction(space, handle.execution_id)
    # Archive each request, response, and usage separately without overwriting others.
    attempts = {step["step_id"]: step["attempts"] for step in status["steps"]}
    assert attempts["discover_candidates"] == 1
    assert attempts["review_candidates"] == 3
    assert attempts["quantities"] == 1
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    assert isinstance(roster, RosterRecord)
    assert roster.data.method == "candidate_review"
    assert {e.item_id: e.disposition for e in roster.data.entries} == EXPECTED_ENTRIES


def test_pending_not_auto_excluded_without_expert(space, service):
    """Ordinary runs with review disabled retain unresolved entries and pending questions."""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-off-daily", expert=False)
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "pending"
    assert entry.issue_refs  # Unresolved entries reference unresolved questions.
    questions = pending_questions(space, handle.execution_id)
    assert len(questions) == 1
    assert questions[0].status == "open"
    # No expert decisions are written.
    decisions = [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
    ]
    assert decisions == []
    # Excluded objects have neither contributions nor object records.
    paper = _record(space, f"paper-fic-candidate-c-{handle.execution_id}")
    assert isinstance(paper, PaperRecord)
    assert [c.roster_item_id for c in paper.data.object_contributions] == ["cand-c-1"]


def test_expert_group_pauses_and_include_decision_adds_quantities(space, service):
    """Enabled runs pause during candidate judgment; inclusion decisions admit unresolved
    entries to the value step.
    """
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-on")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    assert first.pending_question is not None
    question = pending_questions(space, handle.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="dec-c-include",
        choice="include",
    )
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "finished"
    assert second.errors == []
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    assert space.store().current_revision(roster.record_id) == 2
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "included"
    assert entry.contribution_id == "contribution-c-3"
    paper = _record(space, f"paper-fic-candidate-c-{handle.execution_id}")
    assert [c.roster_item_id for c in paper.data.object_contributions] == [
        "cand-c-1",
        "cand-c-3",
    ]
    values = {
        c.roster_item_id: c.quantities[0].values[0].value
        for c in paper.data.object_contributions
    }
    expected = {
        item["roster_item_id"]: item["value"]
        for item in MATERIALS["papers"]["FIC-CANDIDATE-C"]["values"]
    }
    assert values == expected
    objects = [
        ref
        for ref in get_extraction(space, handle.execution_id)["result_refs"]
        if ref["record_id"].startswith("obj-")
    ]
    assert len(objects) == 2


def test_unresolved_decision_keeps_run_waiting(space, service):
    """An unresolved expert answer is saved while the step keeps waiting, without automatic
    exclusion.
    """
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-unresolved")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    question = pending_questions(space, handle.execution_id)[0]
    receipt = submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="dec-c-pending",
        choice="keep_pending",
    )
    assert receipt.first_save is True
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "paused"
    assert second.pending_question["answered_outcome"] == "unresolved"
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "pending"


def test_review_step_model_selection(space, service):
    """Use step-specific models for candidate judgments and the default fallback for
    discovery.
    """
    source = import_mock_source(space, PAPER_C)
    models = [
        StepModel(model="mock-default"),
        StepModel(step_id="review_candidates", model="mock-review"),
    ]
    handle = _start(space, source, request_key="c-models", expert=False, models=models)
    resume_extraction(space, handle.execution_id, service=service)
    used = {call["step"]: call["model"] for call in service.calls}
    assert used["discover_candidates"] == "mock-default"
    assert used["review_candidates"] == "mock-review"


def test_candidate_failure_preserves_discovery(space, service):
    """A candidate-judgment failure fails the paper but retains reusable discovery results."""

    def fail_review():
        raise RuntimeError("simulated review failure")

    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.pipeline.methods import CANDIDATE_REVIEW_METHOD
    from hvs_catalog.runtime import resume_execution

    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-fail", expert=False)
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            CANDIDATE_REVIEW_METHOD,
            mode="daily",
            fault_hooks={"after_response:review_candidates": fail_review},
        ),
        service=service,
    )
    assert report.outcome == "failed"
    status = get_extraction(space, handle.execution_id)
    states = {step["step_id"]: step["state"] for step in status["steps"]}
    assert states["discover_candidates"] == "finished"
    assert service.calls_for_step("discover_candidates") == 1
    recovered = resume_extraction(space, handle.execution_id, service=service)
    assert recovered.outcome == "finished"
    assert service.calls_for_step("discover_candidates") == 1
