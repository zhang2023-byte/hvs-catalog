"""Atomic question creation and contribution-response reuse regressions.

Save questions, execution/step bindings, and receipts in one business transaction;
aborts leave no partial questions. After question creation but before step receipt,
fresh-process recovery reuses archived contribution responses without new calls or
comparing saved paper revisions against new timestamps. Missing bindings are not
wildcards; reject wrong-step submissions.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from shared_scenarios import (
    direct_flow,
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.review import ReviewService
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import WorkspaceIntegrityError, initialize_workspace


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _run_child(tmp_path: Path, code: str, *args) -> int:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (74,), result.stderr
    return result.returncode


@pytest.mark.integration
def test_crash_after_question_commit_resumes_without_extra_calls(tmp_path, service):
    """Exit after question commit but before step receipt; a fresh process resumes without
    new calls.
    """
    space_root = tmp_path / "space"
    space = initialize_workspace(space_root)
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        code = _run_child(
            tmp_path,
            """
            import os
            import sys
            from pathlib import Path

            from hvs_catalog.runtime import resume_execution
            from shared_scenarios import open_service
            from shared_scenarios import direct_flow
            from hvs_catalog.workspace import open_workspace

            space = open_workspace(Path(sys.argv[1]))
            service = open_service(Path(sys.argv[2]))

            def die():
                os._exit(74)

            flow = direct_flow(fault_hooks={"after_question_create": die})
            resume_execution(space, sys.argv[3], flow, service=service)
            """,
            str(space_root),
            str(tmp_path / "calls.jsonl"),
            execution.execution_id,
        )
        assert code == 74
        calls_before = service.summary()["calls"]
        assert calls_before == 2  # One roster call and one contribution call.
        # The question is saved with correct bindings; its transaction also saved the paper.
        question = pending_questions(space, execution_id=execution.execution_id)[0]
        assert question.status == "open"
        assert question.step_id == "quantities"
        from shared_scenarios import material as m

        paper_id = m.paper_record_id("A", execution.execution_id)
        assert space.store().current_revision(paper_id) == 1

        # Fresh-process recovery reuses archived responses without calls and returns to review wait.
        report = resume_execution(
            space, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "paused"
        assert service.summary()["calls"] == calls_before

        # Reject wrong-step submissions; bindings are not wildcards.
        with pytest.raises(WorkspaceIntegrityError, match="跨步骤"):
            ReviewService(space).submit_decision(
                execution_id=execution.execution_id,
                step_id="wrong-step",
                submission=_unresolved_submission(question),
                request_key="wrong-step",
            )

        # Finish after a valid decision, still without new calls.
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
        assert service.summary()["calls"] == calls_before
        assert space.store().current_revision(paper_id) == 2
    finally:
        space.close()


@pytest.mark.integration
def test_crash_inside_question_transaction_leaves_no_half_state(tmp_path, service):
    """Exit during the question transaction; leave no partial question or residual binding."""
    space_root = tmp_path / "space"
    space = initialize_workspace(space_root)
    try:
        execution, _flow, _ = start_execution(space, paper="A", request_key="a")
        code = _run_child(
            tmp_path,
            """
            import os
            import sys
            from pathlib import Path

            import hvs_catalog.workspace.store as store_mod
            from hvs_catalog.review.service import QUESTION_CREATE_KIND
            from hvs_catalog.runtime import resume_execution
            from shared_scenarios import open_service
            from shared_scenarios import direct_flow
            from hvs_catalog.workspace import open_workspace

            space = open_workspace(Path(sys.argv[1]))
            service = open_service(Path(sys.argv[2]))
            original = store_mod.StoreTransaction

            class ExitInQuestion(original):
                def insert_receipt(self, receipt) -> None:
                    if receipt.kind == QUESTION_CREATE_KIND:
                        os._exit(74)
                    super().insert_receipt(receipt)

            store_mod.StoreTransaction = ExitInQuestion
            resume_execution(
                space, sys.argv[3], direct_flow(), service=service
            )
            """,
            str(space_root),
            str(tmp_path / "calls.jsonl"),
            execution.execution_id,
        )
        assert code == 74
        calls_before = service.summary()["calls"]
        # The uncommitted transaction leaves no question or associated paper.
        assert pending_questions(space, execution_id=execution.execution_id) == []
        from shared_scenarios import material as m

        paper_id = m.paper_record_id("A", execution.execution_id)
        assert space.store().read_record_current(paper_id) is None
        # Fresh-process recovery reuses archived responses and creates a complete bound question without new calls.
        report = resume_execution(
            space, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "paused"
        assert service.summary()["calls"] == calls_before
        question = pending_questions(space, execution_id=execution.execution_id)[0]
        assert question.step_id == "quantities"
    finally:
        space.close()


def _unresolved_submission(question):
    from hvs_catalog.records.questions import Actor
    from hvs_catalog.review import DecisionSubmission

    return DecisionSubmission(
        question_ref=question.question_ref,
        fingerprint=question.record.data.fingerprint,
        actor=Actor(kind="human", id="probe"),
        outcome="unresolved",
        answer_original="x",
        answer_en="x",
        reason="x",
        evidence_refs=[],
        patch=None,
    )
