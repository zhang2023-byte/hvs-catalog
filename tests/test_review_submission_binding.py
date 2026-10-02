"""Decision identity/exact-revision regressions: reject cross-workspace delivery and
missing/stale revisions.

Workspaces creating executions/questions for one paper in identical order may share
execution IDs, but question IDs derive from logical workspace identity. Reject unchanged
foreign submissions without new decisions. Exact-revision submission/query rejects
missing revisions and old revisions outside the minimal answer chain, while
same-request/valid-chain retries remain idempotent. Replay accepts only revisions
explicitly linked to the original successful receipt.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from shared_scenarios import (
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.questions import Actor
from hvs_catalog.review import DecisionSubmission, ReviewService
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    RevisionStaleError,
    WorkspaceIntegrityError,
    initialize_workspace,
)


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _paused_with_question(space, service, *, request_key="a"):
    execution, flow, _ = start_execution(space, paper="A", request_key=request_key)
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "paused"
    question = pending_questions(space, execution_id=execution.execution_id)[0]
    return execution, question


def test_cross_space_submission_rejected_verbatim(tmp_path, service):
    """Reject unchanged cross-workspace submissions despite identical paper/order, adding
    no target decisions.
    """
    space_a = initialize_workspace(tmp_path / "a")
    space_b = initialize_workspace(tmp_path / "b")
    try:
        exec_a, question_a = _paused_with_question(space_a, service)
        exec_b, question_b = _paused_with_question(space_b, service)
        # Per-workspace execution counters coincide, but workspace-derived question IDs differ.
        assert exec_a.execution_id == exec_b.execution_id
        assert question_a.record.record_id != question_b.record.record_id
        assert space_a.identity.space_id != space_b.identity.space_id
        # Deliver A's unchanged question reference, fingerprint, execution, step, and answer into B.
        submission = DecisionSubmission(
            question_ref=question_a.question_ref,
            fingerprint=question_a.record.data.fingerprint,
            actor=Actor(kind="human", id="expert"),
            outcome="unresolved",
            answer_original="Source-space-only answer.",
            answer_en="Source-space-only answer.",
            reason="Verbatim cross-space request.",
        )
        with pytest.raises(WorkspaceIntegrityError):
            ReviewService(space_b).submit_decision(
                execution_id=exec_a.execution_id,
                step_id=question_a.step_id or "quantities",
                submission=submission,
                request_key="from-space-a",
            )
        # Add no decision to B; its own question remains open.
        decisions = [
            r for r in space_b.store().list_records() if r.RECORD_NAME == "decision"
        ]
        assert decisions == []
        assert question_b.status == "open"
        # B can still submit an answer to its own question normally.
        receipt = submit_expert_decision(
            space_b,
            execution_id=exec_b.execution_id,
            question_ref=question_b.question_ref,
            request_key="b-own",
        )
        assert receipt.first_save
    finally:
        space_a.close()
        space_b.close()


def test_nonexistent_question_revision_rejected(tmp_path, service):
    """Reject nonexistent question revisions without decisions or new revisions."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, question = _paused_with_question(space, service)
        nonexistent = RecordRef(record_id=question.record.record_id, revision=999)
        with pytest.raises(WorkspaceIntegrityError, match="不存在"):
            submit_expert_decision(
                space,
                execution_id=execution.execution_id,
                question_ref=nonexistent,
                request_key="bad-revision",
            )
        assert question.status == "open"
        decisions = [
            r for r in space.store().list_records() if r.RECORD_NAME == "decision"
        ]
        assert decisions == []
        # Queries also reject nonexistent revisions.
        with pytest.raises(WorkspaceIntegrityError, match="不存在"):
            ReviewService(space).get_question(nonexistent)
    finally:
        space.close()


def test_answered_question_accepts_only_answer_chain(tmp_path, service):
    """Answered questions accept only the minimal pre/post-answer revision chain."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, question = _paused_with_question(space, service)
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=question.question_ref,
            request_key="dec",
        )
        current = ReviewService(space).get_question(question.question_ref)
        assert current.record.revision == 2
        # Same answer to pre-answer v1 under a new key is a valid retry returning the original decision.
        before = RecordRef(record_id=question.record.record_id, revision=1)
        replay = submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=before,
            request_key="dec-retry-old-chain",
        )
        assert not replay.first_save
        decisions = [
            r for r in space.store().list_records() if r.RECORD_NAME == "decision"
        ]
        assert len(decisions) == 1
        # Reject nonexistent revisions outside the answer chain.
        stale = RecordRef(record_id=question.record.record_id, revision=999)
        with pytest.raises((RevisionStaleError, WorkspaceIntegrityError)):
            submit_expert_decision(
                space,
                execution_id=execution.execution_id,
                question_ref=stale,
                request_key="dec-stale",
            )
    finally:
        space.close()


# ---- Replay also validates submitted references ----


def _unresolved_submission(question):
    return DecisionSubmission(
        question_ref=question.question_ref,
        fingerprint=question.record.data.fingerprint,
        actor=Actor(kind="human", id="expert"),
        outcome="unresolved",
        answer_original="uncertain",
        answer_en="uncertain",
        reason="Synthetic replay probe.",
    )


def test_replay_with_unrelated_revision_rejected(tmp_path, service):
    """Reject same-key replay carrying nonexistent v999 through the public service."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, question = _paused_with_question(space, service)
        review = ReviewService(space)
        submission = _unresolved_submission(question)
        step_id = question.step_id or "quantities"
        first = review.submit_decision(
            execution_id=execution.execution_id,
            step_id=step_id,
            submission=submission,
            request_key="same-key",
        )
        assert first.first_save

        bad = replace(
            submission,
            question_ref=RecordRef(record_id=question.record.record_id, revision=999),
        )
        with pytest.raises(RevisionStaleError, match="999"):
            review.submit_decision(
                execution_id=execution.execution_id,
                step_id=step_id,
                submission=bad,
                request_key="same-key",
            )

        # Replay of original v1 and answered v2 remains idempotent.
        replay_v1 = review.submit_decision(
            execution_id=execution.execution_id,
            step_id=step_id,
            submission=submission,
            request_key="same-key",
        )
        assert not replay_v1.first_save
        assert replay_v1.question_status == "answered"
        replay_v2 = review.submit_decision(
            execution_id=execution.execution_id,
            step_id=step_id,
            submission=replace(
                submission,
                question_ref=RecordRef(record_id=question.record.record_id, revision=2),
            ),
            request_key="same-key",
        )
        assert not replay_v2.first_save
        decisions = [
            r for r in space.store().list_records() if r.RECORD_NAME == "decision"
        ]
        assert len(decisions) == 1
    finally:
        space.close()


def test_old_success_receipt_queryable_after_supersede(tmp_path, service):
    """After later supersession, query the original successful receipt by replaying its
    original revision.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, question = _paused_with_question(space, service)
        review = ReviewService(space)
        submission = _unresolved_submission(question)
        step_id = question.step_id or "quantities"
        review.submit_decision(
            execution_id=execution.execution_id,
            step_id=step_id,
            submission=submission,
            request_key="dec-then-supersede",
        )
        # Supersede after the decision; reuse contents with a derived new identity for the required new question.
        from hvs_catalog.review.service import QuestionContent

        content = QuestionContent(
            category=question.record.data.category,
            target_refs=question.record.data.target_refs,
            source_refs=question.record.data.source_refs,
            rules_ref=question.record.data.rules_ref,
            prompt=question.record.data.prompt + " (revised)",
            evidence_refs=question.record.data.evidence_refs,
        )
        current = review.get_question(question.question_ref)
        review.supersede_question(
            execution_id=execution.execution_id,
            step_id=step_id,
            old_ref=current.question_ref,
            request_key="supersede-1",
            new_content=content,
            reason="synthetic supersede",
        )
        replay = review.submit_decision(
            execution_id=execution.execution_id,
            step_id=step_id,
            submission=submission,
            request_key="dec-then-supersede",
        )
        assert not replay.first_save
        assert replay.question_status == "superseded"
        assert replay.note is not None
    finally:
        space.close()
