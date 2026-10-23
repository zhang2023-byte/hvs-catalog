"""Backups reject incomplete or altered data and preserve source boundaries."""

import json
import shutil

import pytest
from test_roster import run, submission

from hvs_catalog.workspace import (
    WorkspaceConflictError,
    WorkspaceIntegrityError,
    backup_workspace,
    read_backup_manifest,
    restore_workspace,
)


def material_path(space):
    return (
        next(
            r for r in space.store().list_records() if r.RECORD_NAME == "source_package"
        )
        .data.files[0]
        .path
    )


def test_backup_refuses_inconsistent_space(prepared, tmp_path):
    """A20: inconsistent workspaces cannot produce backups."""
    space = prepared[0]
    run(prepared, [submission()])
    (space.root / material_path(space)).write_bytes(b"tampered")
    with pytest.raises(WorkspaceIntegrityError, match="哈希"):
        backup_workspace(space, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_incomplete_backup_unusable_and_source_untouched(prepared, tmp_path):
    """A20: backups without complete markers are unusable; invalid backups publish no valid
    workspace.
    """
    space = prepared[0]
    run(prepared, [submission()])
    backup_workspace(space, tmp_path / "backup")
    marker = tmp_path / "backup" / "backup-manifest.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    # Simulate interruption by reverting the manifest to incomplete.
    manifest["state"] = "incomplete"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(WorkspaceIntegrityError, match="完整标记"):
        read_backup_manifest(tmp_path / "backup")
    with pytest.raises(WorkspaceIntegrityError, match="完整标记"):
        restore_workspace(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_restore_detects_missing_file_wrong_hash_corrupt_db(prepared, tmp_path):
    """A20: reject backups with missing files, incorrect hashes, or corrupted databases."""
    space = prepared[0]
    run(prepared, [submission()])
    backup_workspace(space, tmp_path / "backup")

    missing = tmp_path / "backup-missing"
    shutil.copytree(tmp_path / "backup", missing)
    (missing / "business.db").unlink()
    with pytest.raises(WorkspaceIntegrityError, match="缺失"):
        restore_workspace(missing, tmp_path / "r1")
    assert not (tmp_path / "r1").exists()

    wrong_hash = tmp_path / "backup-hash"
    shutil.copytree(tmp_path / "backup", wrong_hash)
    material = wrong_hash / material_path(space)
    material.write_bytes(material.read_bytes() + b"x")
    with pytest.raises(WorkspaceIntegrityError, match="哈希不符"):
        restore_workspace(wrong_hash, tmp_path / "r2")

    corrupt = tmp_path / "backup-corrupt"
    shutil.copytree(tmp_path / "backup", corrupt)
    (corrupt / "checkpoint.db").write_bytes(b"not a database")
    with pytest.raises(WorkspaceIntegrityError):
        restore_workspace(corrupt, tmp_path / "r3")


def test_restore_refuses_existing_target_and_escapes(prepared, tmp_path):
    """A21: reject existing restore targets, out-of-bounds paths, and escaping symlinks."""
    space = prepared[0]
    run(prepared, [submission()])
    backup_workspace(space, tmp_path / "backup")

    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("用户资料", encoding="utf-8")
    with pytest.raises(WorkspaceConflictError, match="拒绝覆盖"):
        restore_workspace(tmp_path / "backup", existing)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "用户资料"

    # Manifest path escapes its root.
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

    # Replace a backup file with a symlink outside the workspace.
    linked = tmp_path / "backup-linked"
    shutil.copytree(tmp_path / "backup", linked)
    victim = tmp_path / "victim.txt"
    victim.write_text("空间外资料", encoding="utf-8")
    target_file = linked / material_path(space)
    target_file.unlink()
    target_file.symlink_to(victim)
    with pytest.raises(WorkspaceIntegrityError, match="符号链接"):
        restore_workspace(linked, tmp_path / "r-linked")
    assert victim.read_text(encoding="utf-8") == "空间外资料"


def test_restore_rejects_symlinked_backup_source_directory(prepared, tmp_path):
    """Intermediate-directory symlinks cannot make restore read material outside the backup
    root.
    """
    space = prepared[0]
    run(prepared, [submission()])
    backup_workspace(space, tmp_path / "backup")

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
    # External file bytes remain unchanged and are copied into no restored output.
    for rel, payload in outside_files.items():
        assert (outside / rel).read_bytes() == payload
    assert not (tmp_path / "restored").exists()


def test_backup_excludes_lock_temp_and_other_backups(prepared, tmp_path):
    space = prepared[0]
    run(prepared, [submission()])
    backup1 = backup_workspace(space, tmp_path / "backup-1")
    backup_workspace(space, space.root / "backups/inner")
    assert backup1.backup_dir.exists()
    assert (space.root / "backups/inner/backup-manifest.json").is_file()
    manifest = read_backup_manifest(space.root / "backups/inner")
    paths = [entry["path"] for entry in manifest["files"]]
    assert not any(".lock" in p for p in paths)
    assert not any("backup" in p for p in paths)
    assert not any(p.endswith(".db-wal") or "tmp" in p for p in paths)
