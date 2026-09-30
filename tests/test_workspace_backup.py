"""备份与恢复验证：四种状态、坏备份拒绝、恢复目标保护（A19/A20/A21）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from shared_scenarios import (
    direct_flow,
    open_service,
    pending_questions,
    start_execution,
    submit_expert_decision,
)
from shared_scenarios import (
    material as m,
)

from hvs_catalog.runtime import resume_execution
from hvs_catalog.workspace import (
    WorkspaceConflictError,
    WorkspaceIntegrityError,
    backup_workspace,
    initialize_workspace,
    read_backup_manifest,
    restore_workspace,
)


@pytest.fixture()
def service(tmp_path):
    svc = open_service(tmp_path / "calls.jsonl")
    yield svc
    svc.close()


def _run_to_pause(space, service, *, paper="A", request_key="a"):
    execution, flow, _ = start_execution(space, paper=paper, request_key=request_key)
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "paused"
    question = pending_questions(space, execution_id=execution.execution_id)[0]
    return execution, flow, question


def _run_finished(space, service, *, paper="B", request_key="b"):
    """无歧义论文一次跑完，为备份测试提供含执行与材料的空间。"""
    execution, flow, _ = start_execution(space, paper=paper, request_key=request_key)
    report = resume_execution(space, execution.execution_id, flow, service=service)
    assert report.outcome == "finished"
    return execution, flow


_CONTINUE_DRIVER = """
import json
import sys
from pathlib import Path

from hvs_catalog.runtime import resume_execution
from shared_scenarios import open_service, pending_questions, submit_expert_decision
from shared_scenarios import direct_flow
from hvs_catalog.workspace import open_workspace

root, execution_id, ledger = sys.argv[1], sys.argv[2], sys.argv[3]
space = open_workspace(Path(root))
service = open_service(Path(ledger))
out = {}
report = resume_execution(space, execution_id, direct_flow(), service=service)
out["first"] = report.outcome
question = pending_questions(space, execution_id=execution_id)[0]
submit_expert_decision(
    space,
    execution_id=execution_id,
    question_ref=question.question_ref,
    request_key="dec-subprocess",
)
report = resume_execution(space, execution_id, direct_flow(), service=service)
out["second"] = report.outcome
from shared_scenarios import material as m

out["paper_revision"] = space.store().current_revision(
    m.paper_record_id("A", execution_id)
)
out["service_calls"] = service.summary()["calls"]
service.close()
space.close()
print(json.dumps(out))
"""


def _continue_in_subprocess(tmp_path, space_root, execution_id, ledger):
    """在新进程中打开恢复出的空间并继续执行（A19 的新进程要求）。"""
    script = tmp_path / "continue_driver.py"
    script.write_text(_CONTINUE_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(space_root), execution_id, str(ledger)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_backup_waiting_answer_and_restore_continues(tmp_path, service):
    """A19 状态一：等待答复时备份，恢复后在新目录新进程继续。"""
    space = initialize_workspace(tmp_path / "space")
    execution, _flow, _question = _run_to_pause(space, service)
    backup_workspace(space, tmp_path / "backup-waiting")
    assert (tmp_path / "backup-waiting" / "backup-manifest.json").is_file()
    calls_before = service.summary()["calls"]

    restored = restore_workspace(tmp_path / "backup-waiting", tmp_path / "restored")
    try:
        assert restored.identity.space_id == space.identity.space_id
        # 新空间继续：材料、业务记录与待办一致
        assert (
            restored.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 1
        )
        questions = pending_questions(restored, execution_id=execution.execution_id)
        assert questions[0].status == "open"
    finally:
        restored.close()
    # 在新进程中继续执行；前两步结果被复用，日志不产生任何新调用
    out = _continue_in_subprocess(
        tmp_path,
        tmp_path / "restored",
        execution.execution_id,
        tmp_path / "calls.jsonl",
    )
    assert out["first"] == "paused"  # 仍等待答复
    assert out["second"] == "finished"
    assert out["paper_revision"] == 2
    assert out["service_calls"] == calls_before
    assert service.summary()["calls"] == calls_before
    space.close()


def test_backup_pending_apply_state(tmp_path, service):
    """A19 状态二：决定已保存、结果待应用。"""
    space = initialize_workspace(tmp_path / "space")
    execution, _flow, question = _run_to_pause(space, service)
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    backup_workspace(space, tmp_path / "backup-pending")
    restored = restore_workspace(tmp_path / "backup-pending", tmp_path / "restored")
    try:
        view = pending_questions(restored, execution_id=execution.execution_id)[0]
        assert view.status == "answered"
        assert view.application is None  # 回执显示待应用
        out = _continue_in_subprocess(
            tmp_path, restored.root, execution.execution_id, tmp_path / "calls.jsonl"
        )
        assert out["first"] == out["second"] == "finished"
        assert out["paper_revision"] == 2
        report = resume_execution(
            restored, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "finished"
        assert (
            restored.store().current_revision(
                m.paper_record_id("A", execution.execution_id)
            )
            == 2
        )
    finally:
        restored.close()
        space.close()


def test_backup_applied_awaiting_resume_state(tmp_path, service):
    """A19 状态三：结果已应用、框架待恢复。"""
    space = initialize_workspace(tmp_path / "space")
    execution, _flow, question = _run_to_pause(space, service)
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )

    def explode():
        raise RuntimeError("exit after apply")

    resume_execution(
        space,
        execution.execution_id,
        direct_flow(fault_hooks={"after_apply": explode}),
        service=service,
    )
    assert (
        space.store().current_revision(m.paper_record_id("A", execution.execution_id))
        == 2
    )
    backup_workspace(space, tmp_path / "backup-applied")
    restored = restore_workspace(tmp_path / "backup-applied", tmp_path / "restored")
    try:
        out = _continue_in_subprocess(
            tmp_path, restored.root, execution.execution_id, tmp_path / "calls.jsonl"
        )
        assert out["first"] == out["second"] == "finished"
        assert out["paper_revision"] == 2
        report = resume_execution(
            restored, execution.execution_id, direct_flow(), service=service
        )
        assert report.outcome == "finished"
        revisions = [
            r.revision
            for r in restored.store().list_records()
            if r.record_id == m.paper_record_id("A", execution.execution_id)
        ]
        assert revisions == [1, 2]  # 已应用结果不再生成第二份修订
    finally:
        restored.close()
        space.close()


def test_backup_finished_state_and_receipts_complete(tmp_path, service):
    """A19 状态四：完成后备份；回执完整、恢复后可核对。"""
    space = initialize_workspace(tmp_path / "space")
    execution, flow, question = _run_to_pause(space, service)
    submit_expert_decision(
        space,
        execution_id=execution.execution_id,
        question_ref=question.question_ref,
        request_key="dec",
    )
    resume_execution(space, execution.execution_id, flow, service=service)
    backup_workspace(space, tmp_path / "backup-finished")
    manifest = read_backup_manifest(tmp_path / "backup-finished")
    roles = {entry["role"] for entry in manifest["files"]}
    assert {"database", "material", "space_config"} <= roles
    # 操作回执在业务库内随快照保存
    restored = restore_workspace(tmp_path / "backup-finished", tmp_path / "restored")
    try:
        receipts = restored.store().find_receipts()
        kinds = {receipt.kind for receipt in receipts}
        assert "decision_save" in kinds
        assert "decision_apply" in kinds
        run = restored.store().read_record_current(f"run-{execution.execution_id}")
        assert run.data.state == "finished"
        out = _continue_in_subprocess(
            tmp_path, restored.root, execution.execution_id, tmp_path / "calls.jsonl"
        )
        assert out["first"] == out["second"] == "finished"
        assert out["paper_revision"] == 2
    finally:
        restored.close()
        space.close()


def test_backup_refuses_inconsistent_space(tmp_path, service):
    """A20：空间不一致时不生成备份。"""
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    (space.root / "sources/r05/b/paper.tex").write_bytes(b"tampered")
    with pytest.raises(WorkspaceIntegrityError, match="一致性核对未通过"):
        backup_workspace(space, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    space.close()


def test_incomplete_backup_unusable_and_source_untouched(tmp_path, service):
    """A20：无完整标记的备份不可用；坏备份不发布有效空间。"""
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    backup_workspace(space, tmp_path / "backup")
    space.close()
    marker = tmp_path / "backup" / "backup-manifest.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    # 模拟中断：清单回退为未完成
    manifest["state"] = "incomplete"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceIntegrityError, match="完整标记"):
        read_backup_manifest(tmp_path / "backup")
    with pytest.raises(WorkspaceIntegrityError, match="完整标记"):
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_restore_detects_missing_file_wrong_hash_corrupt_db(tmp_path, service):
    """A20：缺文件、错误哈希与损坏数据库的备份都不可用。"""
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    backup_workspace(space, tmp_path / "backup")
    space.close()

    missing = tmp_path / "backup-missing"
    shutil.copytree(tmp_path / "backup", missing)
    (missing / "business.db").unlink()
    with pytest.raises(WorkspaceIntegrityError, match="缺失"):
        restore_workspace(missing, tmp_path / "r1")
    assert not (tmp_path / "r1").exists()

    wrong_hash = tmp_path / "backup-hash"
    shutil.copytree(tmp_path / "backup", wrong_hash)
    material = wrong_hash / "sources/r05/b/paper.tex"
    material.write_bytes(material.read_bytes() + b"x")
    with pytest.raises(WorkspaceIntegrityError, match="哈希不符"):
        restore_workspace(wrong_hash, tmp_path / "r2")

    corrupt = tmp_path / "backup-corrupt"
    shutil.copytree(tmp_path / "backup", corrupt)
    (corrupt / "checkpoint.db").write_bytes(b"not a database")
    with pytest.raises(WorkspaceIntegrityError):
        restore_workspace(corrupt, tmp_path / "r3")


def test_restore_refuses_existing_target_and_escapes(tmp_path, service):
    """A21：恢复目标已存在、路径越界与符号链接逃逸明确拒绝。"""
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    backup_workspace(space, tmp_path / "backup")
    space.close()

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("用户资料", encoding="utf-8")
    with pytest.raises(WorkspaceConflictError, match="拒绝覆盖"):
        restore_workspace(tmp_path / "backup", existing)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "用户资料"

    # 清单路径越界
    evil = tmp_path / "backup-evil"
    shutil.copytree(tmp_path / "backup", evil)
    marker = evil / "backup-manifest.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    manifest["files"].append(
        {"path": "../escape.txt", "size": 1, "sha256": "0" * 64, "role": "material"}
    )
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceIntegrityError, match="越界"):
        restore_workspace(evil, tmp_path / "r-evil")
    assert not (tmp_path / "escape.txt").exists()

    # 符号链接逃逸：备份里的文件换成指向空间外的链接
    linked = tmp_path / "backup-linked"
    shutil.copytree(tmp_path / "backup", linked)
    victim = tmp_path / "victim.txt"
    victim.write_text("空间外资料", encoding="utf-8")
    target_file = linked / "sources/r05/b/paper.tex"
    target_file.unlink()
    target_file.symlink_to(victim)
    with pytest.raises(WorkspaceIntegrityError, match="符号链接"):
        restore_workspace(linked, tmp_path / "r-linked")
    assert victim.read_text(encoding="utf-8") == "空间外资料"


def test_restore_rejects_symlinked_backup_source_directory(tmp_path, service):
    """中间目录符号链接不能让恢复读取备份根外的材料。"""
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    backup_workspace(space, tmp_path / "backup")
    space.close()

    backup = tmp_path / "backup"
    outside = tmp_path / "outside-sources"
    shutil.move(str(backup / "sources"), str(outside))
    (backup / "sources").symlink_to(outside, target_is_directory=True)
    outside_files = {
        p.relative_to(outside).as_posix(): p.read_bytes()
        for p in outside.rglob("*")
        if p.is_file()
    }
    assert outside_files, "场景构造失败：备份应包含来源材料"
    with pytest.raises(WorkspaceIntegrityError, match="符号链接"):
        restore_workspace(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    # 备份根外的文件字节不变，没有把外部内容复制进任何恢复产物
    for rel, payload in outside_files.items():
        assert (outside / rel).read_bytes() == payload
    assert not (tmp_path / "restored").exists()


def test_backup_excludes_lock_temp_and_other_backups(tmp_path, service):
    space = initialize_workspace(tmp_path / "space")
    _run_finished(space, service)
    backup1 = backup_workspace(space, tmp_path / "backup-1")
    backup_workspace(space, space.root / "backups/inner")
    assert backup1.backup_dir.exists()
    assert (space.root / "backups/inner/backup-manifest.json").is_file()
    manifest = read_backup_manifest(space.root / "backups/inner")
    paths = [entry["path"] for entry in manifest["files"]]
    assert not any(".lock" in p for p in paths)
    assert not any("backup" in p for p in paths)
    assert not any(p.endswith(".db-wal") or "tmp" in p for p in paths)
    space.close()
