"""Old demo persistence retired; never adopt or clear an unknown directory."""

import pytest

from hvs_catalog.demo import runner
from hvs_catalog.workspace import WorkspaceError, initialize_workspace


@pytest.mark.parametrize("kind", ["old-demo", "ordinary-workspace"])
def test_demo_rejects_non_demo_space_without_changes(tmp_path, kind):
    root = tmp_path / kind
    if kind == "ordinary-workspace":
        space = initialize_workspace(root)
        space.close()
    else:
        root.mkdir()
        for name in ["business.db", "checkpoint.db", "service.db"]:
            (root / name).write_bytes(b"retired database, keep bytes")
    before = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    with pytest.raises((WorkspaceError, ValueError)):
        runner.run(root)
    after = {
        p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert after == before
    assert not runner.calls_path(root).exists()
