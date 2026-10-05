"""Pending questions and decisions: binding, replay, conflicting/stale rejection, and
unresolved answers (A09/A10/A11).
"""

from __future__ import annotations

import pytest
from shared_scenarios import (
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.records.questions import Actor
from hvs_catalog.review import DecisionSubmission, ReviewService, derive_question_id
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    DecisionConflictError,
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
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
    """Run paper A until review wait; return (execution, flow, question)."""
    execution, flow, _ = start_execution(space, paper="A", request_key="a")
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "paused"
    question = pending_questions(space, execution_id=execution.execution_id)[0]
    return execution, flow, question


def test_create_question_replay_returns_original(space, waiting):
    execution, _, question = waiting
    assert question.status == "open"
    assert question.needs_processing
    # Identical request/content derives the same question identity from workspace, execution, and contents.
    from shared_scenarios import material as m

    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.rules import load_rules

    rules = load_rules()
    content = m.question_content(
        "A",
        source_ref=RecordRef(record_id=m.SOURCE_IDS["A"], revision=1),
        rules=__import__(
            "hvs_catalog.records.questions", fromlist=["RulesRef"]
        ).RulesRef(version=rules.version, sha256=rules.sha256),
        paper_id=m.paper_record_id("A", execution.execution_id),
    )
    assert (
        derive_question_id(
            execution.execution_id,
            content,
            space_id=space.identity.space_id,
        )
        == question.record.record_id
    )


def test_cross_step_submission_rejected(space, waiting):
    execution, _, question = waiting
    with pytest.raises(WorkspaceIntegrityError, match="跨步骤"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="roster",
            submission=_submission_for(question),
            request_key="wrong-step",
        )


def test_same_display_question_different_context_not_reused(space, service, waiting):
    """A09: identical display questions cannot mix decisions; fingerprints reflect actual
    fixed contents.
    """
    other = initialize_workspace(space.root.parent / "second")
    try:
        ex2, flow2, _ = start_execution(other, paper="A", request_key="a")
        resume_execution(other, ex2.execution_id, flow2, service=service)
        q2 = pending_questions(other, execution_id=ex2.execution_id)[0]
        # Identical display text in separate workspaces never mixes saved decisions.
        assert q2.record.data.prompt == _q_prompt(space)
        submit_expert_decision(
            other,
            execution_id=ex2.execution_id,
            question_ref=q2.question_ref,
            request_key="dec-2",
        )
        # The first workspace's question remains open, without implicitly consuming another workspace's decision.
        space_q = pending_questions(space)[0]
        assert space_q.status == "open"
        assert space_q.decisions == []
    finally:
        other.close()


def _q_prompt(space):
    return pending_questions(space)[0].record.data.prompt


def test_same_key_different_content_rejected(space, waiting):
    execution, _, question = waiting
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    other_content = _submission_for(
        question,
        answer_en="A materially different answer.",
        reason="Different reason text.",
    )
    with pytest.raises(OperationKeyConflictError, match="不同内容"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=other_content,
            request_key="dec",
        )


def test_conflicting_answer_rejected_and_duplicate_saved_once(space, waiting):
    """A10: save repeated answers once and reject conflicting answers."""
    execution, _, question = waiting
    first = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    assert first.first_save
    again = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    assert not again.first_save
    different_key = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec-2",
    )
    assert not different_key.first_save
    # Reject conflicting contents.
    conflicting = _submission_for(
        question,
        answer_en="A conflicting expert answer.",
        reason="Conflicting reason.",
    )
    with pytest.raises(DecisionConflictError, match="冲突"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=conflicting,
            request_key="dec-3",
        )
    # Only one decision record exists and the question is answered once.
    decisions = [r for r in space.store().list_records() if r.RECORD_NAME == "decision"]
    assert len(decisions) == 1
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.status == "answered"
    assert len(view.decisions) == 1


def test_stale_fingerprint_rejected(space, waiting):
    execution, _, question = waiting
    stale = DecisionSubmission(
        question_ref=question.question_ref,
        fingerprint="0" * 64,
        actor=Actor(kind="human", id="expert"),
        outcome="resolved",
        answer_original="旧内容",
        answer_en="Old content",
        reason="Stale",
        evidence_refs=[],
        patch=None,
    )
    with pytest.raises(RevisionStaleError, match="指纹"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=stale,
            request_key="stale",
        )


def test_unresolved_answer_keeps_question_open_and_independent_tasks_proceed(
    space, service, waiting
):
    """A11: save unresolved answers reliably without patches/exclusion; independent tasks
    can advance.
    """
    execution, _, question = waiting
    receipt = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="unresolved",
        outcome="unresolved",
        patch=None,
    )
    assert receipt.first_save
    view = pending_questions(space, execution_id=execution.execution_id)[0]
    assert view.status == "answered"
    assert view.answered_outcome == "unresolved"
    assert view.application is None
    assert view.needs_processing  # Answered but unresolved still needs handling.
    # Keep the original paper revision with no patch or scientific exclusion.
    from shared_scenarios import material as m

    paper = space.store().read_record_current(
        m.paper_record_id("A", execution.execution_id)
    )
    assert paper.revision == 1
    # Independent paper B can subsequently complete.
    ex_b, flow_b, _ = start_execution(space, paper="B", request_key="b")
    report_b = resume_execution(space, ex_b.execution_id, flow_b, service=service)
    assert report_b.outcome == "finished"
    # Recovery of A still waits instead of treating unresolved science as resolved.
    from shared_scenarios import direct_flow

    report_a = resume_execution(
        space, execution.execution_id, direct_flow(), service=service
    )
    assert report_a.outcome == "paused"
    assert report_a.pending_question is not None


def test_unresolved_with_patch_rejected(space, waiting):
    execution, _, question = waiting
    submission = _submission_for(question, outcome="unresolved")
    with pytest.raises(WorkspaceIntegrityError, match="补丁"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=submission,
            request_key="unresolved-patch",
        )


def test_model_actor_rejected(space, waiting):
    execution, _, question = waiting
    submission = DecisionSubmission(
        question_ref=question.question_ref,
        fingerprint=question.record.data.fingerprint,
        actor=Actor(kind="model", id="synthetic-mock"),
        outcome="resolved",
        answer_original="模型判断",
        answer_en="Model judgment",
        reason="Should be rejected",
        evidence_refs=[],
        patch=None,
    )
    from hvs_catalog.workspace import AdjudicationNotAllowedError

    with pytest.raises(AdjudicationNotAllowedError, match="人类操作者"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=submission,
            request_key="model-actor",
        )


def test_superseded_question_rejects_old_answer(space, waiting):
    """A10: reject stale answers after supersession; historical successful receipts remain
    queryable but never reapply.
    """
    from hvs_catalog.records.rules import load_rules
    from hvs_catalog.review import QuestionContent

    execution, _, question = waiting
    # Answer and apply first.
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    resume_execution(
        space,
        execution.execution_id,
        __import__("shared_scenarios", fromlist=["direct_flow"]).direct_flow(),
        service=service,
    )
    # Changed material supersedes the question with updated context targeting the new paper revision.
    # Supersession must reference current v2 after answering; reject old revisions as stale.
    with pytest.raises(RevisionStaleError, match="只能替代当前修订"):
        ReviewService(space).supersede_question(
            execution_id=execution.execution_id,
            step_id="quantities",
            old_ref=question.question_ref,
            request_key="supersede-stale",
            new_content=QuestionContent(
                category="author_assessment",
                target_refs=[question.record.data.target_refs[0]],
                source_refs=question.record.data.source_refs,
                rules_ref=question.record.data.rules_ref,
                prompt="placeholder",
                evidence_refs=question.record.data.evidence_refs,
            ),
            reason="stale reference",
        )
    current_ref = ReviewService(space).get_question(question.question_ref).question_ref
    assert current_ref.revision == 2
    rules = load_rules()
    from hvs_catalog.records.questions import RulesRef

    new_content = QuestionContent(
        category="author_assessment",
        target_refs=[
            question.record.data.target_refs[0].model_copy(update={"revision": 2})
        ],
        source_refs=question.record.data.source_refs,
        rules_ref=RulesRef(version=rules.version, sha256=rules.sha256),
        prompt="Re-consulted question with updated context after revision.",
        evidence_refs=question.record.data.evidence_refs,
    )
    review = ReviewService(space)
    new_view = review.supersede_question(
        execution_id=execution.execution_id,
        step_id="quantities",
        old_ref=current_ref,
        request_key="supersede",
        new_content=new_content,
        reason="Materials changed after the decision was applied.",
    )
    assert new_view.status == "open"
    old_view = review.get_question(question.question_ref)
    assert old_view.status == "superseded"
    assert old_view.superseded_by == new_view.question_ref
    # Retain historical answers while marking the question superseded.
    assert old_view.decisions
    # Replay the same key to obtain the old successful receipt with a superseded notice.
    replay = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    assert not replay.first_save
    assert replay.question_status == "superseded"
    assert replay.note and "不可再次应用" in replay.note
    # A new key submitting an old answer to a superseded question is stale and rejected.
    with pytest.raises(RevisionStaleError, match="已被替代"):
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=question.question_ref,
            request_key="dec-after-supersede",
        )
    # The new question rejects the old decision because its v1 patch target lies outside current targets;
    # reject the entire submission.
    from shared_scenarios import material as m

    stale_patch = m.expert_decision_patch(
        question.record.data.target_refs[0],
        evidence_ref=question.record.data.evidence_refs[0],
    )
    with pytest.raises(WorkspaceIntegrityError, match="目标范围"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="quantities",
            submission=_submission_for(new_view, patch=stale_patch),
            request_key="dec-new-target",
        )


def _submission_for(question, *, outcome="resolved", patch="default", **overrides):
    from shared_scenarios import material as m

    fields = {
        "question_ref": question.question_ref,
        "fingerprint": question.record.data.fingerprint,
        "actor": Actor(kind="human", id="pipeline-synthetic-expert-01"),
        "outcome": outcome,
        "answer_original": "无总体结论：作者在两种模型下给出相反结论且未作总体判断。",
        "answer_en": (
            "No overall conclusion: the author gives opposite conditional"
            " conclusions and defers an overall assessment."
        ),
        "reason": "Synthetic expert decision fixed in the pipeline mock materials.",
        "evidence_refs": [],
        "patch": (
            m.expert_decision_patch(
                question.record.data.target_refs[0],
                evidence_ref=question.record.data.evidence_refs[0],
            )
            if patch == "default"
            else patch
        ),
    }
    fields.update(overrides)
    return DecisionSubmission(**fields)
