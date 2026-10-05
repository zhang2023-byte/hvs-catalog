"""Shared extraction entry point: expert-review enabled/disabled groups (C04, synthetic
S11/E20 cases).

Keep material/method/model identical and vary only the switch. Enabled runs pause for
experts and resume through ReviewService. Disabled runs never request/wait for experts:
ordinary runs retain unresolved issues, while evaluation/synthetic AI-only runs may save
model_judgments without Decisions. Isolate execution and decisions; disabled runs cannot
consume newly added enabled-group decisions.
"""

from __future__ import annotations

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
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
from hvs_catalog.records.questions import DecisionRecord
from hvs_catalog.workspace import WorkspaceIntegrityError, initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _request(source, *, mode="daily", expert=True):
    return ExtractionRequest(
        mode=mode,
        method="roster",
        source_refs=[source],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )


def test_s11_two_groups_same_inputs_only_switch_differs(space, service):
    """S11: run both expert-review groups on identical paper, material, method, and model."""
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(space, request=_request(source), request_key="s11-on")
    off = start_extraction(
        space,
        request=_request(source, mode="evaluation", expert=False),
        request_key="s11-off",
    )
    assert on.execution_id != off.execution_id
    assert on.method == off.method == "roster"
    assert on.models == off.models
    assert on.input_digest != off.input_digest

    # Enabled group pauses for an expert, then resumes after a simulated human decision.
    first = resume_extraction(space, on.execution_id, service=service)
    assert first.outcome == "paused"
    assert first.pending_question["status"] == "open"
    question = pending_questions(space, on.execution_id)[0]
    receipt = submit_expert_decision(
        space,
        execution_id=on.execution_id,
        question_ref=question.question_ref,
        request_key="s11-decision",
        choice="no_overall_conclusion",
    )
    assert receipt.first_save is True
    assert receipt.decision_ref is not None
    second = resume_extraction(space, on.execution_id, service=service)
    assert second.outcome == "finished"
    assert second.errors == []
    # A human expert decision is applied to the paper revision.
    decisions = [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
    ]
    assert len(decisions) == 1
    assert decisions[0].execution_id == on.execution_id
    assert decisions[0].data.actor.kind == "human"
    on_status = get_extraction(space, on.execution_id)
    paper_id = f"paper-fic-r05-a-{on.execution_id}"
    assert space.store().current_revision(paper_id) == 2
    assert any(
        ref["record_id"] == paper_id and ref["revision"] == 2
        for ref in on_status["result_refs"]
    )

    # Evaluation AI-only disabled group completes without expert requests/waits and saves model judgments.
    off_report = resume_extraction(space, off.execution_id, service=service)
    assert off_report.outcome == "finished"
    assert off_report.pending_question is None
    off_status = get_extraction(space, off.execution_id)
    assert off_report.model_judgments == 1
    assert off_status["questions"] == []
    # Enabled-group decisions never revise the disabled-group paper; isolate outputs by execution.
    off_paper = f"paper-fic-r05-a-{off.execution_id}"
    assert space.store().current_revision(off_paper) == 1
    assert off_paper != paper_id


def test_off_group_cannot_consume_on_group_decisions(space, service):
    """Disabled groups reject current-round expert decisions and never inherit
    enabled-group decisions.
    """
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(space, request=_request(source), request_key="iso-on")
    resume_extraction(space, on.execution_id, service=service)
    question_on = pending_questions(space, on.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=on.execution_id,
        question_ref=question_on.question_ref,
        request_key="iso-decision",
        choice="no_overall_conclusion",
    )
    resume_extraction(space, on.execution_id, service=service)

    off = start_extraction(
        space,
        request=_request(source, mode="synthetic", expert=False),
        request_key="iso-off",
    )
    report = resume_extraction(space, off.execution_id, service=service)
    assert report.outcome == "finished"
    assert pending_questions(space, off.execution_id) == []
    with pytest.raises(WorkspaceIntegrityError):
        submit_expert_decision(
            space,
            execution_id=off.execution_id,
            question_ref=question_on.question_ref,
            request_key="iso-off-decision",
            choice="no_overall_conclusion",
        )
    # Disabled fixed input excludes new enabled-group decisions from this round.
    off_status = get_extraction(space, off.execution_id)
    assert off_status["config"]["existing_decision_refs"] == []
    # The disabled-group paper remains unrevised.
    assert space.store().current_revision(f"paper-fic-r05-a-{off.execution_id}") == 1


def test_existing_decision_allowlist_is_frozen_and_part_of_digest(space, service):
    """Snapshot the fixed allowed-decision set and include it in the execution digest, with
    verifiable structure.
    """
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(space, request=_request(source), request_key="allow-on")
    resume_extraction(space, on.execution_id, service=service)
    question = pending_questions(space, on.execution_id)[0]
    receipt = submit_expert_decision(
        space,
        execution_id=on.execution_id,
        question_ref=question.question_ref,
        request_key="allow-decision",
        choice="no_overall_conclusion",
    )
    resume_extraction(space, on.execution_id, service=service)

    reuse = start_extraction(
        space,
        request=_request(
            source,
            mode="synthetic",
            expert=False,
        ),
        request_key="allow-off",
    )
    with_allow = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="roster",
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=False,
            existing_decision_refs=[receipt.decision_ref],
        ),
        request_key="allow-off-with",
    )
    assert reuse.input_digest != with_allow.input_digest
    status = get_extraction(space, with_allow.execution_id)
    assert status["config"]["existing_decision_refs"] == [
        receipt.decision_ref.model_dump(mode="json")
    ]
    # Identical request/input retries reuse the execution rather than creating another.
    again = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="roster",
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=False,
            existing_decision_refs=[receipt.decision_ref],
        ),
        request_key="allow-off-with",
    )
    assert again.reused is True
    assert again.execution_id == with_allow.execution_id


def test_e20_pure_ai_group_records_model_judgment_not_decision(space, service):
    """E20: the AI-only group attempts ambiguity explanation and saves unresolved model
    results.
    """
    source = import_mock_source(space, PAPER_A)
    pure_ai = start_extraction(
        space,
        request=_request(source, mode="synthetic", expert=False),
        request_key="e20-pure",
    )
    report = resume_extraction(space, pure_ai.execution_id, service=service)
    assert report.outcome == "finished"
    assert service.calls_for_step("interpret_ambiguity") == 0
    status = get_extraction(space, pure_ai.execution_id)
    judgments = status["model_judgments"]
    assert len(judgments) == 1
    assert judgments[0]["step_id"] == "quantities"
    assert "most supported" in judgments[0]["judgment"]
    # The issue remains unresolved; no expert decision is created.
    assert status["questions"] == []
    decisions = [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
    ]
    assert decisions == []
    # Author assessment remains unresolved without fabricated conclusions.
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.paper import PaperRecord

    paper = space.store().read_record(
        RecordRef(
            record_id=f"paper-fic-r05-a-{pure_ai.execution_id}",
            revision=1,
        )
    )
    assert isinstance(paper, PaperRecord)
    boundness = paper.data.object_contributions[0].paper_boundness
    assert boundness.status == "possibly_unbound"
    assert not boundness.issue_refs


def test_daily_off_group_cannot_use_pure_ai_exception(space, service):
    """Ordinary disabled runs do not enable the AI-only exception: no explanation calls or
    model judgments; retain unresolved issues.
    """
    source = import_mock_source(space, PAPER_A)
    daily_off = start_extraction(
        space,
        request=_request(source, mode="daily", expert=False),
        request_key="daily-off",
    )
    report = resume_extraction(space, daily_off.execution_id, service=service)
    assert report.outcome == "finished"
    assert report.pending_question is None
    assert report.model_judgments == 1
    assert service.calls_for_step("interpret_ambiguity") == 0
    status = get_extraction(space, daily_off.execution_id)
    assert status["questions"] == []
    assert len(status["model_judgments"]) == 1


def test_expert_on_group_does_not_use_pure_ai_attempt(space, service):
    """Enabled runs wait for experts without AI-only explanation calls."""
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(
        space, request=_request(source, mode="evaluation"), request_key="eval-on"
    )
    report = resume_extraction(space, on.execution_id, service=service)
    assert report.outcome == "paused"
    assert service.calls_for_step("interpret_ambiguity") == 0
    assert report.model_judgments == 0
