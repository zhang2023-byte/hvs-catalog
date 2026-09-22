"""待办与决定：绑定、重放、冲突与过期拒绝、未决答复保留（A09/A10/A11）。"""

from __future__ import annotations

import pytest

from hvs_catalog.records.questions import Actor
from hvs_catalog.review import DecisionSubmission, ReviewService, derive_question_id
from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import (
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)
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
    svc = open_service(tmp_path / "ledger.db")
    yield svc
    svc.close()


@pytest.fixture()
def waiting(space, service):
    """跑 A 篇到等待裁决；返回 (execution, flow, question)。"""
    execution, flow, _ = start_execution(space, paper="A", request_key="a")
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "paused"
    question = pending_questions(space, execution_id=execution.execution_id)[0]
    return execution, flow, question


def test_create_question_replay_returns_original(space, waiting):
    execution, _, question = waiting
    assert question.status == "open"
    assert question.needs_processing
    # 同请求同内容：编号派生自空间、执行与内容，重建得到同一问题
    from hvs_catalog.records.common import RecordRef
    from hvs_catalog.records.rules import load_rules
    from hvs_catalog.synthetic import material as m

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
            step_id="extract_roster",
            submission=_submission_for(question),
            request_key="wrong-step",
        )


def test_same_display_question_different_context_not_reused(space, service, waiting):
    """A09：相同显示问题不造成决定串用——指纹按实际固定内容计算。"""
    other = initialize_workspace(space.root.parent / "second")
    try:
        ex2, flow2, _ = start_execution(other, paper="A", request_key="a")
        resume_execution(other, ex2.execution_id, flow2, service=service)
        q2 = pending_questions(other, execution_id=ex2.execution_id)[0]
        # 同一显示文本在两个空间各自存在；决定保存互不串用
        assert q2.record.data.prompt == _q_prompt(space)
        submit_expert_decision(
            other,
            execution_id=ex2.execution_id,
            question_ref=q2.question_ref,
            request_key="dec-2",
        )
        # 第一个空间的问题仍为 open，没有被动使用其他空间的决定
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
            step_id="extract_contribution",
            submission=other_content,
            request_key="dec",
        )


def test_conflicting_answer_rejected_and_duplicate_saved_once(space, waiting):
    """A10：重复答复只保存一次；冲突答复拒绝。"""
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
    # 冲突内容被拒绝
    conflicting = _submission_for(
        question,
        answer_en="A conflicting expert answer.",
        reason="Conflicting reason.",
    )
    with pytest.raises(DecisionConflictError, match="冲突"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="extract_contribution",
            submission=conflicting,
            request_key="dec-3",
        )
    # 只有一份决定记录；问题只答复一次
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
            step_id="extract_contribution",
            submission=stale,
            request_key="stale",
        )


def test_unresolved_answer_keeps_question_open_and_independent_tasks_proceed(
    space, service, waiting
):
    """A11：答复仍未决——可靠保存、不应用补丁、不排除对象、独立任务可推进。"""
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
    assert view.needs_processing  # 已答复仍未决，仍需处理
    # 论文保持原修订：没有补丁、没有科学排除
    from hvs_catalog.synthetic import material as m

    paper = space.store().read_record_current(
        m.paper_record_id("A", execution.execution_id)
    )
    assert paper.revision == 1
    # 独立任务（B 篇）可以随后推进到完成
    ex_b, flow_b, _ = start_execution(space, paper="B", request_key="b")
    report_b = resume_execution(space, ex_b.execution_id, flow_b, service=service)
    assert report_b.outcome == "finished"
    # A 篇恢复后仍停在等待，不把未决执行成科学解决
    from hvs_catalog.synthetic.flow import SyntheticFlow

    report_a = resume_execution(
        space, execution.execution_id, SyntheticFlow(paper="A"), service=service
    )
    assert report_a.outcome == "paused"
    assert report_a.pending_question is not None


def test_unresolved_with_patch_rejected(space, waiting):
    execution, _, question = waiting
    submission = _submission_for(question, outcome="unresolved")
    with pytest.raises(WorkspaceIntegrityError, match="补丁"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="extract_contribution",
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
            step_id="extract_contribution",
            submission=submission,
            request_key="model-actor",
        )


def test_superseded_question_rejects_old_answer(space, waiting):
    """A10：替代问题后提交旧答复被拒；旧成功回执可查但不重新生效。"""
    from hvs_catalog.records.rules import load_rules
    from hvs_catalog.review import QuestionContent

    execution, _, question = waiting
    # 先答复并应用
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    resume_execution(
        space,
        execution.execution_id,
        __import__(
            "hvs_catalog.synthetic.flow", fromlist=["SyntheticFlow"]
        ).SyntheticFlow(paper="A"),
        service=service,
    )
    # 材料变化：替代问题（同一目标的更新上下文，指向论文新修订的条目）。
    # 替代必须引用当前修订（答复后为 v2）；引用旧修订按过期拒绝
    with pytest.raises(RevisionStaleError, match="只能替代当前修订"):
        ReviewService(space).supersede_question(
            execution_id=execution.execution_id,
            step_id="extract_contribution",
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
        step_id="extract_contribution",
        old_ref=current_ref,
        request_key="supersede",
        new_content=new_content,
        reason="Materials changed after the decision was applied.",
    )
    assert new_view.status == "open"
    old_view = review.get_question(question.question_ref)
    assert old_view.status == "superseded"
    assert old_view.superseded_by == new_view.question_ref
    # 旧答复保留（历史可查），问题显示已失效
    assert old_view.decisions
    # 旧成功回执可查：同键重放仍返回原回执，并提示已失效
    replay = submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    assert not replay.first_save
    assert replay.question_status == "superseded"
    assert replay.note and "不可再次应用" in replay.note
    # 向失效问题换新键提交旧答复：过期拒绝
    with pytest.raises(RevisionStaleError, match="已被替代"):
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=question.question_ref,
            request_key="dec-after-supersede",
        )
    # 新问题不接受旧决定：旧补丁目标（修订 1）不在新问题的目标范围内，
    # 整体拒绝
    from hvs_catalog.synthetic import material as m

    stale_patch = m.expert_decision_patch(
        question.record.data.target_refs[0],
        evidence_ref=question.record.data.evidence_refs[0],
    )
    with pytest.raises(WorkspaceIntegrityError, match="目标范围"):
        ReviewService(space).submit_decision(
            execution_id=execution.execution_id,
            step_id="extract_contribution",
            submission=_submission_for(new_view, patch=stale_patch),
            request_key="dec-new-target",
        )


def _submission_for(question, *, outcome="resolved", patch="default", **overrides):
    from hvs_catalog.synthetic import material as m

    fields = {
        "question_ref": question.question_ref,
        "fingerprint": question.record.data.fingerprint,
        "actor": Actor(kind="human", id="synthetic-expert-01"),
        "outcome": outcome,
        "answer_original": "无总体结论：作者在两种模型下给出相反结论且未作总体判断。",
        "answer_en": (
            "No overall conclusion: the author gives opposite conditional"
            " conclusions and defers an overall assessment."
        ),
        "reason": "Synthetic expert decision fixed in R05 test inputs.",
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
