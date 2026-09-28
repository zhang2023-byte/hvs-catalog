"""共享提取入口：candidate_review 模拟方法（C02/C03）。

候选发现→逐个判定→共用数值/收尾；纳入/排除/未决三种处置都保留：
被排除对象没有贡献，未决对象不自动排除，专家决定可把未决条目改为
收录后进入数值步骤。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hvs_catalog.pipeline import (
    PAPER_C,
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
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.questions import DecisionRecord
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.workspace import initialize_workspace

FIXTURES = Path(__file__).parent / "fixtures" / "pipeline"
MATERIALS = json.loads((FIXTURES / "materials.json").read_text(encoding="utf-8"))
EXPECTED_ENTRIES = {
    entry["item_id"]: entry["disposition"]
    for entry in MATERIALS["papers"]["FIC-CANDIDATE-C"]["roster_entries"]
}


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _start(space, source, *, request_key, mode="daily", expert=True, models=None):
    request = ExtractionRequest(
        mode=mode,
        method="candidate_review",
        source_refs=[source],
        models=models or [StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _record(space, record_id):
    revision = space.store().current_revision(record_id)
    assert revision is not None
    record = space.store().read_record(
        RecordRef(record_id=record_id, revision=revision)
    )
    assert record is not None
    return record


def test_discovery_and_per_candidate_review_calls(space, service):
    """发现一次调用，逐候选各一次调用；固定处置与夹具一致。"""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-calls", expert=False)
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    assert service.calls_for_step("discover_candidates") == 1
    assert service.calls_for_step("review_candidates") == 3
    status = get_extraction(space, handle.execution_id)
    # 每次调用的请求/响应与用量都归档，不互相覆盖
    attempts = {step["step_id"]: step["attempts"] for step in status["steps"]}
    assert attempts["discover_candidates"] == 1
    assert attempts["review_candidates"] == 3
    assert attempts["quantities"] == 1
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    assert isinstance(roster, RosterRecord)
    assert roster.data.method == "candidate_review"
    assert {e.item_id: e.disposition for e in roster.data.entries} == EXPECTED_ENTRIES


def test_pending_not_auto_excluded_without_expert(space, service):
    """关闭裁决的日常运行：未决条目保留未决，问题保持待处理。"""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-off-daily", expert=False)
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "pending"
    assert entry.issue_refs  # 未决条目关联未决问题
    questions = pending_questions(space, handle.execution_id)
    assert len(questions) == 1
    assert questions[0].status == "open"
    # 没有任何专家决定被写入
    decisions = [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
    ]
    assert decisions == []
    # 被排除对象没有贡献与对象记录
    paper = _record(space, f"paper-fic-candidate-c-{handle.execution_id}")
    assert isinstance(paper, PaperRecord)
    assert [c.roster_item_id for c in paper.data.object_contributions] == ["cand-c-1"]


def test_expert_group_pauses_and_include_decision_adds_quantities(space, service):
    """开启组暂停在候选判定；收录决定后未决条目进入数值步骤。"""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-on")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    assert first.pending_question is not None
    question = pending_questions(space, handle.execution_id)[0]
    submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="dec-c-include",
        choice="include",
    )
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "finished"
    assert second.errors == []
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    assert space.store().current_revision(roster.record_id) == 2
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "included"
    assert entry.contribution_id == "contribution-c-3"
    paper = _record(space, f"paper-fic-candidate-c-{handle.execution_id}")
    assert [c.roster_item_id for c in paper.data.object_contributions] == [
        "cand-c-1",
        "cand-c-3",
    ]
    values = {
        c.roster_item_id: c.quantities[0].values[0].value
        for c in paper.data.object_contributions
    }
    expected = {
        item["roster_item_id"]: item["value"]
        for item in MATERIALS["papers"]["FIC-CANDIDATE-C"]["values"]
    }
    assert values == expected
    objects = [
        ref
        for ref in get_extraction(space, handle.execution_id)["result_refs"]
        if ref["record_id"].startswith("obj-")
    ]
    assert len(objects) == 2


def test_unresolved_decision_keeps_run_waiting(space, service):
    """专家答复仍未决：决定保存，步骤保留等待，不自动排除。"""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-unresolved")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "paused"
    question = pending_questions(space, handle.execution_id)[0]
    receipt = submit_expert_decision(
        space,
        execution_id=handle.execution_id,
        question_ref=question.question_ref,
        request_key="dec-c-pending",
        choice="keep_pending",
    )
    assert receipt.first_save is True
    second = resume_extraction(space, handle.execution_id, service=service)
    assert second.outcome == "paused"
    assert second.pending_question["answered_outcome"] == "unresolved"
    roster = _record(space, f"roster-fic-candidate-c-{handle.execution_id}")
    entry = next(e for e in roster.data.entries if e.item_id == "cand-c-3")
    assert entry.disposition == "pending"


def test_review_step_model_selection(space, service):
    """逐候选判定使用逐步指定的模型，发现步骤用默认 fallback。"""
    source = import_mock_source(space, PAPER_C)
    models = [
        StepModel(model="mock-default"),
        StepModel(step_id="review_candidates", model="mock-review"),
    ]
    handle = _start(space, source, request_key="c-models", expert=False, models=models)
    resume_extraction(space, handle.execution_id, service=service)
    used = {call["step"]: call["model"] for call in service.calls}
    assert used["discover_candidates"] == "mock-default"
    assert used["review_candidates"] == "mock-review"


def test_candidate_failure_preserves_discovery(space, service):
    """逐候选判定失败：整篇失败，发现步骤结果保留并可复用。"""

    def fail_review():
        raise RuntimeError("simulated review failure")

    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.pipeline.methods import CANDIDATE_REVIEW_METHOD
    from hvs_catalog.runtime import resume_execution

    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-fail", expert=False)
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            CANDIDATE_REVIEW_METHOD,
            mode="daily",
            fault_hooks={"after_response:review_candidates": fail_review},
        ),
        service=service,
    )
    assert report.outcome == "failed"
    status = get_extraction(space, handle.execution_id)
    states = {step["step_id"]: step["state"] for step in status["steps"]}
    assert states["discover_candidates"] == "finished"
    assert service.calls_for_step("discover_candidates") == 1
    recovered = resume_extraction(space, handle.execution_id, service=service)
    assert recovered.outcome == "finished"
    assert service.calls_for_step("discover_candidates") == 1
