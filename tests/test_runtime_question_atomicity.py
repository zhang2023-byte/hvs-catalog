"""问题创建原子性与贡献响应复用回归。

问题、执行/步骤绑定与回执在同一业务事务保存：事务中途中止不留半份
问题；问题创建后、步骤回执前的中断由新进程恢复——已归档的贡献响应
被复用，不再调用模拟服务，也不与新时间戳比较已保存的论文修订。缺
失绑定不视为通配，错误步骤提交被拒绝。
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
    """问题提交后、步骤回执前真实退出——新进程零新增调用地接续。"""
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
        assert calls_before == 2  # 名单 + 贡献各一次
        # 问题已保存且绑定正确；论文已随问题事务保存
        question = pending_questions(space, execution_id=execution.execution_id)[0]
        assert question.status == "open"
        assert question.step_id == "quantities"
        from shared_scenarios import material as m

        paper_id = m.paper_record_id("A", execution.execution_id)
        assert space.store().current_revision(paper_id) == 1

        # 新进程恢复：复用已归档响应，不重复调用服务，停回等待裁决
        report = resume_execution(
            space, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "paused"
        assert service.summary()["calls"] == calls_before

        # 错误步骤提交被拒绝：绑定不是通配
        with pytest.raises(WorkspaceIntegrityError, match="跨步骤"):
            ReviewService(space).submit_decision(
                execution_id=execution.execution_id,
                step_id="wrong-step",
                submission=_unresolved_submission(question),
                request_key="wrong-step",
            )

        # 正常决定后完成，仍无新增调用
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
    """问题事务中途真实退出——无半份问题、无残留绑定。"""
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
        # 事务未提交：没有问题、没有问题事务保存的论文
        assert pending_questions(space, execution_id=execution.execution_id) == []
        from shared_scenarios import material as m

        paper_id = m.paper_record_id("A", execution.execution_id)
        assert space.store().read_record_current(paper_id) is None
        # 新进程恢复：响应已归档，不重复调用服务；问题完整创建并带绑定
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
