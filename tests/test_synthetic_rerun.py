"""同空间重跑与换配置新执行的输出隔离回归。

有意重跑使用新请求键：第二次执行走完预期路径，提取输出（名单、论文
贡献、对象、问题）按执行隔离编号，与第一次执行的结果、决定及回执互
不混用；可共享的固定来源与证据按不可变修订复用。
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
    """同空间 B 篇有意重跑，两个执行各自完成。"""
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

        # 输出按执行隔离：两次执行的名单/论文/对象编号互不相同
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
        # 固定来源与证据复用：同内容不可变修订只保存一次
        sources = [
            r for r in space.store().list_records() if r.RECORD_NAME == "source_package"
        ]
        assert len(sources) == 1
        evidences = [
            r for r in space.store().list_records() if r.RECORD_NAME == "evidence"
        ]
        assert len(evidences) == 3
        # 回执按执行隔离，不混用
        first_receipts = space.store().find_receipts(execution_id=first.execution_id)
        second_receipts = space.store().find_receipts(execution_id=second.execution_id)
        assert first_receipts and second_receipts
        assert {r.operation_key for r in first_receipts}.isdisjoint(
            {r.operation_key for r in second_receipts}
        )
        # 两个执行都实际调用了服务（步骤缓存限定在本执行内）
        assert service.calls_for_step("direct_roster") == 2
    finally:
        space.close()


def test_same_space_config_change_creates_isolated_execution(tmp_path, service):
    """A 篇专家组完成后，关闭裁决建立新执行并完成。"""
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

        # 新执行输出独立：自己的名单/论文/对象，论文不被专家决定修订
        pure_paper = m.paper_record_id("A", pure.execution_id)
        assert space.store().current_revision(pure_paper) == 1
        assert space.store().current_revision(expert_paper) == 2
        pure_object = m.object_record_id("A", pure.execution_id)
        assert space.store().current_revision(pure_object) == 1
        # 新执行保存了自己的问题（未决），与专家组决定不混用
        pure_questions = pending_questions(space, execution_id=pure.execution_id)
        assert [q.status for q in pure_questions] == ["open"]
        assert pure_questions[0].record.record_id != question.record.record_id
        assert pure_questions[0].decisions == []
        # 模型判断记入新执行运行清单，不进入专家决定
        run_p = space.store().read_record_current(f"run-{pure.execution_id}")
        assert run_p.data.model_judgments
    finally:
        space.close()
