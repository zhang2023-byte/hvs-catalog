"""Shared extraction entry point: frozen configuration, recovery, and group isolation
(C05).

Freeze method/model/review switches and include them in execution digests. Reject
changed input under one request key; new configuration requires a new key/execution.
Recovery reads only saved snapshots and reuses archived responses after interruption
without duplicate calls.
"""

from __future__ import annotations

import inspect

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    ExtractionRequest,
    PipelineRegistry,
    StepModel,
    UnknownMethodError,
    get_extraction,
    import_mock_source,
    pending_questions,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import ROSTER_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    initialize_workspace,
)


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _request(source, **overrides):
    payload = {
        "mode": "daily",
        "method": "roster",
        "source_refs": [source],
        "models": [StepModel(model="mock-default")],
        "expert_adjudication": True,
    }
    payload.update(overrides)
    return ExtractionRequest(**payload)


def test_same_request_key_rejects_changed_switch(space, tmp_path):
    source = import_mock_source(space, PAPER_B)
    start_extraction(space, request=_request(source), request_key="frozen")
    with pytest.raises(OperationKeyConflictError, match="不同输入"):
        start_extraction(
            space,
            request=_request(source, expert_adjudication=False),
            request_key="frozen",
        )


def test_same_request_key_rejects_changed_model_or_method(space):
    source = import_mock_source(space, PAPER_B)
    start_extraction(space, request=_request(source), request_key="frozen-2")
    with pytest.raises(OperationKeyConflictError):
        start_extraction(
            space,
            request=_request(source, models=[StepModel(model="another-model")]),
            request_key="frozen-2",
        )
    with pytest.raises(OperationKeyConflictError):
        start_extraction(
            space,
            request=_request(source, mode="evaluation"),
            request_key="frozen-2",
        )


def test_same_request_same_input_reuses_execution(space):
    source = import_mock_source(space, PAPER_B)
    first = start_extraction(space, request=_request(source), request_key="reuse")
    second = start_extraction(space, request=_request(source), request_key="reuse")
    assert first.reused is False
    assert second.reused is True
    assert first.execution_id == second.execution_id


def test_resume_signature_has_no_request_channel(space):
    """Recovery accepts no new request content; configuration cannot change within the
    original run.
    """
    parameters = inspect.signature(resume_extraction).parameters
    assert set(parameters) == {"space", "execution_id", "service", "registry"}
    assert "request" not in parameters
    assert "expert_adjudication" not in parameters
    assert "models" not in parameters


def test_resume_uses_frozen_snapshot_config(space, service):
    """Paused recovery uses saved configuration, and status echoes the same freeze."""
    source = import_mock_source(space, PAPER_A)
    handle = start_extraction(
        space,
        request=_request(
            source,
            mode="evaluation",
            models=[
                StepModel(model="mock-default"),
                StepModel(step_id="quantities", model="mock-quantities"),
            ],
        ),
        request_key="frozen-config",
    )
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    assert first.config["method"] == "roster"
    assert first.config["mode"] == "evaluation"
    assert first.config["expert_adjudication"] is True
    assert first.config["models"][1]["model"] == "mock-quantities"
    question = pending_questions(space, handle.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="frozen-decision",
        choice="no_overall_conclusion",
    )
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "finished"
    assert second.config == first.config
    status = get_extraction(space, handle.execution_id)
    assert status["config"] == first.config
    # The value step actually used the frozen step-specific model.
    used = {call["step"]: call["model"] for call in service.calls}
    assert used["quantities"] == "mock-quantities"


def test_archived_response_reused_after_interrupted_commit(space):
    """After interruption between archiving and commit, reuse the response without calling
    again.
    """
    from hvs_catalog.runtime import resume_execution

    source = import_mock_source(space, PAPER_B)
    handle = start_extraction(space, request=_request(source), request_key="archive")

    def interrupt_after_archive():
        raise RuntimeError("simulated crash after archive")

    failed = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            ROSTER_METHOD,
            mode="daily",
            fault_hooks={"after_archive:roster": interrupt_after_archive},
        ),
        service=PipelineMockService(),
    )
    assert failed.outcome == "failed"
    # A fresh recovery service lacks old call logs; archived responses avoid repeated calls.
    fresh_service = PipelineMockService()
    report = resume_extraction(space, handle.execution_id, service=fresh_service)
    assert report.outcome == "finished"
    assert fresh_service.calls_for_step("roster") == 0
    assert fresh_service.calls_for_step("quantities") == 1
    status = get_extraction(space, handle.execution_id)
    roster_attempts = next(
        step["attempts"] for step in status["steps"] if step["step_id"] == "roster"
    )
    assert (
        roster_attempts == 1
    )  # One actual call occurred; archive reuse makes no second call.


def test_registry_without_method_refuses_resume(space, service):
    """Reject advancement when the method is unregistered, for example after code rollback;
    do not attach a legacy flow.
    """
    source = import_mock_source(space, PAPER_B)
    handle = start_extraction(space, request=_request(source), request_key="no-reg")
    empty = PipelineRegistry()
    with pytest.raises(UnknownMethodError):
        resume_extraction(space, handle.execution_id, service=service, registry=empty)


def test_two_groups_outputs_and_decisions_isolated(space, service):
    """Never mix group outputs/decisions; reruns in one workspace use new request keys."""
    source = import_mock_source(space, PAPER_A)
    expert = start_extraction(space, request=_request(source), request_key="g1")
    resume_extraction(space, expert.execution_id, service=service)
    question = pending_questions(space, expert.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=expert.execution_id,
        question_ref=question.question_ref,
        request_key="g1-decision",
        choice="no_overall_conclusion",
    )
    resume_extraction(space, expert.execution_id, service=service)
    pure = start_extraction(
        space,
        request=_request(source, mode="evaluation", expert_adjudication=False),
        request_key="g2",
    )
    report = resume_extraction(space, pure.execution_id, service=service)
    assert report.outcome == "finished"
    expert_paper = f"paper-fic-r05-a-{expert.execution_id}"
    pure_paper = f"paper-fic-r05-a-{pure.execution_id}"
    assert expert_paper != pure_paper
    assert space.store().current_revision(expert_paper) == 2
    assert space.store().current_revision(pure_paper) == 1
    receipts_expert = space.store().find_receipts(execution_id=expert.execution_id)
    receipts_pure = space.store().find_receipts(execution_id=pure.execution_id)
    assert receipts_expert and receipts_pure
    assert {r.operation_key for r in receipts_expert}.isdisjoint(
        {r.operation_key for r in receipts_pure}
    )
