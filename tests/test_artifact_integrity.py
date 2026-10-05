"""Regression tests for archive and checkpoint integrity.

Commits verify the current batch's reference closure. Execution recovery verifies its
history, including requests/responses, and dependencies; full backup and restore verify
all history. Check original hashes and checkpoint execution/state bindings even for
completed runs. Reject matching thread identifiers with mismatched contents. Validate
the complete checkpoint before applying decisions; completion requires both business and
framework completion.
"""

from __future__ import annotations

import hashlib
import json
import shutil

import pytest
from shared_scenarios import (
    direct_flow,
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)

from hvs_catalog.records.runs import Attempt
from hvs_catalog.review import ReviewService
from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _finished(space, service, *, paper="B", request_key="b"):
    execution, flow, _ = start_execution(space, paper=paper, request_key=request_key)
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "finished"
    return execution


def _add_attempt_with_response(space, execution, response_path, response_bytes):
    """Append and commit a roster-step attempt referencing the response file."""
    from hvs_catalog.workspace import CommitRequest

    ref = space.publish_run_file(execution.execution_id, response_path, response_bytes)
    store = space.store()
    run_id = f"run-{execution.execution_id}"
    run = store.read_record_current(run_id)
    steps = list(run.data.steps)
    target = next(i for i, s in enumerate(steps) if s.step_id == "roster")
    attempt = Attempt(
        request=None,
        response=ref,
        state="succeeded",
        started_at=None,
        finished_at=None,
        usage=None,
        cost=None,
        currency=None,
        service_request_id=None,
    )
    steps[target] = steps[target].model_copy(update={"attempts": [attempt]})
    updated = run.model_copy(
        update={
            "revision": run.revision + 1,
            "data": run.data.model_copy(update={"steps": steps}),
        }
    )
    store.commit(
        CommitRequest(
            operation_key=f"add-response:{execution.execution_id}",
            kind="run_update",
            new_records=[updated],
            expected_revisions={run.record_id: run.revision},
            execution_id=execution.execution_id,
        )
    )
    return ref


def test_commit_rejects_corrupt_response_reference(tmp_path, service):
    """Reject a run revision referencing a corrupted response file."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        ref = space.publish_run_file(
            execution.execution_id, "responses/integrity-fixture.json", b'{"ok": true}'
        )
        # Corrupt the file before committing a record referencing it.
        (space.root / ref.path).write_bytes(b'{"evidence": "corrupt"}')
        from hvs_catalog.workspace import CommitRequest

        store = space.store()
        run_id = f"run-{execution.execution_id}"
        run = store.read_record_current(run_id)
        steps = list(run.data.steps)
        target = next(i for i, s in enumerate(steps) if s.step_id == "roster")
        attempt = Attempt(
            request=None,
            response=ref,
            state="succeeded",
            started_at=None,
            finished_at=None,
            usage=None,
            cost=None,
            currency=None,
            service_request_id=None,
        )
        steps[target] = steps[target].model_copy(update={"attempts": [attempt]})
        updated = run.model_copy(
            update={
                "revision": run.revision + 1,
                "data": run.data.model_copy(update={"steps": steps}),
            }
        )
        with pytest.raises(WorkspaceIntegrityError, match="哈希与记录不符"):
            store.commit(
                CommitRequest(
                    operation_key="commit-corrupt",
                    kind="run_update",
                    new_records=[updated],
                    expected_revisions={run.record_id: run.revision},
                    execution_id=execution.execution_id,
                )
            )
    finally:
        space.close()


def test_post_commit_corruption_blocks_backup_and_finished_resume(tmp_path, service):
    """Reject backup after post-commit response corruption; completed recovery still
    verifies integrity.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        ref = _add_attempt_with_response(
            space,
            execution,
            "responses/integrity-fixture.json",
            b'{"evidence": "original"}',
        )
        # Corrupt committed file contents while retaining the record's original hash.
        (space.root / ref.path).write_bytes(b'{"evidence": "corrupt"}')
        with pytest.raises(WorkspaceIntegrityError, match="哈希与记录不符"):
            backup_workspace(space, tmp_path / "backup")
        # Completed recovery also verifies archives instead of only reporting already-finished.
        with pytest.raises(RecoveryInconsistentError, match="哈希与记录不符"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


def test_missing_response_file_blocks_backup_and_resume(tmp_path, service):
    """Reject backup and recovery when the response file is missing."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        ref = _add_attempt_with_response(
            space,
            execution,
            "responses/integrity-fixture.json",
            b'{"evidence": "original"}',
        )
        (space.root / ref.path).unlink()
        with pytest.raises(WorkspaceIntegrityError, match="缺失或不可读"):
            backup_workspace(space, tmp_path / "backup")
        with pytest.raises(RecoveryInconsistentError, match="缺失或不可读"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


def test_symlinked_intermediate_directory_rejected(tmp_path, service):
    """Reject an archive whose intermediate directory was replaced by a symlink."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        ref = _add_attempt_with_response(
            space, execution, "test-window/roster.json", b'{"evidence": "original"}'
        )
        target = space.root / ref.path
        payload = target.read_bytes()
        target.unlink()
        link_base = tmp_path / "outside"
        link_base.mkdir()
        (link_base / "roster.json").write_bytes(payload)
        target.parent.rmdir()
        target.parent.symlink_to(link_base)
        with pytest.raises(WorkspaceIntegrityError):
            backup_workspace(space, tmp_path / "backup")
    finally:
        space.close()


def _tamper_checkpoint_binding(space, execution_id, new_binding):
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = space.checkpoint_connection()
    saver = SqliteSaver(conn)
    from hvs_catalog.runtime import RuntimeContext
    from hvs_catalog.runtime.execution import get_execution

    ctx = RuntimeContext(
        space=space,
        execution=get_execution(space, execution_id),
        flow=direct_flow(),
        service=None,
        checkpointer=saver,
    )
    graph = direct_flow().build_graph(ctx)
    graph.update_state(
        {"configurable": {"thread_id": execution_id}},
        {"execution_id": new_binding},
    )
    conn.close()


def test_checkpoint_content_mismatch_blocks_backup(tmp_path, service):
    """Reject backup when the thread identifier matches but checkpoint bindings do not."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, flow, _ = start_execution(space, paper="A", request_key="a")
        report = resume_execution(space, execution.execution_id, flow, service=service)
        assert report.outcome == "paused"
        _tamper_checkpoint_binding(space, execution.execution_id, "foreign-execution")
        with pytest.raises(WorkspaceIntegrityError, match="绑定执行 foreign-execution"):
            backup_workspace(space, tmp_path / "backup")
        # Recovery also detects the mismatch during checkpoint loading and stops.
        with pytest.raises(RecoveryInconsistentError, match="foreign-execution"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


def test_finished_execution_validates_checkpoint_content(tmp_path, service):
    """Completed state does not bypass checkpoint content checks."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        _tamper_checkpoint_binding(space, execution.execution_id, "foreign-execution")
        with pytest.raises(RecoveryInconsistentError, match="foreign-execution"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


def test_finished_execution_with_missing_result_record_stops(tmp_path, service):
    """Reject completion when the record is complete but business results are missing."""
    import sqlite3

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        from shared_scenarios import material as m

        roster_id = m.roster_record_id("B", execution.execution_id)
        conn = sqlite3.connect(space.root / "business.db")
        conn.execute("DELETE FROM records WHERE record_id = ?", (roster_id,))
        conn.execute("DELETE FROM current_revisions WHERE record_id = ?", (roster_id,))
        conn.commit()
        conn.close()
        with pytest.raises(
            RecoveryInconsistentError, match="业务结果缺失|业务结果不完整"
        ):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


# ---- Validate before applying ----


def test_pending_decision_not_applied_when_checkpoint_corrupt(tmp_path, service):
    """Validate before applying pending decisions; a bad checkpoint leaves results
    unchanged.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        execution, flow, _ = start_execution(space, paper="A", request_key="a")
        report = resume_execution(space, execution.execution_id, flow, service=service)
        assert report.outcome == "paused"
        question = pending_questions(space, execution_id=execution.execution_id)[0]
        submit_expert_decision(
            space,
            execution_id=execution.execution_id,
            question_ref=question.question_ref,
            request_key="dec",
        )
        target = question.record.data.target_refs[0].record_id
        _tamper_checkpoint_binding(space, execution.execution_id, "foreign-execution")
        with pytest.raises(RecoveryInconsistentError, match="foreign-execution"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
        # Retain the saved decision while leaving target revisions and application receipts unchanged.
        review = ReviewService(space)
        view = review.get_question(question.question_ref)
        assert view.status == "answered"
        assert view.decisions, "决定本身必须保留"
        assert review.find_application(view.decisions[0].ref()) is None
        assert space.store().current_revision(target) == 1
    finally:
        space.close()


# ---- Completion requires both business and framework completion ----


def _force_pending_framework_nodes(space, execution_id, *, paper, as_node="roster"):
    """Forge a checkpoint with valid bindings/references but pending framework nodes."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from hvs_catalog.runtime import RuntimeContext
    from hvs_catalog.runtime.execution import get_execution

    conn = space.checkpoint_connection()
    try:
        ctx = RuntimeContext(
            space=space,
            execution=get_execution(space, execution_id),
            flow=direct_flow(),
            service=None,
            checkpointer=SqliteSaver(conn),
        )
        graph = direct_flow().build_graph(ctx)
        config = {"configurable": {"thread_id": execution_id}}
        graph.update_state(config, {}, as_node=as_node)
        pending = tuple(graph.get_state(config).next)
        assert pending, "场景构造失败：检查点应有待执行节点"
    finally:
        conn.close()


def test_pending_framework_nodes_reject_backup_and_finished_resume(tmp_path, service):
    """Reject backup and completion reports while framework nodes remain pending."""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        _force_pending_framework_nodes(space, execution.execution_id, paper="B")
        with pytest.raises(WorkspaceIntegrityError, match="待执行节点"):
            backup_workspace(space, tmp_path / "backup")
        with pytest.raises(RecoveryInconsistentError, match="待执行节点"):
            resume_execution(
                space, execution.execution_id, direct_flow(), service=service
            )
    finally:
        space.close()


def test_restore_rejects_semantically_inconsistent_backup(tmp_path, service):
    """Recovery semantic validation rejects the contradiction between completion and
    pending nodes.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        backup_workspace(space, tmp_path / "backup")
        # Build a hash-consistent but semantically contradictory backup by replacing the checkpoint and manifest.
        _force_pending_framework_nodes(space, execution.execution_id, paper="B")
        backup = tmp_path / "backup"
        shutil.copyfile(space.root / "checkpoint.db", backup / "checkpoint.db")
        manifest_path = backup / "backup-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(e for e in manifest["files"] if e["path"] == "checkpoint.db")
        data = (backup / "checkpoint.db").read_bytes()
        entry["size"] = len(data)
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(WorkspaceIntegrityError, match="待执行节点"):
            restore_workspace(backup, tmp_path / "restored")
        assert not (tmp_path / "restored").exists()
    finally:
        space.close()


def test_intact_space_still_backs_up_and_restores(tmp_path, service):
    """A valid workspace with archived responses and a paused checkpoint remains
    recoverable through backup/restore.
    """
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        _add_attempt_with_response(
            space,
            execution,
            "responses/integrity-fixture.json",
            b'{"evidence": "original"}',
        )
        digest = hashlib.sha256(b'{"evidence": "original"}').hexdigest()
        backup_workspace(space, tmp_path / "backup")
        restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
        try:
            assert (
                hashlib.sha256(
                    (
                        restored.root
                        / f"runs/{execution.execution_id}/responses/integrity-fixture.json"
                    ).read_bytes()
                ).hexdigest()
                == digest
            )
            report = resume_execution(
                restored,
                execution.execution_id,
                direct_flow(),
                service=service,
            )
            assert report.outcome == "finished"
            assert report.mode == "already-finished"
        finally:
            restored.close()
    finally:
        space.close()
