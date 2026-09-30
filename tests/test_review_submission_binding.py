"""裁决身份与准确修订回归：跨空间误投拒绝、不存在/过期修订拒绝。

两个空间对同一论文按相同顺序创建执行与问题时，执行编号相同但问题
编号按逻辑空间派生；原样投递另一空间的提交被拒绝且目标库不新增决
定。提交与查询使用准确修订：不存在的修订、已答复问题答复链之外的
旧修订均拒绝，同请求重试与答复链内的合法重试保持幂等；重放入口同
样只承认原成功回执明确关联的修订。
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
    """两空间同论文同顺序，原样误投被拒且目标库不新增决定。"""
    space_a = initialize_workspace(tmp_path / "a")
    space_b = initialize_workspace(tmp_path / "b")
    try:
        exec_a, question_a = _paused_with_question(space_a, service)
        exec_b, question_b = _paused_with_question(space_b, service)
        # 各空间独立计数的执行编号相同；问题编号因空间身份而不同
        assert exec_a.execution_id == exec_b.execution_id
        assert question_a.record.record_id != question_b.record.record_id
        assert space_a.identity.space_id != space_b.identity.space_id
        # A 空间的完整原样提交（问题引用、指纹、执行、步骤与答复）投入 B
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
        # 目标库不新增任何决定；B 空间自己的问题仍为 open
        decisions = [
            r for r in space_b.store().list_records() if r.RECORD_NAME == "decision"
        ]
        assert decisions == []
        assert question_b.status == "open"
        # B 空间对自己的问题正常提交仍然成功
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
    """提交不存在的问题修订被拒，不产生决定或新修订。"""
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
        # 查询同样拒绝不存在的修订
        with pytest.raises(WorkspaceIntegrityError, match="不存在"):
            ReviewService(space).get_question(nonexistent)
    finally:
        space.close()


def test_answered_question_accepts_only_answer_chain(tmp_path, service):
    """已答复问题只开放答复前后的最短修订链。"""
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
        # 答复前修订（v1）的同内容换键提交：合法重试，返回原决定
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
        # 答复链之外的修订（不存在）拒绝
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


# ---- 重放入口同样核验提交引用 ----


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
    """同请求键携带不存在的 v999 重放被拒（直接覆盖公共服务）。"""
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

        # 原修订（v1）与答复修订（v2）的重放保持幂等
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
    """问题随后被替代后，原成功请求的回执仍可按原修订重放查询。"""
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
        # 决定后问题被替代；替代需新内容，这里复用内容派生新身份
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
