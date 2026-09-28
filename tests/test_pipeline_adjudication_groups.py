"""共享提取入口：裁决开关两组执行（C04，S11/E20 合成正反例）。

同一材料/方法/模型只改开关：开启组暂停等待专家并经原 ReviewService
恢复；关闭组不请求也不等待专家——日常保留未决，评测/合成纯 AI 组可
保存 model_judgments 而不写 Decision。两组执行与决定互相隔离，关闭组
不能消费开启组本轮新增决定。
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
from hvs_catalog.workspace import AdjudicationNotAllowedError, initialize_workspace


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
        method="direct",
        source_refs=[source],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )


def test_s11_two_groups_same_inputs_only_switch_differs(space, service):
    """S11：同论文同材料/方法/模型的开关两组执行。"""
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(space, request=_request(source), request_key="s11-on")
    off = start_extraction(
        space,
        request=_request(source, mode="evaluation", expert=False),
        request_key="s11-off",
    )
    assert on.execution_id != off.execution_id
    assert on.method == off.method == "direct"
    assert on.models == off.models
    assert on.input_digest != off.input_digest

    # 开启组：暂停等待专家；模拟人类决定后恢复完成
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
    # 决定是人类专家决定，应用到论文修订
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

    # 关闭组（评测纯 AI）：不请求不等待专家，完成并保存模型判断
    off_report = resume_extraction(space, off.execution_id, service=service)
    assert off_report.outcome == "finished"
    assert off_report.pending_question is None
    off_status = get_extraction(space, off.execution_id)
    assert off_report.model_judgments == 1
    assert off_status["questions"][0]["status"] == "open"
    # 关闭组论文不被开启组决定修订，输出按执行隔离
    off_paper = f"paper-fic-r05-a-{off.execution_id}"
    assert space.store().current_revision(off_paper) == 1
    assert off_paper != paper_id


def test_off_group_cannot_consume_on_group_decisions(space, service):
    """关闭组拒绝本轮专家决定；不把开启组决定带进关闭组运行。"""
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
    question_off = pending_questions(space, off.execution_id)[0]
    with pytest.raises(AdjudicationNotAllowedError):
        submit_expert_decision(
            space,
            execution_id=off.execution_id,
            question_ref=question_off.question_ref,
            request_key="iso-off-decision",
            choice="no_overall_conclusion",
        )
    # 关闭组固定输入不含开启组本轮新增决定
    off_status = get_extraction(space, off.execution_id)
    assert off_status["config"]["existing_decision_refs"] == []
    # 关闭组论文保持未修订
    assert space.store().current_revision(f"paper-fic-r05-a-{off.execution_id}") == 1


def test_existing_decision_allowlist_is_frozen_and_part_of_digest(space, service):
    """固定允许的决定集合进入快照并参与执行摘要；结构上可核对。"""
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
            method="direct",
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
    # 同请求同输入重试复用，不建立第二个执行
    again = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="direct",
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
    """E20：纯 AI 组尝试解释歧义，仍未决保存为模型结果。"""
    source = import_mock_source(space, PAPER_A)
    pure_ai = start_extraction(
        space,
        request=_request(source, mode="synthetic", expert=False),
        request_key="e20-pure",
    )
    report = resume_extraction(space, pure_ai.execution_id, service=service)
    assert report.outcome == "finished"
    assert service.calls_for_step("interpret_ambiguity") == 1
    status = get_extraction(space, pure_ai.execution_id)
    judgments = status["model_judgments"]
    assert len(judgments) == 1
    assert judgments[0]["step_id"] == "quantities"
    assert "remains unresolved" in judgments[0]["judgment"]
    # 问题保持未决，不是专家决定
    assert status["questions"][0]["status"] == "open"
    decisions = [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
    ]
    assert decisions == []
    # 作者判断保持未决（不伪造结论）
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
    assert boundness.status is None
    assert boundness.issue_refs


def test_daily_off_group_cannot_use_pure_ai_exception(space, service):
    """日常关闭组不启用纯 AI 例外：无解释调用、无模型判断，保留未决。"""
    source = import_mock_source(space, PAPER_A)
    daily_off = start_extraction(
        space,
        request=_request(source, mode="daily", expert=False),
        request_key="daily-off",
    )
    report = resume_extraction(space, daily_off.execution_id, service=service)
    assert report.outcome == "finished"
    assert report.pending_question is None
    assert report.model_judgments == 0
    assert service.calls_for_step("interpret_ambiguity") == 0
    status = get_extraction(space, daily_off.execution_id)
    assert status["questions"][0]["status"] == "open"
    assert status["model_judgments"] == []


def test_expert_on_group_does_not_use_pure_ai_attempt(space, service):
    """开启组走专家等待，不做纯 AI 解释调用。"""
    source = import_mock_source(space, PAPER_A)
    on = start_extraction(
        space, request=_request(source, mode="evaluation"), request_key="eval-on"
    )
    report = resume_extraction(space, on.execution_id, service=service)
    assert report.outcome == "paused"
    assert service.calls_for_step("interpret_ambiguity") == 0
    assert report.model_judgments == 0
