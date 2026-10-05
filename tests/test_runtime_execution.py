"""Runtime/steps: frozen inputs, snapshot isolation, archive checks, and review switches
(A07/A08/A12).

Synthetic materials come from hvs_catalog.synthetic; scientific contents are fixed in
test inputs.
"""

from __future__ import annotations

import pytest
from shared_scenarios import direct_flow, open_service, start_execution

from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.runtime import (
    build_input_snapshot,
    create_execution,
    get_execution,
    resume_execution,
)
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
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _snapshot(space, *, expert=True, model="synthetic-mock", method="roster"):
    from shared_scenarios import import_materials

    source = import_materials(space, "A")
    return build_input_snapshot(
        source_refs=[source],
        method=method,
        models=[ModelSpec(step_id=None, provider=None, model=model, parameters={})],
        expert_adjudication=expert,
    )


def test_same_request_same_input_reuses_execution(space):
    snapshot = _snapshot(space)
    flow = direct_flow()
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
    flow = direct_flow()
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
    flow = direct_flow()
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
    # Separate run manifests freeze their own inputs.
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
    # Local configuration changes cannot affect completed frozen-input execution.
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
    # Tampered current source files cannot impersonate fixed input.
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
    mismatch = direct_flow()
    mismatch.flow_version = "999"
    with pytest.raises(RecoveryInconsistentError, match="流程适配器不匹配"):
        resume_execution(space, execution.execution_id, mismatch, service=service)


def test_code_version_mismatch_blocks_resume(space, service):
    from shared_scenarios import import_materials

    source = import_materials(space, "A")
    snapshot = build_input_snapshot(
        source_refs=[source], expert_adjudication=True
    ).model_copy(update={"code_version": "0.0.0-test"})
    flow = direct_flow()
    handle, _ = create_execution(
        space, request_key="version-mismatch", input_snapshot=snapshot, flow=flow
    )
    with pytest.raises(RecoveryInconsistentError, match="代码版本不符"):
        resume_execution(space, handle.execution_id, flow, service=service)


def test_expert_and_pure_ai_groups_isolated(tmp_path):
    """A12: groups use identical papers; the disabled group rejects current-round expert
    decisions and derived results.
    """
    expert_space = initialize_workspace(tmp_path / "expert")
    pure_space = initialize_workspace(tmp_path / "pure-ai")
    service = open_service(tmp_path / "calls.jsonl")
    try:
        ex_e, flow_e, _ = start_execution(expert_space, paper="A", request_key="e")
        r_e = resume_execution(expert_space, ex_e.execution_id, flow_e, service=service)
        assert r_e.outcome == "paused"
        from shared_scenarios import pending_questions, submit_expert_decision

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
        from shared_scenarios import material as m

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
        assert (
            pure_paper.revision == 1
        )  # Model judgments never become expert decisions.
        run_p = pure_space.store().read_record_current(f"run-{ex_p.execution_id}")
        assert run_p.data.model_judgments
        questions_p = pending_questions(pure_space, execution_id=ex_p.execution_id)
        assert questions_p == []

        # Reject expert submissions to the disabled group and transplantation of enabled-group decisions.
        from hvs_catalog.workspace import WorkspaceIntegrityError

        with pytest.raises(WorkspaceIntegrityError):
            submit_expert_decision(
                pure_space,
                execution_id=ex_p.execution_id,
                question_ref=question.question_ref,
                request_key="p-decision",
            )
    finally:
        expert_space.close()
        pure_space.close()
        service.close()


def test_cross_execution_decision_rejected_in_same_space(space, service):
    """Changing execution IDs cannot bypass isolation; reject cross-execution decisions."""
    from shared_scenarios import pending_questions, submit_expert_decision

    from hvs_catalog.workspace import WorkspaceIntegrityError

    ex_b, flow_b, _ = start_execution(space, paper="B", request_key="b")
    resume_execution(space, ex_b.execution_id, flow_b, service=service)
    # Another workspace's execution-A question does not exist here.
    other = initialize_workspace(space.root.parent / "other-space")
    try:
        ex_a, flow_a, _ = start_execution(other, paper="A", request_key="a")
        resume_execution(other, ex_a.execution_id, flow_a, service=service)
        question = pending_questions(other, execution_id=ex_a.execution_id)[0]
        # Reject foreign submissions explicitly because their questions do not exist in this workspace.
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
    """Initial step caching is execution-local; another execution never automatically
    reuses intermediate results.
    """
    ex1, flow1, _ = start_execution(space, paper="B", request_key="b1")
    resume_execution(space, ex1.execution_id, flow1, service=service)
    calls_after_first = service.summary()["calls"]
    # Identical input with a new request key/workspace record reruns the complete chain.
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
