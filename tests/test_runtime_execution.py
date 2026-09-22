"""运行与步骤：固定输入、快照隔离、归档核验与裁决开关（A07/A08/A12）。

合成材料见 ``hvs_catalog.synthetic``；科学内容固定在测试输入中。
"""

from __future__ import annotations

import pytest

from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.runtime import (
    build_input_snapshot,
    create_execution,
    get_execution,
    resume_execution,
)
from hvs_catalog.synthetic import open_service, start_execution
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import (
    OperationKeyConflictError,
    RecoveryInconsistentError,
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


def _snapshot(space, *, expert=True, model="synthetic-mock", method="direct"):
    from hvs_catalog.synthetic.runner import import_materials

    source = import_materials(space, "A")
    return build_input_snapshot(
        source_refs=[source],
        method=method,
        models=[ModelSpec(step_id=None, provider=None, model=model, parameters={})],
        expert_adjudication=expert,
    )


def test_same_request_same_input_reuses_execution(space):
    snapshot = _snapshot(space)
    flow = SyntheticFlow(paper="A")
    first, reused = create_execution(
        space, request_key="req-1", input_snapshot=snapshot, flow=flow
    )
    assert not reused
    second, reused_again = create_execution(
        space, request_key="req-1", input_snapshot=snapshot, flow=flow
    )
    assert reused_again
    assert second.execution_id == first.execution_id
    assert get_execution(space, first.execution_id).input_digest == first.input_digest


def test_same_request_different_input_rejected(space):
    flow = SyntheticFlow(paper="A")
    create_execution(
        space,
        request_key="req-1",
        input_snapshot=_snapshot(space, expert=True),
        flow=flow,
    )
    with pytest.raises(OperationKeyConflictError, match="异输入|不同输入"):
        create_execution(
            space,
            request_key="req-1",
            input_snapshot=_snapshot(space, expert=False),
            flow=flow,
        )


def test_changed_input_creates_new_execution(space):
    flow = SyntheticFlow(paper="A")
    first, _ = create_execution(
        space,
        request_key="req-1",
        input_snapshot=_snapshot(space, expert=True),
        flow=flow,
    )
    second, reused = create_execution(
        space,
        request_key="req-2",
        input_snapshot=_snapshot(space, expert=False),
        flow=flow,
    )
    assert not reused
    assert second.execution_id != first.execution_id
    # 两份运行清单分别固定各自输入
    runs = {
        first.execution_id: space.store().read_record_current(first.run_ref.record_id),
        second.execution_id: space.store().read_record_current(
            second.run_ref.record_id
        ),
    }
    assert runs[first.execution_id].data.input_snapshot.expert_adjudication is True
    assert runs[second.execution_id].data.input_snapshot.expert_adjudication is False


def test_resume_uses_original_snapshot_after_config_change(space, service, tmp_path):
    execution, flow, _ = start_execution(space, paper="B", request_key="b")
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "finished"
    # 本机配置改变不影响已完成的固定输入执行
    space.update_config({"download": {"concurrency": 8}})
    again = resume_execution(space, execution.execution_id, flow, service=service)
    assert again.mode == "already-finished"
    assert again.service_summary["calls"] == 2


def test_missing_source_file_blocks_resume(space, service):
    execution, flow, _ = start_execution(space, paper="B", request_key="b")
    (space.root / "sources/r05/b/paper.tex").unlink()
    with pytest.raises(RecoveryInconsistentError, match="来源文件缺失"):
        resume_execution(space, execution.execution_id, flow, service=service)


def test_modified_source_file_blocks_resume(space, service):
    execution, flow, _ = start_execution(space, paper="B", request_key="b")
    # 篡改来源文件内容后，当前文件不能冒充固定输入
    (space.root / "sources/r05/b/paper.tex").write_bytes(b"tampered\n")
    with pytest.raises(RecoveryInconsistentError, match="哈希改变"):
        resume_execution(space, execution.execution_id, flow, service=service)


def test_missing_rules_archive_blocks_resume(space, service, tmp_path):
    execution, flow, _ = start_execution(space, paper="B", request_key="b")
    from hvs_catalog.runtime import rules_archive_rel

    (space.root / rules_archive_rel(execution.execution_id)).unlink()
    with pytest.raises(RecoveryInconsistentError, match="规则归档缺失"):
        resume_execution(space, execution.execution_id, flow, service=service)


def test_incompatible_flow_version_blocks_resume(space, service):
    execution, _flow, _ = start_execution(space, paper="B", request_key="b")
    mismatch = SyntheticFlow(paper="B")
    mismatch.flow_version = "999"
    with pytest.raises(RecoveryInconsistentError, match="流程适配器不匹配"):
        resume_execution(space, execution.execution_id, mismatch, service=service)


def test_code_version_mismatch_blocks_resume(space, service):
    from hvs_catalog.synthetic.runner import import_materials

    source = import_materials(space, "A")
    snapshot = build_input_snapshot(
        source_refs=[source], expert_adjudication=True
    ).model_copy(update={"code_version": "0.0.0-test"})
    flow = SyntheticFlow(paper="A")
    handle, _ = create_execution(
        space, request_key="version-mismatch", input_snapshot=snapshot, flow=flow
    )
    with pytest.raises(RecoveryInconsistentError, match="代码版本不符"):
        resume_execution(space, handle.execution_id, flow, service=service)


def test_expert_and_pure_ai_groups_isolated(tmp_path):
    """A12：两组使用相同论文；关闭组拒绝本轮专家决定及衍生结果。"""
    expert_space = initialize_workspace(tmp_path / "expert")
    pure_space = initialize_workspace(tmp_path / "pure-ai")
    service = open_service(tmp_path / "ledger.db")
    try:
        ex_e, flow_e, _ = start_execution(expert_space, paper="A", request_key="e")
        r_e = resume_execution(expert_space, ex_e.execution_id, flow_e, service=service)
        assert r_e.outcome == "paused"
        from hvs_catalog.synthetic import pending_questions, submit_expert_decision

        question = pending_questions(expert_space, execution_id=ex_e.execution_id)[0]
        submit_expert_decision(
            expert_space,
            execution_id=ex_e.execution_id,
            question_ref=question.question_ref,
            request_key="e-decision",
        )
        finished = resume_execution(
            expert_space, ex_e.execution_id, flow_e, service=service
        )
        assert finished.outcome == "finished"
        from hvs_catalog.synthetic import material as m

        expert_paper = expert_space.store().read_record_current(
            m.paper_record_id("A", ex_e.execution_id)
        )
        assert expert_paper.revision == 2

        ex_p, flow_p, _ = start_execution(
            pure_space, paper="A", expert_adjudication=False, request_key="p"
        )
        r_p = resume_execution(pure_space, ex_p.execution_id, flow_p, service=service)
        assert r_p.outcome == "finished"
        assert r_p.pending_question is None
        pure_paper = pure_space.store().read_record_current(
            m.paper_record_id("A", ex_p.execution_id)
        )
        assert pure_paper.revision == 1  # 模型判断不进入专家决定
        run_p = pure_space.store().read_record_current(f"run-{ex_p.execution_id}")
        assert run_p.data.model_judgments
        questions_p = pending_questions(pure_space, execution_id=ex_p.execution_id)
        assert [q.status for q in questions_p] == ["open"]

        # 关闭组提交专家决定被拒绝；专家组的决定不能移植到关闭组
        from hvs_catalog.workspace import AdjudicationNotAllowedError

        with pytest.raises(AdjudicationNotAllowedError):
            submit_expert_decision(
                pure_space,
                execution_id=ex_p.execution_id,
                question_ref=questions_p[0].question_ref,
                request_key="p-decision",
            )
    finally:
        expert_space.close()
        pure_space.close()
        service.close()


def test_cross_execution_decision_rejected_in_same_space(space, service):
    """换执行标识不能绕过隔离：跨执行提交决定被拒绝。"""
    from hvs_catalog.synthetic import pending_questions, submit_expert_decision
    from hvs_catalog.workspace import WorkspaceIntegrityError

    ex_b, flow_b, _ = start_execution(space, paper="B", request_key="b")
    resume_execution(space, ex_b.execution_id, flow_b, service=service)
    # 另一空间的执行 A 的问题不存在于本空间
    other = initialize_workspace(space.root.parent / "other-space")
    try:
        ex_a, flow_a, _ = start_execution(other, paper="A", request_key="a")
        resume_execution(other, ex_a.execution_id, flow_a, service=service)
        question = pending_questions(other, execution_id=ex_a.execution_id)[0]
        # 跨空间提交：另一空间的问题在本空间不存在，明确拒绝
        with pytest.raises(WorkspaceIntegrityError, match="未知问题|不属于执行|不存在"):
            submit_expert_decision(
                space,
                execution_id=ex_b.execution_id,
                question_ref=question.question_ref,
                request_key="cross",
            )
    finally:
        other.close()


def test_step_results_cached_within_execution_only(space, service, tmp_path):
    """首版步骤缓存限定在本执行内：另一执行不自动复用中间结果。"""
    ex1, flow1, _ = start_execution(space, paper="B", request_key="b1")
    resume_execution(space, ex1.execution_id, flow1, service=service)
    calls_after_first = service.summary()["calls"]
    # 相同输入的另一次执行（不同请求键、不同空间记录）重新走完整链路
    other = initialize_workspace(tmp_path / "other")
    try:
        ex2, flow2, _ = start_execution(other, paper="B", request_key="b2")
        report = resume_execution(other, ex2.execution_id, flow2, service=service)
        assert report.outcome == "finished"
        assert service.summary()["calls"] > calls_after_first
    finally:
        other.close()


def test_unknown_execution_rejected(space):
    with pytest.raises(WorkspaceIntegrityError, match="未知执行"):
        get_execution(space, "exec-does-not-exist")
