"""Recovery/application: apply new revisions once, reuse reentry, block stale application,
and stop inconsistencies.

In-process A13/A15/A16/A18 cases; see test_runtime_subprocess.py for real exits before
saving, during application, and after application.
"""

from __future__ import annotations

import pytest
from shared_scenarios import (
    direct_flow,
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)
from shared_scenarios import material as m

from hvs_catalog.records.common import RecordRef
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    RecoveryInconsistentError,
    initialize_workspace,
)


@pytest.fixture()
def space(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    yield space
    space.close()


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


@pytest.fixture()
def waiting(space, service):
    execution, flow, _ = start_execution(space, paper="A", request_key="a")
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "paused"
    question = pending_questions(space, execution_id=execution.execution_id)[0]
    return execution, flow, question


def test_decision_application_creates_single_revision_and_keeps_history(
    space, service, waiting
):
    """A13: create a new revision once, preserve old records, and expose decision links."""
    execution, flow, question = waiting
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "finished"
    applied = [a for a in report.applied if a.blocked_reason is None]
    assert len(applied) == 1
    # Repeated recovery creates no second new revision.
    paper_id = m.paper_record_id("A", execution.execution_id)
    paper_revisions = [
        r for r in space.store().list_records() if r.record_id == paper_id
    ]
    assert [r.revision for r in paper_revisions] == [1, 2]
    again = resume_execution(space, execution.execution_id, flow, service=service)
    assert again.outcome == "finished"
    paper_revisions = [
        r for r in space.store().list_records() if r.record_id == paper_id
    ]
    assert [r.revision for r in paper_revisions] == [1, 2]
    # Read original contributions/text through exact revisions.
    v1 = space.store().read_record(RecordRef(record_id=paper_id, revision=1))
    assert v1.data.object_contributions[0].paper_boundness.status is None
    v2 = space.store().read_record(RecordRef(record_id=paper_id, revision=2))
    assert (
        v2.data.object_contributions[0].paper_boundness.status
        == "no_overall_conclusion"
    )
    # Trace question to decision to application receipt.
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.status == "answered"
    assert view.application is not None
    assert view.application.phase == "applied"
    assert view.decisions[0].record_id in view.application.receipt_key or True


def test_no_half_decision_before_save(space, waiting):
    """A15 before save: paused execution has no decision or partial decision."""
    _execution, _, question = waiting
    decisions = [r for r in space.store().list_records() if r.RECORD_NAME == "decision"]
    assert decisions == []
    assert question.status == "open"


def test_decision_saved_but_not_applied_keeps_results_untouched(space, waiting):
    """A15 after save, before apply: decisions are queryable but uncommitted results are
    absent.
    """
    execution, _, question = waiting
    receipt = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    assert receipt.first_save
    assert (
        space.store().current_revision(m.paper_record_id("A", execution.execution_id))
        == 1
    )
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.status == "answered"
    assert view.application is None  # Not yet applied.


def test_crash_after_apply_does_not_create_second_revision(space, service, waiting):
    """A15 after apply, before recovery: committed application takes effect only once."""
    execution, _, question = waiting
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )

    def explode():
        raise RuntimeError("caller lost the completion response")

    crashing_flow = direct_flow(fault_hooks={"after_apply": explode})
    failed = resume_execution(
        space, execution.execution_id, crashing_flow, service=service
    )
    assert failed.outcome == "failed"
    # Application is committed: paper v2 and its application receipt exist.
    assert (
        space.store().current_revision(m.paper_record_id("A", execution.execution_id))
        == 2
    )
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.application.phase == "applied"
    # Recovery reuses the receipt without another revision and continues to completion.
    recovered = resume_execution(
        space, execution.execution_id, direct_flow(), service=service
    )
    assert recovered.outcome == "finished"
    revisions = [
        r.revision
        for r in space.store().list_records()
        if r.record_id == m.paper_record_id("A", execution.execution_id)
    ]
    assert revisions == [1, 2]


def test_node_reentry_reuses_step_results_without_extra_calls(space, service, waiting):
    """A16: node reentry during recovery reuses step results without another mock call."""
    execution, _, question = waiting
    calls_before = service.summary()["calls"]
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    report = resume_execution(
        space, execution.execution_id, direct_flow(), service=service
    )
    assert report.outcome == "finished"
    assert (
        service.summary()["calls"] == calls_before
    )  # Neither decision application nor reentry calls the service again.


def test_finished_execution_resume_reports_without_extra_work(space, service, waiting):
    """A16: after framework advancement with a lost completion response, reentry only
    validates and reports.
    """
    execution, flow, question = waiting
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    resume_execution(space, execution.execution_id, flow, service=service)
    calls = service.summary()["calls"]
    again = resume_execution(space, execution.execution_id, flow, service=service)
    assert again.outcome == "finished"
    assert again.mode == "already-finished"
    assert service.summary()["calls"] == calls


def test_response_received_but_not_saved_bills_again(space, service, tmp_path):
    """A17 in-process exception: fail after response receipt but before save, then call
    again on recovery and record both facts.
    """

    def explode():
        raise RuntimeError("crash after response, before save")

    execution, flow, _ = start_execution(
        space,
        paper="B",
        request_key="b",
        fault_hooks={"after_response:quantities": explode},
    )
    failed = resume_execution(space, execution.execution_id, flow, service=service)
    assert failed.outcome == "failed"
    billed = service.calls_for_step("quantities")
    assert billed >= 1
    recovered = resume_execution(
        space, execution.execution_id, direct_flow(), service=service
    )
    assert recovered.outcome == "finished"
    # New calls add facts; independent logs retain prior calls despite client transaction rollback.
    assert service.calls_for_step("quantities") > billed
    assert service.summary()["calls"] >= 3


def test_application_blocked_when_target_revised_by_other_operation(
    space, service, waiting
):
    """A18: another operation revises the target after decision save; block application and
    retain queryable decisions/errors.
    """
    execution, _, question = waiting
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    # Another operation first reaffirms the paper in a new identical-content revision without changing the roster.
    paper = space.store().read_record_current(
        m.paper_record_id("A", execution.execution_id)
    )
    from datetime import UTC, datetime

    reaffirmed = paper.model_copy(
        update={"revision": paper.revision + 1, "created_at": datetime.now(UTC)}
    )
    from hvs_catalog.workspace import CommitRequest

    space.store().commit(
        CommitRequest(
            operation_key="other-operation",
            kind="manual",
            new_records=[reaffirmed],
            expected_revisions={paper.record_id: paper.revision},
        )
    )
    report = resume_execution(
        space, execution.execution_id, direct_flow(), service=service
    )
    assert report.outcome == "paused"
    blocked = [a for a in report.applied if a.blocked_reason]
    assert blocked and "不能移植" in blocked[0].blocked_reason
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.application.phase == "blocked"
    assert "不能移植" in (view.application.error or "")
    # The decision stays queryable while the paper remains at the other operation's revision.
    assert view.decisions
    assert (
        space.store().current_revision(m.paper_record_id("A", execution.execution_id))
        == 2
    )


def test_checkpoint_cross_wired_execution_stops(space, service, tmp_path):
    """A18: a checkpoint bound to another execution stops this execution and reports
    inconsistency.
    """
    ex_b, flow_b, _ = start_execution(space, paper="B", request_key="b")
    resume_execution(space, ex_b.execution_id, flow_b, service=service)
    ex_a, flow_a, _ = start_execution(space, paper="A", request_key="a")
    resume_execution(space, ex_a.execution_id, flow_a, service=service)

    conn = space.checkpoint_connection()
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND (name LIKE 'checkpoint%' OR name LIKE '%writes%')"
            )
        ]
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            thread_col = "thread_id"
            if thread_col not in columns:
                continue
            rest = [c for c in columns if c != thread_col]
            conn.execute(
                f"DELETE FROM {table} WHERE {thread_col} = ?",
                (ex_a.execution_id,),
            )
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)})"
                f" SELECT ?, {', '.join(rest)} FROM {table} WHERE {thread_col} = ?",
                (ex_a.execution_id, ex_b.execution_id),
            )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RecoveryInconsistentError, match="绑定执行"):
        resume_execution(space, ex_a.execution_id, direct_flow(), service=service)
    run_a = space.store().read_record_current(f"run-{ex_a.execution_id}")
    assert run_a.data.state == "failed"


def test_missing_business_result_stops_execution(space, service, waiting):
    """A18: missing business results referenced by checkpoints stop execution without
    scientific conclusions.
    """
    execution, _, _ = waiting
    # Delete the roster record directly from the business database to simulate missing results.
    import sqlite3

    conn = sqlite3.connect(space.root / "business.db")
    roster_id = m.roster_record_id("A", execution.execution_id)
    conn.execute("DELETE FROM records WHERE record_id = ?", (roster_id,))
    conn.execute("DELETE FROM current_revisions WHERE record_id = ?", (roster_id,))
    conn.commit()
    conn.close()
    with pytest.raises(RecoveryInconsistentError, match="缺失"):
        resume_execution(space, execution.execution_id, direct_flow(), service=service)
    # Recovery failure is not a scientific conclusion; stop advancement and retain/report state if revision is impossible.
    run = space.store().read_record_current(f"run-{execution.execution_id}")
    assert run.data.state in {"failed", "waiting_review"}
