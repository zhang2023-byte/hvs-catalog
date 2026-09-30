"""Unified current Gold writes: authorization, prerequisites and target protection.

Replaces preparation/index/operation tests, not scientific checks or readiness.
"""

import json

import pytest
from gold_save_helpers import (
    approve_revision,
    edit_pair,
    flow,
    revise,
    snapshot,
)
from pydantic import ValidationError

from hvs_catalog.evaluation import (
    GoldApprovalError,
    GoldCheckError,
    GoldIntegrityError,
    GoldTarget,
    GoldTargetError,
    SaveApproval,
    approved_save,
    check_save,
    read_compact_gold_pair,
)


def test_new_and_revision_same_writer_fixed_paths(tmp_path):
    args = flow(tmp_path)
    assert not args["target"].root.exists()  # check_save is read-only
    result = approved_save(**args)
    assert not result.already_present
    current, approval = args["target"].read_current()
    assert current.answer.contributions[0].quantities[0].values[0].value == "612.0"
    before = snapshot(args["target"].root)
    assert approved_save(**args).already_present
    assert snapshot(args["target"].root) == before
    revised = approve_revision(revise(args))
    assert approved_save(**revised).pair_sha256 != result.pair_sha256
    pair, _ = args["target"].read_current()
    assert pair.metadata.annotation == current.metadata.annotation
    assert (
        pair.metadata.revision.previous_answer_sha256 == approval.binding.answer_sha256
    )
    assert {p.name for p in args["target"].root.iterdir()} == {
        "gold-target.json",
        ".lock",
        "answer.json",
        "metadata.json",
        "approval.json",
    }
    with pytest.raises(GoldApprovalError):
        approved_save(**args)  # work pair now differs from old approval
    # Even the exact old pair cannot overwrite a later revision.
    from hvs_catalog.evaluation import write_compact_gold_pair

    old_work = tmp_path / "old-pair"
    write_compact_gold_pair(current, old_work)
    with pytest.raises(GoldTargetError, match="Expected old"):
        approved_save(**dict(args, pair_path=old_work))


@pytest.mark.parametrize(
    "fault",
    [
        "ai",
        "no_kind",
        "no_time",
        "naive_time",
        "no_review",
        "blank_human",
        "blank_basis",
        "binding_hash",
        "binding_target",
        "binding_rules",
        "pending",
    ],
)
def test_approval_refusals_do_not_create_target(tmp_path, fault):
    args = flow(tmp_path)
    payload = args["approval"].model_dump(mode="json")
    if fault == "ai":
        payload["approver_kind"] = "ai"
    elif fault == "no_kind":
        payload.pop("approver_kind")
    elif fault == "no_time":
        payload.pop("approved_at")
    elif fault == "naive_time":
        payload["approved_at"] = "2026-09-30T00:00:00"
    elif fault == "no_review":
        payload["scientific_review"] = False
    elif fault == "blank_human":
        payload["approved_by"] = " "
    elif fault == "blank_basis":
        payload["basis"] = " "
    elif fault == "binding_hash":
        payload["binding_sha256"] = "a" * 64
    elif fault == "binding_target":
        payload["binding"]["target_path"] += "-other"
    elif fault == "binding_rules":
        payload["binding"]["rules_sha256"] = "b" * 64
    else:
        payload["accepted_pending"] = ["nonexistent"]
    with pytest.raises((GoldApprovalError, ValidationError)):
        approved_save(**dict(args, approval=SaveApproval.model_validate(payload)))
    assert not args["target"].root.exists()


def test_missing_and_model_copy_ai_approval_rejected(tmp_path):
    args = flow(tmp_path)
    with pytest.raises(GoldApprovalError):
        approved_save(**dict(args, approval=None))
    with pytest.raises(GoldApprovalError):
        approved_save(
            **dict(
                args,
                approval=args["approval"].model_copy(update={"approver_kind": "ai"}),
            )
        )
    assert not args["target"].root.exists()


@pytest.mark.parametrize(
    "change",
    ["answer", "title", "reviewer", "rules", "source", "pdf", "scope", "original"],
)
def test_changed_pair_rejects_original_approval(tmp_path, change):
    args = flow(tmp_path)
    approved_save(**args)
    before = snapshot(args["target"].root)
    pair = read_compact_gold_pair(args["pair_path"])
    answer = pair.answer.model_dump(mode="json")
    metadata = pair.metadata.model_dump(mode="json", by_alias=True)
    if change == "answer":
        answer["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    elif change == "title":
        metadata["paper"]["title"] += " revised"
    elif change == "reviewer":
        metadata["annotation"]["reviewer"] = "another"
    elif change == "rules":
        metadata["rules"]["sha256"] = "b" * 64
    elif change == "source":
        metadata["source"]["version"] = "v2"
    elif change == "pdf":
        metadata["files"][0]["sha256"] = "b" * 64
    elif change == "scope":
        metadata["review"]["scope"]["basis"] += " revised"
    else:
        metadata["annotation"]["original_annotator"] = "another"
    with pytest.raises((GoldApprovalError, GoldCheckError, ValidationError)):
        changed = edit_pair(args, answer=answer, metadata=metadata)
        approved_save(**changed)
    assert snapshot(args["target"].root) == before


@pytest.mark.parametrize(
    "identity", ["original_annotator", "original_annotated_at", "original_models"]
)
def test_revision_cannot_replace_original_source(tmp_path, identity):
    args = flow(tmp_path)
    approved_save(**args)
    changed = revise(args)
    pair = read_compact_gold_pair(changed["pair_path"])
    metadata = pair.metadata.model_dump(mode="json", by_alias=True)
    metadata["annotation"][identity] = (
        ["other"] if identity == "original_models" else "other"
    )
    changed = edit_pair(changed, metadata=metadata)
    with pytest.raises(GoldTargetError, match="original"):
        approve_revision(changed)


def test_revision_needs_reason_and_predecessor_hash(tmp_path):
    args = flow(tmp_path)
    approved_save(**args)
    pair = read_compact_gold_pair(args["pair_path"])
    with pytest.raises(GoldTargetError, match="Revision needs"):
        check_save(
            pair_path=args["pair_path"],
            material_root=args["material_root"],
            target=args["target"],
            expected_old=args["target"].fingerprint(),
        )
    metadata = pair.metadata.model_dump(mode="json", by_alias=True)
    changed = revise(args)
    metadata["revision"] = read_compact_gold_pair(
        changed["pair_path"]
    ).metadata.revision.model_dump(mode="json")
    metadata["revision"]["previous_answer_sha256"] = "b" * 64
    with pytest.raises(GoldTargetError, match="previous"):
        approve_revision(edit_pair(changed, metadata=metadata))


@pytest.mark.parametrize("name", ["paper.pdf", "paper.tex"])
def test_changed_material_refuses_and_preserves_current(tmp_path, name):
    args = flow(tmp_path)
    approved_save(**args)
    before = snapshot(args["target"].root)
    (args["material_root"] / name).write_bytes(b"replaced")
    with pytest.raises(GoldCheckError):
        approved_save(**args)
    assert snapshot(args["target"].root) == before


def test_foreign_target_rejected_before_initialization(tmp_path):
    args = flow(tmp_path)
    other = GoldTarget(tmp_path / "other")
    with pytest.raises(GoldApprovalError, match="binding"):
        approved_save(**dict(args, target=other))
    assert not other.root.exists()


@pytest.mark.parametrize(
    "layout", ["unknown", "repo", "old", "marker", "marker_dir", "file"]
)
def test_target_protection_no_changes(tmp_path, layout):
    root = tmp_path / "target"
    if layout == "file":
        root.write_text("untouched")
        before = root.read_bytes()
    else:
        root.mkdir()
        if layout == "unknown":
            (root / "valuable.txt").write_text("untouched")
        elif layout == "repo":
            (root / "README.md").write_text("Private synthetic repo")
            (root / ".git").mkdir()
        elif layout == "old":
            (root / "annotations" / "gold-synth" / "v1").mkdir(parents=True)
            (root / "gold-target.json").write_text(
                json.dumps({"format": "hvs-catalog-gold-target", "format_version": 1})
            )
            (root / "operations").mkdir()
            (root / "operations" / "approval.json").write_text(
                "do not read/reinterpret"
            )
        elif layout == "marker":
            (root / "gold-target.json").write_text("broken")
        else:
            (root / "gold-target.json").mkdir()
        before = snapshot(root)
    with pytest.raises(GoldTargetError):
        GoldTarget(root).initialize()
    assert (root.read_bytes() if layout == "file" else snapshot(root)) == before


@pytest.mark.parametrize("where", ["root", "answer", "metadata", "approval", "lock"])
def test_symlink_target_protection(tmp_path, where):
    args = flow(tmp_path)
    target = args["target"]
    outside = tmp_path / "outside"
    outside.mkdir()
    if where == "root":
        alias = tmp_path / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with pytest.raises(GoldTargetError, match="symbolic"):
            GoldTarget(alias)
    else:
        target.initialize()
        path = target.root / (".lock" if where == "lock" else where + ".json")
        path.symlink_to(outside / "uncreated")
        with pytest.raises(GoldTargetError, match="Symbolic"):
            approved_save(**args)
    assert list(outside.iterdir()) == []


def test_parent_alias_binds_real_target_and_changed_alias_rejects(tmp_path):
    args = flow(tmp_path)
    parent = args["target"].root.parent
    parent.mkdir(parents=True)
    alias = tmp_path / "parent-alias"
    alias.symlink_to(parent, target_is_directory=True)
    aliased = GoldTarget(alias / args["target"].root.name)
    checked = check_save(
        pair_path=args["pair_path"], material_root=args["material_root"], target=aliased
    )
    assert checked.binding == args["approval"].binding
    approved_save(**dict(args, target=aliased))
    assert args["target"].fingerprint() == aliased.fingerprint()
    alias.unlink()
    other = tmp_path / "other-parent"
    other.mkdir()
    alias.symlink_to(other, target_is_directory=True)
    changed_target = GoldTarget(alias / args["target"].root.name)
    with pytest.raises(GoldApprovalError, match="binding"):
        approved_save(**dict(args, target=changed_target))
    assert list(other.iterdir()) == []


def test_incomplete_and_tampered_current_rejected(tmp_path):
    args = flow(tmp_path)
    approved_save(**args)
    metadata = args["target"].root / "metadata.json"
    data = metadata.read_bytes()
    metadata.unlink()
    with pytest.raises(GoldIntegrityError, match="Incomplete"):
        args["target"].read_current()
    metadata.write_bytes(data + b" ")
    with pytest.raises(GoldIntegrityError, match="bytes"):
        args["target"].read_current()


@pytest.mark.parametrize("scope", ["complete", "partial"])
def test_empty_list_scope_and_uncertainty_save_not_scoring_readiness(tmp_path, scope):
    from evaluation_helpers import build_fresh_draft, write_synthetic_pdf

    from hvs_catalog.evaluation import adapt_compact_gold_pair

    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    raw = build_fresh_draft(pdf_sha256=digest)
    raw.update(status="no_contributions", contributions=[])
    args = flow(tmp_path, raw=raw, scope=scope)
    if scope == "partial":
        with pytest.raises(GoldApprovalError):
            approved_save(
                **dict(
                    args,
                    approval=args["approval"].model_copy(
                        update={"accepted_pending": []}
                    ),
                )
            )
    approved_save(**args)
    pair = args["target"].read_current()[0]
    assert pair.answer.contributions == [] and pair.answer.coverage.value == scope
    if scope == "partial":
        from hvs_catalog.evaluation import GoldAdaptationError

        with pytest.raises(GoldAdaptationError, match="封闭子范围"):
            adapt_compact_gold_pair(pair)
    else:
        assert adapt_compact_gold_pair(pair).formal_readiness


def test_accepted_unresolved_content_preserved_and_old_approval_invalidated(tmp_path):
    args = flow(
        tmp_path,
        unresolved=[
            {
                "subject": "SYN-A identity",
                "value": "Synthetic identity remains unsettled",
            }
        ],
    )
    assert "unresolved_1" in args["approval"].accepted_pending
    with pytest.raises(GoldApprovalError):
        approved_save(
            **dict(
                args,
                approval=args["approval"].model_copy(update={"accepted_pending": []}),
            )
        )
    approved_save(**args)
    pair = args["target"].read_current()[0]
    assert pair.answer.unresolved[0].value == "Synthetic identity remains unsettled"
    from hvs_catalog.evaluation import adapt_compact_gold_pair

    assert not adapt_compact_gold_pair(pair).formal_readiness
    raw = pair.answer.model_dump(mode="json")
    raw["unresolved"][0]["value"] += " changed detail"
    with pytest.raises(GoldApprovalError):
        approved_save(**edit_pair(args, answer=raw))


def test_plain_draft_reader_is_not_formal_reader(tmp_path):
    args = flow(tmp_path)
    assert read_compact_gold_pair(args["pair_path"])
    with pytest.raises(GoldTargetError, match="layout"):
        GoldTarget(args["pair_path"]).read_current()
