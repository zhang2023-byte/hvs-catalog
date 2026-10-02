"""Output-isolation regressions for deliberate workspace reruns and new executions with
changed configuration.

A new request key reruns the expected path. Isolate roster, paper contribution, object,
and question identities by execution; never mix prior results, decisions, or receipts.
Reuse shared fixed sources/evidence through immutable revisions.
"""

from __future__ import annotations

import pytest
from shared_scenarios import (
    material as m,
)
from shared_scenarios import (
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def test_same_space_rerun_with_new_request_key_completes(tmp_path, service):
    """Deliberately rerun paper B in one workspace; both executions complete independently."""
    space = initialize_workspace(tmp_path / "space")
    try:
        first, flow1, _ = start_execution(space, paper="B", request_key="b1")
        assert (
            resume_execution(space, first.execution_id, flow1, service=service).outcome
            == "finished"
        )

        second, flow2, _ = start_execution(
            space, paper="B", request_key="intentional-rerun"
        )
        report = resume_execution(space, second.execution_id, flow2, service=service)
        assert report.outcome == "finished"
        assert report.errors == []

        # Distinct execution-specific roster, paper, and object identifiers.
        for paper, execution_id in (
            ("B", first.execution_id),
            ("B", second.execution_id),
        ):
            assert (
                space.store().current_revision(m.roster_record_id(paper, execution_id))
                == 1
            )
            assert (
                space.store().current_revision(m.paper_record_id(paper, execution_id))
                == 1
            )
            assert (
                space.store().current_revision(m.object_record_id(paper, execution_id))
                == 1
            )
        assert first.execution_id != second.execution_id
        # Reuse immutable fixed sources/evidence; identical contents are saved once.
        sources = [
            r for r in space.store().list_records() if r.RECORD_NAME == "source_package"
        ]
        assert len(sources) == 1
        evidences = [
            r for r in space.store().list_records() if r.RECORD_NAME == "evidence"
        ]
        assert len(evidences) == 3
        # Isolate receipts by execution.
        first_receipts = space.store().find_receipts(execution_id=first.execution_id)
        second_receipts = space.store().find_receipts(execution_id=second.execution_id)
        assert first_receipts and second_receipts
        assert {r.operation_key for r in first_receipts}.isdisjoint(
            {r.operation_key for r in second_receipts}
        )
        # Both executions actually call services; step caches are execution-local.
        assert service.calls_for_step("direct_roster") == 2
    finally:
        space.close()


def test_same_space_config_change_creates_isolated_execution(tmp_path, service):
    """After paper A completes with experts enabled, create and complete a new
    disabled-review execution.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        expert, flow_e, _ = start_execution(space, paper="A", request_key="expert-run")
        paused = resume_execution(space, expert.execution_id, flow_e, service=service)
        assert paused.outcome == "paused"
        question = pending_questions(space, execution_id=expert.execution_id)[0]
        submit_expert_decision(
            space,
            execution_id=expert.execution_id,
            question_ref=question.question_ref,
            request_key="expert-dec",
        )
        finished = resume_execution(space, expert.execution_id, flow_e, service=service)
        assert finished.outcome == "finished"
        expert_paper = m.paper_record_id("A", expert.execution_id)
        assert space.store().current_revision(expert_paper) == 2

        pure, flow_p, _ = start_execution(
            space, paper="A", request_key="pure-ai-run", expert_adjudication=False
        )
        assert pure.execution_id != expert.execution_id
        report = resume_execution(space, pure.execution_id, flow_p, service=service)
        assert report.outcome == "finished"
        assert report.errors == []

        # Independent outputs use their own roster/paper/objects; expert decisions never revise the new paper.
        pure_paper = m.paper_record_id("A", pure.execution_id)
        assert space.store().current_revision(pure_paper) == 1
        assert space.store().current_revision(expert_paper) == 2
        pure_object = m.object_record_id("A", pure.execution_id)
        assert space.store().current_revision(pure_object) == 1
        # Save new unresolved questions without mixing enabled-group decisions.
        pure_questions = pending_questions(space, execution_id=pure.execution_id)
        assert [q.status for q in pure_questions] == ["open"]
        assert pure_questions[0].record.record_id != question.record.record_id
        assert pure_questions[0].decisions == []
        # Save model judgments in the new run manifest, never as expert decisions.
        run_p = space.store().read_record_current(f"run-{pure.execution_id}")
        assert run_p.data.model_judgments
    finally:
        space.close()
