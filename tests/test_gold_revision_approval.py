"""No second Git writer; current revisions preserve attribution and exact premise."""

from importlib.util import find_spec

import pytest
from gold_save_helpers import approve_revision, flow, revise, snapshot

from hvs_catalog.evaluation import GoldTargetError, approved_save


def test_removed_git_refresh_is_not_a_compatibility_writer():
    assert find_spec("hvs_catalog.evaluation.git_revision") is None


def test_two_reviewed_revisions_old_permission_cannot_overwrite(tmp_path):
    args = flow(tmp_path)
    approved_save(**args)
    first = approve_revision(revise(args, work=tmp_path / "first"))
    competing = approve_revision(
        revise(args, value="614.0", work=tmp_path / "competing")
    )
    approved_save(**first)
    before = snapshot(args["target"].root)
    with pytest.raises(GoldTargetError, match="Expected old"):
        approved_save(**competing)
    assert snapshot(args["target"].root) == before
    assert (
        first["target"].read_current()[0].metadata.annotation.original_annotator
        == "synthetic-expert"
    )
    assert not any(p.name.startswith("v") for p in first["target"].root.iterdir())
