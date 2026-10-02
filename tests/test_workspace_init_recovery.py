"""Whole-directory initialization: precise failure windows, contention, and nonempty
rejection without taking over old staging.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from hvs_catalog.workspace import (
    UnknownWorkspaceError,
    WorkspaceConflictError,
    initialize_workspace,
    layout,
    open_workspace,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("existing", [False, True])
def test_initialize_empty_or_new_directory(tmp_path, existing):
    root = tmp_path / "space"
    if existing:
        root.mkdir()
    space = initialize_workspace(root)
    assert space.read_config() == {}
    space.close()
    assert list(tmp_path.glob(".space-init-*")) == []


@pytest.mark.parametrize("window", ["before", "after"])
@pytest.mark.parametrize("existing", [False, True])
def test_hard_exit_at_whole_directory_publish(tmp_path, window, existing):
    root = tmp_path / "space"
    if existing:
        root.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import os, sys
            from pathlib import Path
            from hvs_catalog.workspace import layout
            original = layout._publish_initialization
            def publish(staging, root):
                if sys.argv[2] == 'before':
                    os._exit(71)
                original(staging, root)
                os._exit(72)
            layout._publish_initialization = publish
            layout.initialize_workspace(Path(sys.argv[1]))
        """),
            str(root),
            window,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (71 if window == "before" else 72), result.stderr
    leftovers = list(tmp_path.glob(".space-init-*"))
    if window == "before":
        assert len(leftovers) == 1
        staged = open_workspace(leftovers[0])
        staged_id = staged.identity.space_id
        staged.close()
        with pytest.raises(UnknownWorkspaceError):
            open_workspace(root)
        # A new call neither resumes nor deletes old staging; the empty target permits fresh independent creation.
        fresh = initialize_workspace(root)
        assert fresh.identity.space_id != staged_id
        fresh.close()
        assert leftovers[0].exists()
    else:
        assert leftovers == []
        complete = open_workspace(root)
        identity = complete.identity
        complete.close()
        reopened = initialize_workspace(root, config={"ignored": True})
        assert reopened.identity == identity
        assert reopened.read_config() == {}
        reopened.close()


def test_normal_exception_cleans_only_this_call(tmp_path, monkeypatch):
    old = tmp_path / ".space-init-old"
    old.mkdir()
    (old / "note").write_bytes(b"keep")

    def fail(staging, root):
        assert (staging / "workspace.json").is_file()
        raise RuntimeError("injected before publish")

    monkeypatch.setattr(layout, "_publish_initialization", fail)
    with pytest.raises(RuntimeError, match="injected"):
        initialize_workspace(tmp_path / "space")
    assert list(tmp_path.glob(".space-init-*")) == [old]
    assert (old / "note").read_bytes() == b"keep"


def test_nonempty_unknown_target_preserved(tmp_path):
    root = tmp_path / "space"
    (root / ".workspace-init" / "nested").mkdir(parents=True)
    note = root / ".workspace-init" / "nested" / "note"
    note.write_bytes(b"user data")
    with pytest.raises(WorkspaceConflictError, match="非空"):
        initialize_workspace(root)
    assert note.read_bytes() == b"user data"
    assert not (root / "workspace.json").exists()


@pytest.mark.parametrize("winner", ["workspace", "user-files"])
@pytest.mark.parametrize("window", ["prepared", "rename"])
def test_publish_race_does_not_overwrite_winner(tmp_path, winner, window):
    root = tmp_path / "space"
    # Pipe handshakes stop exactly after staging completes and before rename, without guessed sleeps.
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
            import sys
            from pathlib import Path
            from hvs_catalog.workspace import layout, WorkspaceConflictError
            original = (layout.os.rename if sys.argv[2] == 'rename'
                        else layout._publish_initialization)
            def publish(staging, root):
                print('prepared', flush=True)
                assert sys.stdin.readline().strip() == 'publish'
                original(staging, root)
            if sys.argv[2] == 'rename':
                layout.os.rename = publish
            else:
                layout._publish_initialization = publish
            try:
                layout.initialize_workspace(Path(sys.argv[1]))
            except WorkspaceConflictError:
                print('conflict', flush=True)
                sys.exit(3)
        """),
            str(root),
            window,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "prepared"
        if winner == "workspace":
            space = initialize_workspace(root, config={"winner": True})
            identity = space.identity
            space.close()
        else:
            root.mkdir()
            (root / "note").write_bytes(b"winner")
        out, err = child.communicate("publish\n", timeout=30)
        assert child.returncode == 3, out + err
        assert "conflict" in out
        if winner == "workspace":
            space = open_workspace(root)
            assert space.identity == identity
            assert space.read_config() == {"winner": True}
            space.close()
        else:
            assert list(root.iterdir()) == [root / "note"]
            assert (root / "note").read_bytes() == b"winner"
        assert list(tmp_path.glob(".space-init-*")) == []
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
