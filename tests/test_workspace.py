"""工作空间初始化、打开、配置与材料发布（A01、A02）。

全部材料为合成内容。中断初始化用手工构造的暂存清单模拟真实中断
现场；真实进程终止的恢复见子进程测试。
"""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from hvs_catalog.workspace import (
    UnknownWorkspaceError,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
    initialize_workspace,
    open_workspace,
)


def test_initialize_creates_marker_databases_and_directories(tmp_path):
    space = initialize_workspace(tmp_path / "space", config={"locale": "en"})
    root = space.root
    assert (root / "workspace.json").is_file()
    assert (root / "config.json").is_file()
    assert (root / "business.db").is_file()
    assert (root / "checkpoint.db").is_file()
    assert (root / "sources").is_dir()
    assert (root / "runs").is_dir()
    assert (root / "backups").is_dir()
    marker = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    assert marker["format"] == "hvs-catalog-workspace"
    assert marker["space_id"] == space.identity.space_id
    assert space.read_config() == {"locale": "en"}
    space.close()


def test_repeated_initialize_reads_existing_config_without_changes(tmp_path):
    root = tmp_path / "space"
    first = initialize_workspace(root, config={"keep": 1})
    space_id = first.identity.space_id
    first.close()
    second = initialize_workspace(root, config={"keep": 2})  # 配置参数被忽略
    assert second.identity.space_id == space_id
    assert second.read_config() == {"keep": 1}
    second.close()


def test_existing_unmanaged_files_preserved_and_reserved_conflicts_reject_all(
    tmp_path,
):
    # 非预留名的用户资料可以保留，初始化成功
    root = tmp_path / "ok-space"
    root.mkdir()
    (root / "user-notes.txt").write_text("用户自有资料", encoding="utf-8")
    space = initialize_workspace(root)
    assert (root / "user-notes.txt").read_text(encoding="utf-8") == "用户自有资料"
    space.close()

    # 预留文件名冲突：整体拒绝，已有资料逐字节保留，不留任何空间文件
    conflict = tmp_path / "conflict-space"
    conflict.mkdir()
    (conflict / "sources").mkdir()
    (conflict / "sources" / "own.txt").write_text("用户来源资料", encoding="utf-8")
    with pytest.raises(WorkspaceConflictError, match="冲突"):
        initialize_workspace(conflict)
    assert (conflict / "sources" / "own.txt").read_text(
        encoding="utf-8"
    ) == "用户来源资料"
    assert not (conflict / "workspace.json").exists()
    assert not (conflict / "business.db").exists()


def test_two_spaces_are_independent(tmp_path):
    a = initialize_workspace(tmp_path / "a")
    b = initialize_workspace(tmp_path / "b", config={"x": 1})
    assert a.identity.space_id != b.identity.space_id
    ref_a = a.publish_source_file("paper.tex", b"content A")
    with pytest.raises(WorkspaceIntegrityError):
        b.read_file(ref_a)
    a.close()
    b.close()


def test_open_refuses_demo_space_and_unknown_marker(tmp_path):
    demo = tmp_path / "demo-space"
    demo.mkdir()
    (demo / "business.db").write_bytes(b"not a workspace db")
    with pytest.raises(UnknownWorkspaceError, match="演示空间"):
        open_workspace(demo)

    bad_format = tmp_path / "bad-format"
    initialize_workspace(bad_format).close()
    marker_path = bad_format / "workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["format"] = "hvs-demo"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(UnknownWorkspaceError, match="未知工作空间格式"):
        open_workspace(bad_format)

    bad_version = tmp_path / "bad-version"
    initialize_workspace(bad_version).close()
    marker_path = bad_version / "workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["format_version"] = 99
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(UnknownWorkspaceError, match="不自动迁移"):
        open_workspace(bad_version)


def test_open_refuses_marker_database_identity_mismatch(tmp_path):
    root = tmp_path / "space"
    initialize_workspace(root).close()
    other = tmp_path / "other"
    initialize_workspace(other).close()
    shutil.move(other / "business.db", root / "business.db")
    with pytest.raises(WorkspaceIntegrityError, match="身份"):
        open_workspace(root)


def test_interrupted_init_recovers_only_own_files(tmp_path):
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    staging.mkdir()
    # 模拟中断现场：清单记录了已发布的目录与文件（含哈希），未列出的
    # 用户资料不动
    (staging / "journal.json").write_text(
        json.dumps(
            {
                "state": "active",
                "space_id": "probe-space",
                "published": [
                    {"path": "sources", "kind": "directory"},
                    {
                        "path": "config.json",
                        "kind": "file",
                        "sha256": _sha(root, None, b"{}"),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "config.json").write_bytes(b"{}")
    (root / "sources").mkdir()
    (staging / "business.db").write_bytes(b"partial")
    (root / "user-notes.txt").write_text("用户自有资料", encoding="utf-8")
    space = initialize_workspace(root)
    # 清单内的文件被回滚后重新创建，未列出的用户资料逐字节保留
    assert (root / "workspace.json").is_file()
    assert (root / "user-notes.txt").read_text(encoding="utf-8") == "用户自有资料"
    assert not (root / ".workspace-init").exists()
    space.close()


def test_interrupted_init_refuses_unknown_same_name_files(tmp_path):
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    staging.mkdir()
    (staging / "journal.json").write_text(
        json.dumps({"state": "active", "space_id": "probe-space", "published": []}),
        encoding="utf-8",
    )
    # 身份不明的同名资料：清单没有记录，初始化拒绝接管
    (root / "business.db").write_bytes(b"someone else's data")
    with pytest.raises(WorkspaceConflictError, match="冲突"):
        initialize_workspace(root)
    assert (root / "business.db").read_bytes() == b"someone else's data"


def test_partial_init_without_marker_is_not_complete_workspace(tmp_path):
    root = tmp_path / "space"
    root.mkdir()
    (root / "business.db").write_bytes(b"partial")
    # 部分初始化（无完成标记）不被当作完成：打开被拒绝，初始化因冲突被拒绝
    with pytest.raises(UnknownWorkspaceError):
        open_workspace(root)
    with pytest.raises(WorkspaceConflictError):
        initialize_workspace(root)


def test_update_config_rejects_credential_keys(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    with pytest.raises(WorkspaceConflictError, match="凭据"):
        space.update_config({"api_key": "abc"})
    with pytest.raises(WorkspaceConflictError, match="凭据"):
        initialize_workspace(tmp_path / "other", config={"auth_token": "x"})
    merged = space.update_config({"download": {"concurrency": 2}})
    assert merged["download"] == {"concurrency": 2}
    space.close()


def test_publish_file_reuse_and_refuse_overwrite(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    ref = space.publish_source_file("paper-a/paper.tex", b"line1\n")
    same = space.publish_source_file("paper-a/paper.tex", b"line1\n")
    assert same == ref
    with pytest.raises(WorkspaceConflictError, match="拒绝覆盖"):
        space.publish_source_file("paper-a/paper.tex", b"changed\n")
    with pytest.raises(WorkspaceConflictError, match="sources/"):
        space.publish_file("outside/paper.tex", b"x")
    with pytest.raises(WorkspaceIntegrityError, match="越界"):
        space.publish_file("sources/../../escape.txt", b"x")
    space.close()


def test_publish_run_file_isolated_per_execution(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    ref = space.publish_run_file("exec-1", "rules.toml", b"rules")
    assert ref.path == "runs/exec-1/rules.toml"
    with pytest.raises(WorkspaceConflictError, match="拒绝覆盖"):
        space.publish_run_file("exec-1", "rules.toml", b"other rules")
    other = space.publish_run_file("exec-2", "rules.toml", b"other rules")
    assert other.sha256 != ref.sha256
    space.close()


def test_checkpoint_connection_is_usable(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    space = initialize_workspace(tmp_path / "space")
    conn = space.checkpoint_connection()
    saver = SqliteSaver(conn)
    assert saver is not None
    conn.close()
    space.close()


def _sha(root, _unused, data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_sqlite_marker_tables_exist(tmp_path):
    space = initialize_workspace(tmp_path / "space")
    conn = sqlite3.connect(space.root / "business.db")
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    space.close()
    assert {"records", "current_revisions", "receipts", "executions"} <= tables
