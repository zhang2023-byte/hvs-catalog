"""公开检查点 ID 完成凭据；缺失拒绝，终态前后中断安全恢复。"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from shared_scenarios import direct_flow, open_service, start_execution

from hvs_catalog.runtime import RuntimeContext, get_execution, resume_execution
from hvs_catalog.workspace import (
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    open_workspace,
    restore_workspace,
)
from hvs_catalog.workspace.backup import framework_final_checkpoint, framework_final_key


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _finished(space, service):
    execution, flow, _ = start_execution(space, paper="B", request_key="cred")
    assert (
        resume_execution(space, execution.execution_id, flow, service=service).outcome
        == "finished"
    )
    return execution


@pytest.mark.parametrize("credential", [None, "wrong-checkpoint"])
def test_missing_or_wrong_credential_rejected_without_backfill(
    tmp_path, service, credential
):
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        key = framework_final_key(execution.execution_id)
        with space.store().transaction() as tx:
            if credential is None:
                tx.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                tx.conn.execute(
                    "UPDATE meta SET value = ? WHERE key = ?", (credential, key)
                )
        before = len(space.store().list_records())
        calls = service.summary()["calls"]
        with pytest.raises(RecoveryInconsistentError, match="完成凭据|已偏离"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
        assert framework_final_checkpoint(space, execution.execution_id) == credential
        assert len(space.store().list_records()) == before
        assert service.summary()["calls"] == calls
        with pytest.raises(WorkspaceIntegrityError, match="完成凭据|已偏离"):
            backup_workspace(space, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        space.close()


def test_new_checkpoint_after_completion_rejected(tmp_path, service):
    from langgraph.checkpoint.sqlite import SqliteSaver

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        proof = framework_final_checkpoint(space, execution.execution_id)
        conn = space.checkpoint_connection()
        try:
            flow = direct_flow()
            ctx = RuntimeContext(
                space=space,
                execution=get_execution(space, execution.execution_id),
                flow=flow,
                service=service,
                checkpointer=SqliteSaver(conn),
            )
            graph = flow.build_graph(ctx)
            graph.update_state(
                {"configurable": {"thread_id": execution.execution_id}},
                {},
                as_node="direct_roster",
            )
        finally:
            conn.close()
        with pytest.raises(WorkspaceIntegrityError, match="已偏离"):
            backup_workspace(space, tmp_path / "backup")
        with pytest.raises(RecoveryInconsistentError, match="待执行节点|已偏离"):
            resume_execution(space, execution.execution_id, flow, service=service)
        assert framework_final_checkpoint(space, execution.execution_id) == proof
    finally:
        space.close()


_ATOMIC_DRIVER = """
import os
import sys
from pathlib import Path
from hvs_catalog.runtime import resume_execution
from shared_scenarios import open_service
from shared_scenarios import direct_flow
from hvs_catalog.workspace import open_workspace
from hvs_catalog.workspace.store import RecordStore, StoreTransaction

old_commit = RecordStore.commit
insert_receipt = StoreTransaction.insert_receipt

def hook(self, request):
    if request.meta_updates:
        if sys.argv[4] == "before_commit":
            def crash(tx, receipt):
                insert_receipt(tx, receipt)
                os._exit(74)
            StoreTransaction.insert_receipt = crash
        result = old_commit(self, request)
        os._exit(75)
    return old_commit(self, request)

RecordStore.commit = hook
space = open_workspace(Path(sys.argv[1]))
service = open_service(Path(sys.argv[2]))
resume_execution(space, sys.argv[3], direct_flow(), service=service)
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    ("window", "exit_code"), [("before_commit", 74), ("after_commit", 75)]
)
def test_finish_transaction_interruption_windows(tmp_path, service, window, exit_code):
    """图已结束但完成未提交／完成已提交：新进程恢复零新增调用。"""
    root = tmp_path / "space"
    space = initialize_workspace(root)
    execution, _, _ = start_execution(space, paper="B", request_key=f"atomic-{window}")
    space.close()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_ATOMIC_DRIVER),
            str(root),
            str(tmp_path / "calls.jsonl"),
            execution.execution_id,
            window,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr
    reopened = open_workspace(root)
    try:
        run = reopened.store().read_record_current(execution.run_ref.record_id)
        proof = framework_final_checkpoint(reopened, execution.execution_id)
        assert (run.data.state == "finished") == (window == "after_commit")
        assert (proof is not None) == (window == "after_commit")
        calls = service.summary()["calls"]
        assert (
            resume_execution(
                reopened,
                execution.execution_id,
                direct_flow(),
                service=service,
            ).outcome
            == "finished"
        )
        assert service.summary()["calls"] == calls
        assert framework_final_checkpoint(reopened, execution.execution_id) is not None
        backup_workspace(reopened, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        try:
            assert (
                resume_execution(
                    restored,
                    execution.execution_id,
                    direct_flow(),
                    service=service,
                ).mode
                == "already-finished"
            )
        finally:
            restored.close()
    finally:
        reopened.close()
