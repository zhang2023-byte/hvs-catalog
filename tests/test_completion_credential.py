"""框架完成凭据回归（R4-01）。

完成凭据除检查点 ID 外绑定内容摘要（按检查点库原始存储字节计算，
含影响恢复行为的待写入状态），与完成修订同事务提交；备份/恢复/完成
报告按同一规则重算比对——同 ID 下内容变化也必须拒绝。凭据缺失的补
记只在调用方流程实际核验后进行，已有凭据冲突时不自动覆盖。
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import textwrap

import pytest

from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import open_service, start_execution
from hvs_catalog.synthetic.flow import SyntheticFlow
from hvs_catalog.workspace import (
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    restore_workspace,
)
from hvs_catalog.workspace.backup import (
    framework_final_checkpoint,
    framework_final_digest,
)


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "ledger.db")
    yield svc
    svc.close()


def _finished(space, service, *, request_key="cred"):
    execution, flow, _ = start_execution(space, paper="B", request_key=request_key)
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "finished"
    return execution


def _tamper_same_id(space, execution_id, *, paper="B"):
    """把仍需执行 extract_contribution 的检查点写回原最终检查点 ID。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from hvs_catalog.runtime import RuntimeContext
    from hvs_catalog.runtime.execution import get_execution

    final = framework_final_checkpoint(space, execution_id)
    assert final is not None
    conn = space.checkpoint_connection()
    try:
        saver = SqliteSaver(conn)
        ctx = RuntimeContext(
            space=space,
            execution=get_execution(space, execution_id),
            flow=SyntheticFlow(paper=paper),
            service=None,
            checkpointer=saver,
        )
        graph = SyntheticFlow(paper=paper).build_graph(ctx)
        config = {"configurable": {"thread_id": execution_id}}
        graph.update_state(config, {}, as_node="extract_roster")
        tup = saver.get_tuple(config)
        new_id = tup.checkpoint["id"]
        changed = copy.deepcopy(tup.checkpoint)
        changed["id"] = final
        typ, blob = saver.serde.dumps_typed(changed)
        conn.execute(
            "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?",
            (execution_id, new_id),
        )
        conn.execute(
            "UPDATE checkpoints SET type = ?, checkpoint = ?"
            " WHERE thread_id = ? AND checkpoint_id = ?",
            (typ, blob, execution_id, final),
        )
        conn.commit()
        state = graph.get_state(config)
        assert state.next, "场景构造失败：框架应仍有待执行节点"
        assert state.config["configurable"]["checkpoint_id"] == final
    finally:
        conn.close()


def test_same_id_content_change_rejects_backup_and_keeps_credential(tmp_path, service):
    """R4-01：同 ID、内容重新出现待执行节点——备份拒绝且凭据不被覆盖。"""
    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        # 正常完成：凭据含 ID 与内容摘要，完好空间可备份恢复
        assert framework_final_digest(space, execution.execution_id) is not None
        _tamper_same_id(space, execution.execution_id)

        with pytest.raises(WorkspaceIntegrityError, match="内容与完成凭据不符"):
            backup_workspace(space, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()

        credential_before = (
            framework_final_checkpoint(space, execution.execution_id),
            framework_final_digest(space, execution.execution_id),
        )
        # 完成分支同样拒绝；失败的恢复不得覆盖既有凭据来通过检查
        with pytest.raises(RecoveryInconsistentError, match="待执行节点"):
            resume_execution(
                space,
                execution.execution_id,
                SyntheticFlow(paper="B"),
                service=service,
            )
        assert (
            framework_final_checkpoint(space, execution.execution_id),
            framework_final_digest(space, execution.execution_id),
        ) == credential_before
    finally:
        space.close()


def test_restore_rejects_same_id_checkpoint_change_in_backup(tmp_path, service):
    """R4-01：完好备份仅改检查点内容并重算清单哈希——恢复仍拒绝。"""
    import shutil

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        backup_workspace(space, tmp_path / "backup")
    finally:
        space.close()

    # 在备份上复刻同 ID 内容改写，并更新清单使文件层自洽
    backup = tmp_path / "backup"
    staged = tmp_path / "staged-tamper"
    shutil.copytree(backup, staged)
    opened = __import__(
        "hvs_catalog.workspace", fromlist=["open_workspace"]
    ).open_workspace(staged)
    _tamper_same_id(opened, execution.execution_id)
    opened.close()
    shutil.copyfile(staged / "checkpoint.db", backup / "checkpoint.db")
    shutil.rmtree(staged)
    manifest_path = backup / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(e for e in manifest["files"] if e["path"] == "checkpoint.db")
    data = (backup / "checkpoint.db").read_bytes()
    entry["size"] = len(data)
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with pytest.raises(WorkspaceIntegrityError, match="内容与完成凭据不符"):
        restore_workspace(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


# ---- 完成事务的真实中断窗口（第四轮验收确认，转为正式回归） ----

_ATOMIC_DRIVER = """
import os
import sys
from pathlib import Path

from hvs_catalog.runtime import resume_execution
from hvs_catalog.synthetic import open_service
from hvs_catalog.synthetic.flow import SyntheticFlow
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
resume_execution(space, sys.argv[3], SyntheticFlow(paper="B"), service=service)
"""


@pytest.mark.parametrize(
    ("window", "exit_code"), [("before_commit", 74), ("after_commit", 75)]
)
def test_finish_transaction_interruption_windows(tmp_path, service, window, exit_code):
    """完成事务提交前/后真实退出：修订与凭据一致，恢复零新增调用。"""
    space = initialize_workspace(tmp_path / "space")
    execution, _flow, _ = start_execution(
        space, paper="B", request_key=f"atomic-{window}"
    )
    space.close()
    driver = tmp_path / "atomic_driver.py"
    driver.write_text(textwrap.dedent(_ATOMIC_DRIVER), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(tmp_path / "space"),
            str(tmp_path / "ledger.db"),
            execution.execution_id,
            window,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr

    reopened = __import__(
        "hvs_catalog.workspace", fromlist=["open_workspace"]
    ).open_workspace(tmp_path / "space")
    try:
        run = reopened.store().read_record_current(execution.run_ref.record_id)
        proof_id = framework_final_checkpoint(reopened, execution.execution_id)
        proof_digest = framework_final_digest(reopened, execution.execution_id)
        if window == "before_commit":
            # 事务未提交：完成修订与凭据都不存在，恢复后重新完成
            assert run.data.state != "finished"
            assert proof_id is None and proof_digest is None
        else:
            # 事务已提交：完成修订与凭据同时可见
            assert run.data.state == "finished"
            assert proof_id is not None and proof_digest is not None
        calls = service.summary()["calls"]
        report = resume_execution(
            reopened, execution.execution_id, SyntheticFlow(paper="B"), service=service
        )
        assert report.outcome == "finished"
        assert service.summary()["calls"] == calls  # 结果复用，零新增调用
        assert framework_final_checkpoint(reopened, execution.execution_id) is not None
        assert framework_final_digest(reopened, execution.execution_id) is not None
    finally:
        reopened.close()


# ---- 部分凭据缺失时的补记核对（第五轮验收 R5-01） ----


def _write_meta_raw(space, key, *, delete=False, value=None):
    """直接改写业务库 meta 表，构造部分凭据缺失或冲突的初始状态。"""
    with space.store().transaction() as tx:
        if delete:
            tx.conn.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            tx.conn.execute("UPDATE meta SET value=? WHERE key=?", (value, key))


def _tamper_final_metadata(space, execution_id):
    """同 ID 修改最终检查点的合法 metadata，业务结果与框架结束状态不变。

    这样的改写不会让框架重新出现待执行节点，只能靠已保存的摘要发现。
    """
    final = framework_final_checkpoint(space, execution_id)
    assert final is not None
    conn = space.checkpoint_connection()
    try:
        row = conn.execute(
            "SELECT metadata FROM checkpoints WHERE thread_id=? AND checkpoint_id=?",
            (execution_id, final),
        ).fetchone()
        meta = json.loads(row[0])
        meta["probe_note"] = "changed after completion"
        conn.execute(
            "UPDATE checkpoints SET metadata=? WHERE thread_id=? AND checkpoint_id=?",
            (json.dumps(meta).encode(), execution_id, final),
        )
        conn.commit()
    finally:
        conn.close()


def test_missing_id_with_conflicting_digest_rejected(tmp_path, service):
    """R5-01：只缺 ID、已有摘要与当前内容冲突——拒绝补记且保留原摘要。"""
    from hvs_catalog.workspace.backup import (
        framework_final_key,
    )

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        id_key = framework_final_key(execution.execution_id)
        original_digest = framework_final_digest(space, execution.execution_id)
        _tamper_final_metadata(space, execution.execution_id)
        _write_meta_raw(space, id_key, delete=True)
        assert space.store().get_meta(id_key) is None

        # 补记前先核对已有凭据：摘要冲突必须拒绝，不能靠覆盖摘要通过
        with pytest.raises(RecoveryInconsistentError, match="已有凭据冲突"):
            resume_execution(
                space,
                execution.execution_id,
                SyntheticFlow(paper="B"),
                service=service,
            )
        # 冲突证据保持原样：ID 仍缺失、摘要仍是改写前的值，备份仍拒绝
        assert space.store().get_meta(id_key) is None
        assert framework_final_digest(space, execution.execution_id) == original_digest
        with pytest.raises(WorkspaceIntegrityError, match="缺少框架完成凭据"):
            backup_workspace(space, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        space.close()


def test_missing_digest_with_conflicting_id_rejected(tmp_path, service):
    """R5-01：只缺摘要、已有 ID 与当前检查点冲突——拒绝且摘要仍缺失。"""
    from hvs_catalog.workspace.backup import (
        framework_final_digest_key,
        framework_final_key,
    )

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        id_key = framework_final_key(execution.execution_id)
        digest_key = framework_final_digest_key(execution.execution_id)
        _write_meta_raw(space, digest_key, delete=True)
        _write_meta_raw(space, id_key, value="unrelated-checkpoint")

        with pytest.raises(RecoveryInconsistentError, match="已有凭据冲突"):
            resume_execution(
                space,
                execution.execution_id,
                SyntheticFlow(paper="B"),
                service=service,
            )
        # 拒绝发生在任何写入之前：摘要仍缺失，冲突的 ID 原样保留
        assert space.store().get_meta(digest_key) is None
        assert framework_final_checkpoint(space, execution.execution_id) == (
            "unrelated-checkpoint"
        )
        with pytest.raises(WorkspaceIntegrityError, match="已偏离|完成凭据"):
            backup_workspace(space, tmp_path / "backup")
        assert not (tmp_path / "backup").exists()
    finally:
        space.close()


def test_missing_id_with_matching_digest_backfills_id_only(tmp_path, service):
    """凭据只缺 ID、已有摘要与当前内容一致——仅补 ID，摘要不变。"""
    from hvs_catalog.workspace.backup import framework_final_key

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        original_digest = framework_final_digest(space, execution.execution_id)
        _write_meta_raw(space, framework_final_key(execution.execution_id), delete=True)
        report = resume_execution(
            space,
            execution.execution_id,
            SyntheticFlow(paper="B"),
            service=service,
        )
        assert report.outcome == "finished"
        assert report.mode == "already-finished"
        assert framework_final_checkpoint(space, execution.execution_id) is not None
        assert framework_final_digest(space, execution.execution_id) == original_digest
    finally:
        space.close()


def test_missing_digest_with_matching_id_backfills_digest_only(tmp_path, service):
    """凭据只缺摘要、已有 ID 与当前检查点一致——仅补摘要，ID 不变。"""
    from hvs_catalog.workspace.backup import framework_final_digest_key

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        original_id = framework_final_checkpoint(space, execution.execution_id)
        original_digest = framework_final_digest(space, execution.execution_id)
        _write_meta_raw(
            space, framework_final_digest_key(execution.execution_id), delete=True
        )
        report = resume_execution(
            space,
            execution.execution_id,
            SyntheticFlow(paper="B"),
            service=service,
        )
        assert report.outcome == "finished"
        assert report.mode == "already-finished"
        assert framework_final_checkpoint(space, execution.execution_id) == original_id
        assert framework_final_digest(space, execution.execution_id) == original_digest
    finally:
        space.close()


def test_both_credentials_missing_backfilled_after_verification(tmp_path, service):
    """两键全缺：不能直接视为已完成，须流程核验通过后成组补记。"""
    from hvs_catalog.workspace.backup import (
        framework_final_digest_key,
        framework_final_key,
    )

    space = initialize_workspace(tmp_path / "space")
    try:
        execution = _finished(space, service)
        original_id = framework_final_checkpoint(space, execution.execution_id)
        original_digest = framework_final_digest(space, execution.execution_id)
        _write_meta_raw(space, framework_final_key(execution.execution_id), delete=True)
        _write_meta_raw(
            space, framework_final_digest_key(execution.execution_id), delete=True
        )
        # 凭据全缺时备份先拒绝：缺失执行不能直接按已完成接受
        with pytest.raises(WorkspaceIntegrityError, match="缺少框架完成凭据"):
            backup_workspace(space, tmp_path / "backup-before")
        assert not (tmp_path / "backup-before").exists()

        report = resume_execution(
            space,
            execution.execution_id,
            SyntheticFlow(paper="B"),
            service=service,
        )
        assert report.outcome == "finished"
        assert report.mode == "already-finished"
        assert framework_final_checkpoint(space, execution.execution_id) == original_id
        assert framework_final_digest(space, execution.execution_id) == original_digest
        backup_workspace(space, tmp_path / "backup-after")
    finally:
        space.close()
