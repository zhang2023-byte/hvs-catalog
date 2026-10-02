"""Shared extraction entry point: execution status, result references, and usage queries
(C01/C02 returns).

get_extraction and execution reports provide references for scoring adaptation, echoed
configuration, question/model-judgment counts, and original errors. Unknown usage stays
null rather than becoming zero.
"""

from __future__ import annotations

import json

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    ExtractionRequest,
    StepModel,
    execution_result_refs,
    get_extraction,
    import_mock_source,
    pending_questions,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from hvs_catalog.pipeline.mock import STEP_TOKENS, PipelineMockService
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _start(space, source, *, request_key, expert=False, mode="synthetic"):
    request = ExtractionRequest(
        mode=mode,
        method="direct",
        source_refs=[source],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def test_status_contains_config_steps_questions_and_usage(space, service):
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="status-b")
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    status = get_extraction(space, handle.execution_id)
    assert status["execution_id"] == handle.execution_id
    assert status["state"] == "finished"
    assert status["config"]["method"] == "direct"
    assert status["config"]["mode"] == "synthetic"
    assert {s["step_id"] for s in status["steps"]} == {
        "direct_roster",
        "quantities",
        "finalize",
    }
    assert all(step["state"] == "finished" for step in status["steps"])
    expected_tokens = STEP_TOKENS["direct_roster"] + STEP_TOKENS["quantities"]
    assert status["usage"] == {"attempts": 2, "tokens": expected_tokens}
    assert status["errors"] == []
    # Read back result references through the scoring-adaptation entry point.
    refs = execution_result_refs(space, handle.execution_id)
    assert [ref.record_id for ref in refs] == [
        ref["record_id"] for ref in status["result_refs"]
    ]
    for ref in refs:
        assert space.store().read_record(ref) is not None


def test_report_is_json_serializable(space, service):
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="report-json")
    report = resume_extraction(space, handle.execution_id, service=service)
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert json.loads(payload)["outcome"] == "finished"


def test_pending_report_carries_question_and_timing_data(space, service):
    """Paused reports list pending questions; status exposes creation/decision times for
    measuring expert time.
    """
    source = import_mock_source(space, PAPER_A)
    handle = _start(space, source, request_key="timing", expert=True, mode="daily")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    assert first.pending_question["needs_processing"] is True
    status = get_extraction(space, handle.execution_id)
    assert status["questions"][0]["decided_at"] is None
    assert status["questions"][0]["created_at"]
    question = pending_questions(space, handle.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="timing-decision",
        choice="no_overall_conclusion",
    )
    status2 = get_extraction(space, handle.execution_id)
    assert status2["questions"][0]["decided_at"] is not None
    assert status2["questions"][0]["answered_outcome"] == "resolved"


def test_unknown_usage_stays_null_not_zero(space):
    """Unknown call usage stays null, not fabricated zero."""

    class NoUsageService(PipelineMockService):
        def call(self, *, step, request, material):
            response = super().call(step=step, request=request, material=material)
            response = dict(response)
            response["tokens"] = None
            return response

    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="usage-null")
    resume_extraction(space, handle.execution_id, service=NoUsageService())
    status = get_extraction(space, handle.execution_id)
    assert status["usage"] == {"attempts": 2, "tokens": None}


def test_failure_report_keeps_raw_errors(space, service):
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="raw-error")

    def boom():
        raise ValueError("raw failure detail")

    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.pipeline.methods import DIRECT_METHOD
    from hvs_catalog.runtime import resume_execution

    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            DIRECT_METHOD,
            mode="synthetic",
            fault_hooks={"after_response:quantities": boom},
        ),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("raw failure detail" in error for error in report.errors)
    status = get_extraction(space, handle.execution_id)
    assert status["state"] == "failed"
    assert any("raw failure detail" in error for error in status["errors"])


def test_service_summary_reported_with_calls_and_tokens(space, service):
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="summary")
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.service_summary == {
        "calls": 2,
        "steps": {"direct_roster": 1, "quantities": 1},
        "tokens": STEP_TOKENS["direct_roster"] + STEP_TOKENS["quantities"],
    }
