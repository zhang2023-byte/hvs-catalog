"""初始化归属与中断恢复：未知目录整体拒绝、真实发布窗口可回滚（A01/A02 回归）。

F01/F02 的正式回归：归属无法证明的 ``.workspace-init`` 整体拒绝且逐
字节保留；初始化采用预写清单（WAL），真实子进程在每个发布窗口终止
后，再次初始化按清单回滚自身产物并重新开始，或按完成标记接续。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap

import pytest

from hvs_catalog.workspace import (
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceIntegrityError,
    initialize_workspace,
    open_workspace,
)


def _run_child(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_unowned_staging_directory_rejected_and_preserved(tmp_path):
    """F01：无法证明归属的 .workspace-init 整体拒绝且逐字节保留。"""
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    (staging / "nested" / "deep").mkdir(parents=True)
    note = staging / "nested" / "user-note.txt"
    note.write_text("用户自有资料，不能删除", encoding="utf-8")
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    digests = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(WorkspaceConflictError, match="无法证明|缺少初始化清单"):
        initialize_workspace(root)
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    assert after == before
    for rel, digest in digests.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == digest
    assert note.read_text(encoding="utf-8") == "用户自有资料，不能删除"
    # 拒绝后没有创建任何空间文件
    assert not (root / "workspace.json").exists()
    assert not (root / ".lock").exists()


def test_corrupt_journal_rejected_and_preserved(tmp_path):
    """F01：清单损坏同样无法证明归属，整体拒绝。"""
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    staging.mkdir()
    (staging / "journal.json").write_bytes(b"{not-json")
    with pytest.raises(WorkspaceIntegrityError, match="无法读取|不完整"):
        initialize_workspace(root)
    assert (staging / "journal.json").read_bytes() == b"{not-json"


def test_concurrent_initialize_reports_busy(tmp_path):
    """并发初始化：另一进程持有初始化锁与清单时，本进程得到明确的忙。"""

    root = tmp_path / "space"
    root.mkdir()
    release = tmp_path / "release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path

                from hvs_catalog.workspace import layout

                root, release = Path(sys.argv[1]), Path(sys.argv[2])
                staging = root / layout.INIT_STAGING_DIRNAME
                staging.mkdir()
                layout._write_init_journal(
                    staging,
                    {"state": "active", "space_id": "probe", "published": []},
                )
                lock = layout._staging_lock(staging)
                lock.acquire()
                print("locked", flush=True)
                for _ in range(600):
                    if release.exists():
                        break
                    time.sleep(0.1)
                lock.release()
                """
            ),
            str(root),
            str(release),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        for _ in range(300):
            line = child.stdout.readline()
            if line.strip() == "locked":
                break
        else:
            raise AssertionError("子进程未取得初始化锁")
        with pytest.raises(WorkspaceBusyError, match="另一执行者"):
            initialize_workspace(root)
        # 目录内容保持原样，未被并发初始化改动
        assert (root / ".workspace-init" / "journal.json").is_file()
    finally:
        release.write_text("go", encoding="utf-8")
        child.wait(timeout=30)
    # 对方释放锁后可正常初始化
    space = initialize_workspace(root)
    assert (root / "workspace.json").is_file()
    space.close()


@pytest.mark.parametrize("n", range(1, 8))
def test_crash_after_journal_write_recovers(tmp_path, n):
    """F02：真实子进程在清单第 n 次落盘后（实际发布前）终止，可回滚。"""
    root = tmp_path / "space"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path

        from hvs_catalog.workspace import layout

        n = int(sys.argv[2])
        state = {"journal": 0}
        original = layout._write_init_journal

        def journal_hook(staging, journal):
            original(staging, journal)
            state["journal"] += 1
            if state["journal"] == n:
                os._exit(73)

        layout._write_init_journal = journal_hook
        layout.initialize_workspace(Path(sys.argv[1]))
        """,
        str(root),
        str(n),
    )
    assert result.returncode == 73, result.stderr
    space = initialize_workspace(root)
    try:
        assert (root / "workspace.json").is_file()
        assert (root / "business.db").is_file()
        assert not (root / ".workspace-init").exists()
        assert open_workspace(root).identity.space_id == space.identity.space_id
    finally:
        space.close()


@pytest.mark.parametrize("n", range(1, 7))
def test_crash_after_publish_recovers(tmp_path, n):
    """F02：真实子进程在第 n 次实际发布后终止，仍可回滚自身产物。"""
    root = tmp_path / f"space-{n}"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path

        from hvs_catalog.workspace import layout

        n = int(sys.argv[2])
        state = {"count": 0}
        original_file = layout._publish_init_file
        original_dir = layout._publish_init_directory

        def after():
            state["count"] += 1
            if state["count"] == n:
                os._exit(74)

        def publish_file(root, staging, journal, name):
            original_file(root, staging, journal, name)
            after()

        def publish_directory(root, staging, journal, name):
            original_dir(root, staging, journal, name)
            after()

        layout._publish_init_file = publish_file
        layout._publish_init_directory = publish_directory
        layout.initialize_workspace(Path(sys.argv[1]))
        """,
        str(root),
        str(n),
    )
    assert result.returncode == 74, result.stderr
    space = initialize_workspace(root)
    try:
        assert (root / "workspace.json").is_file()
        assert not (root / ".workspace-init").exists()
    finally:
        space.close()


def test_crash_after_complete_marker_opens_completed_space(tmp_path):
    """F02：完成标记发布后终止：空间按完成处理，残留暂存目录被清理。"""
    root = tmp_path / "space"
    result = _run_child(
        """
        import os
        import sys
        from pathlib import Path

        from hvs_catalog.workspace import layout

        original = layout._publish_init_file

        def publish_file(root, staging, journal, name):
            original(root, staging, journal, name)
            if name == layout.WORKSPACE_FILENAME:
                os._exit(75)

        layout._publish_init_file = publish_file
        layout.initialize_workspace(Path(sys.argv[1]))
        """,
        str(root),
    )
    assert result.returncode == 75, result.stderr
    assert (root / "workspace.json").is_file()
    assert (root / ".workspace-init").exists()
    space = initialize_workspace(root)
    try:
        assert not (root / ".workspace-init").exists()
        assert open_workspace(root).identity.space_id == space.identity.space_id
    finally:
        space.close()


# ---- R2-01：任何清理前完整核验清单与待清理内容 ----


def _forge_staging(root, journal_payload, *, note="用户资料，不能删除"):
    """在已完成空间内伪造暂存目录与清单（R2-01 场景构造）。"""
    staging = root / ".workspace-init"
    staging.mkdir()
    (staging / "journal.json").write_text(json.dumps(journal_payload), encoding="utf-8")
    (staging / "user-note.txt").write_text(note, encoding="utf-8")
    return staging


def test_malformed_completed_journal_preserves_staging(tmp_path):
    """R2-01：可解析但条目损坏的 complete 清单不能触发删除。"""
    root = tmp_path / "space"
    initialize_workspace(root).close()
    staging = _forge_staging(
        root, {"state": "complete", "published": [{"invalid_entry": True}]}
    )
    note = staging / "user-note.txt"
    journal_bytes = (staging / "journal.json").read_bytes()
    with pytest.raises(WorkspaceIntegrityError, match="清单内容不完整"):
        initialize_workspace(root)
    assert note.read_text(encoding="utf-8") == "用户资料，不能删除"
    assert (staging / "journal.json").read_bytes() == journal_bytes
    assert staging.exists()


def test_wellformed_completed_journal_with_user_content_preserved(tmp_path):
    """R2-01：结构完好且绑定正确的 complete 清单，含未知文件仍拒绝清理。"""
    root = tmp_path / "space"
    space = initialize_workspace(root)
    space_id = space.identity.space_id
    space.close()
    staging = _forge_staging(
        root, {"state": "complete", "space_id": space_id, "published": []}
    )
    with pytest.raises(WorkspaceConflictError, match="无法证明属于初始化"):
        initialize_workspace(root)
    assert (staging / "user-note.txt").exists()
    assert (staging / "journal.json").exists()


def test_completed_journal_bound_to_other_space_rejected(tmp_path):
    """R2-01：清单绑定其他空间时不能按本空间完成残留清理。"""
    root = tmp_path / "space"
    initialize_workspace(root).close()
    other = tmp_path / "other"
    other_space_id = initialize_workspace(other).identity.space_id
    staging = _forge_staging(
        root,
        {"state": "complete", "space_id": other_space_id, "published": []},
        note="",
    )
    (staging / "user-note.txt").unlink()  # 只留清单，排除内容检查的干扰
    with pytest.raises(WorkspaceConflictError, match="不一致"):
        initialize_workspace(root)
    assert (staging / "journal.json").exists()


def test_structurally_broken_active_journal_preserves_products(tmp_path):
    """R2-01：active 清单条目损坏时拒绝接管，已发布产物不回滚。"""
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    staging.mkdir()
    journal = {
        "state": "active",
        "space_id": "probe-space",
        "published": [
            {"path": "sources", "kind": "directory"},
            {"path": "config.json", "kind": "file"},  # 缺 sha256
        ],
    }
    (staging / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    (root / "sources").mkdir()
    with pytest.raises(WorkspaceIntegrityError, match="清单内容不完整"):
        initialize_workspace(root)
    assert (root / "sources").exists()  # 没有按损坏清单回滚任何产物
    assert (staging / "journal.json").read_bytes() == json.dumps(journal).encode()


def test_completed_cleanup_waits_for_init_mutex(tmp_path):
    """R2-01：完成后的清理同样遵循初始化互斥。"""
    root = tmp_path / "space"
    space = initialize_workspace(root)
    space_id = space.identity.space_id
    space.close()
    staging = root / ".workspace-init"
    staging.mkdir()
    (staging / "journal.json").write_text(
        json.dumps({"state": "complete", "space_id": space_id, "published": []}),
        encoding="utf-8",
    )
    release = tmp_path / "release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys
                import time
                from pathlib import Path

                from hvs_catalog.workspace import layout

                root, release = Path(sys.argv[1]), Path(sys.argv[2])
                staging = root / layout.INIT_STAGING_DIRNAME
                lock = layout._staging_lock(staging)
                lock.acquire()
                print("locked", flush=True)
                for _ in range(600):
                    if release.exists():
                        break
                    time.sleep(0.1)
                lock.release()
                """
            ),
            str(root),
            str(release),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        for _ in range(300):
            line = child.stdout.readline()
            if line.strip() == "locked":
                break
        else:
            raise AssertionError("子进程未取得初始化锁")
        with pytest.raises(WorkspaceBusyError, match="另一执行者"):
            initialize_workspace(root)
    finally:
        release.write_text("go", encoding="utf-8")
        child.wait(timeout=30)
    initialize_workspace(root).close()
    assert not staging.exists()


# ---- R3-01：回滚不能沿符号链接或嵌套路径删除空间外/未知文件 ----


def _crash_after_initial_journal(root) -> subprocess.CompletedProcess:
    """真实子进程在首份清单落盘后立即退出，保留 active 清单与 space_id。"""
    return _run_child(
        """
        import os
        import sys
        from pathlib import Path

        from hvs_catalog.workspace import layout

        original = layout._write_init_journal

        def stop_after_initial(staging, journal):
            original(staging, journal)
            os._exit(73)

        layout._write_init_journal = stop_after_initial
        layout.initialize_workspace(Path(sys.argv[1]))
        """,
        str(root),
    )


def test_rollback_rejects_journal_path_through_symlink(tmp_path):
    """R3-01：清单加入经中间目录符号链接的条目，回滚整体拒绝。"""
    root = tmp_path / "space"
    result = _crash_after_initial_journal(root)
    assert result.returncode == 73, result.stderr
    journal_path = root / ".workspace-init" / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["state"] == "active"

    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "user.txt"
    victim.write_bytes(b"outside user content must survive")
    (root / "alias").symlink_to(outside, target_is_directory=True)
    journal["published"].append(
        {
            "path": "alias/user.txt",
            "kind": "file",
            "sha256": hashlib.sha256(victim.read_bytes()).hexdigest(),
        }
    )
    poisoned = json.dumps(journal).encode()
    journal_path.write_bytes(poisoned)

    with pytest.raises(WorkspaceIntegrityError, match="清单内容不完整"):
        initialize_workspace(root)
    # 空间外字节不变；投毒清单与未知链接逐字节保留
    assert victim.read_bytes() == b"outside user content must survive"
    assert journal_path.read_bytes() == poisoned
    assert (root / "alias").is_symlink()

    # 移除投毒条目与链接后，同一中断现场仍可正常恢复初始化
    journal["published"].pop()
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    (root / "alias").unlink()
    initialize_workspace(root).close()
    assert (root / "workspace.json").is_file()


def test_rollback_rejects_nested_path_without_symlink(tmp_path):
    """R3-01：嵌套路径不是初始化发布的根级名称，即使无符号链接也拒绝。"""
    root = tmp_path / "space"
    root.mkdir()
    staging = root / ".workspace-init"
    staging.mkdir()
    payload = b"user file outside init protocol"
    (root / "sub").mkdir()
    (root / "sub" / "user.txt").write_bytes(payload)
    journal = {
        "state": "active",
        "space_id": "probe-space",
        "published": [
            {
                "path": "sub/user.txt",
                "kind": "file",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    (staging / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(WorkspaceIntegrityError, match="清单内容不完整"):
        initialize_workspace(root)
    assert (root / "sub" / "user.txt").read_bytes() == payload  # 未被回滚删除
    assert (staging / "journal.json").read_text(encoding="utf-8") == json.dumps(journal)
