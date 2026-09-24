"""Approved Gold template: usable inputs, human lineage and fixed material scope."""

import copy
import json
import re

import pytest
from evaluation_helpers import (
    add_synthetic_metadata,
    build_approval_payload,
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)
from pydantic import ValidationError

from hvs_catalog.evaluation import (
    GoldApprovalError,
    GoldCheckError,
    GoldFormatError,
    GoldTarget,
    GoldTargetError,
    SaveApproval,
    approved_save,
    check_gold_annotation,
    migrate_legacy_annotation,
    parse_gold_annotation,
    prepare_save,
    query_operation,
)
from hvs_catalog.evaluation.generate import build_outputs
from hvs_catalog.evaluation.models import GoldAnnotation


def draft(tmp_path):
    digest = write_synthetic_pdf(tmp_path / "paper.pdf")
    return build_fresh_draft(pdf_sha256=digest)


def prepare(tmp_path, data, version=None):
    repo = tmp_path / "private-repo"
    repo.mkdir(exist_ok=True)
    (repo / "README.md").write_text("Private synthetic Gold repository")
    (repo / ".git").mkdir(exist_ok=True)
    target = GoldTarget(repo / "data")
    path = tmp_path / "input.json"
    path.write_text(json.dumps(data))
    preparation = prepare_save(
        draft_path=path,
        material_root=tmp_path,
        work_dir=tmp_path / "work",
        target=target,
        expected_old_version=version,
    )
    return {
        "draft_path": path,
        "material_root": tmp_path,
        "target": target,
        "preparation_dir": preparation.directory,
        "approval": SaveApproval.model_validate(build_approval_payload(preparation)),
        "operation_id": f"op-{preparation.manifest.new_version}",
    }


def test_generated_template_is_complete_json_and_valid_draft(tmp_path):
    outputs = build_outputs()
    raw = json.loads(outputs["legacy/templates/gold-annotation.json"])
    markdown_raw = (
        outputs["legacy/templates/gold-annotation.md"]
        .split("```json\n")[1]
        .split("```", 1)[0]
    )
    assert json.loads(markdown_raw) == raw
    parsed = GoldAnnotation.model_validate(
        raw
    )  # no normalization concealing absent fields
    assert parsed.model_dump(mode="json", by_alias=True) == raw
    report = check_gold_annotation(parsed, material_root=tmp_path)
    assert (
        report.blocking_count > 0
    )  # examples cannot masquerade as reviewed real inputs
    assert {p.code for p in report.problems} >= {
        "missing_preparer",
        "missing_paper_version",
        "material_correspondence_pending",
    }
    doc = outputs["legacy/gold-format.md"]
    assert "{GOLD_SCHEMA_NAME}" not in doc and "PydanticUndefined" not in doc
    assert "evaluation.hvs_contribution_gold" in doc
    for section in doc.split("| Field | Type | Presence | Default | Constraints |\n")[
        1:
    ]:
        for row in section.split("\n\n", 1)[0].splitlines():
            if row.startswith("|"):
                assert len(re.split(r"(?<!\\)\|", row)) == 7, row


def test_template_can_be_completed_without_changing_shape(tmp_path):
    raw = json.loads(build_outputs()["legacy/templates/gold-annotation.json"])
    raw["files"][0]["sha256"] = write_synthetic_pdf(tmp_path / "paper.pdf")
    add_synthetic_metadata(raw)
    args = prepare(tmp_path, raw)
    assert approved_save(**args).version == 1


def test_reader_assigns_only_missing_technical_ids(tmp_path):
    raw = draft(tmp_path)
    first = raw["contributions"][0]
    first["contribution_id"] = "c1"
    second = copy.deepcopy(first)
    second.pop("contribution_id")
    second["identifiers"][0]["value"] = "SYN-B"
    raw["contributions"].insert(0, second)
    raw["pending"] = [
        {
            "category": "science_review",
            "subject": "SYN-B",
            "description": "Synthetic ambiguity.",
        }
    ]
    before = copy.deepcopy(raw)
    parsed = parse_gold_annotation(raw)
    assert [c.contribution_id for c in parsed.contributions] == ["c2", "c1"]
    assert parsed.pending[0].item_id == "p1"
    assert parsed == parse_gold_annotation(raw) and before == raw
    assert parsed.contributions[0].quantities == parsed.contributions[1].quantities
    raw["contributions"][0]["contribution_id"] = "c1"
    with pytest.raises(GoldFormatError, match="contribution_id 重复"):
        parse_gold_annotation(raw)


@pytest.mark.parametrize("missing", ["maker", "paper_version", "correspondence"])
def test_missing_metadata_blocks_prepare(tmp_path, missing):
    raw = draft(tmp_path)
    if missing == "maker":
        raw["provenance"].pop("prepared_by")
    elif missing == "paper_version":
        raw["paper"].pop("version")
    else:
        raw["material_correspondence"] = {"status": "pending"}
    with pytest.raises(GoldCheckError):
        prepare(tmp_path, raw)


@pytest.mark.parametrize(
    "fault",
    ["ai_without_model", "maker_naive_time", "checker_ai", "checked_without_basis"],
)
def test_actor_and_checked_material_claims_are_validated(tmp_path, fault):
    raw = draft(tmp_path)
    if fault == "ai_without_model":
        raw["provenance"]["prepared_by"]["actor"]["kind"] = "ai"
    elif fault == "maker_naive_time":
        raw["provenance"]["prepared_by"]["prepared_at"] = "2026-09-22T00:00:00"
    elif fault == "checker_ai":
        raw["material_correspondence"]["checked_by"]["kind"] = "ai"
    else:
        raw["material_correspondence"]["basis"] = ""
    with pytest.raises(GoldFormatError):
        parse_gold_annotation(raw)


@pytest.mark.parametrize("fault", ["ai", "missing_kind", "naive_time"])
def test_approval_requires_explicit_human_and_time(tmp_path, fault):
    args = prepare(tmp_path, draft(tmp_path))
    payload = args["approval"].model_dump(mode="json")
    if fault == "ai":
        payload["approver_kind"] = "ai"
    elif fault == "missing_kind":
        payload.pop("approver_kind")
    else:
        payload["approved_at"] = "2026-09-22T00:00:00"
    with pytest.raises(ValidationError):
        SaveApproval.model_validate(payload)


def test_copied_ai_approval_cannot_bypass_write_validation(tmp_path):
    args = prepare(tmp_path, draft(tmp_path))
    args["approval"] = args["approval"].model_copy(update={"approver_kind": "ai"})
    with pytest.raises(ValidationError):
        approved_save(**args)
    assert args["target"].read_entry("gold-synth-001") is None


def test_new_gold_keeps_first_human_across_revisions(tmp_path):
    raw = draft(tmp_path)
    raw["provenance"]["prepared_by"]["actor"] = {
        "actor_id": "synthetic-ai",
        "kind": "ai",
        "model": "synthetic-model-v1",
    }
    args = prepare(tmp_path, raw)
    args["approval"] = args["approval"].model_copy(update={"approved_by": "will"})
    first = approved_save(**args)
    raw["provenance"]["prepared_by"].update(
        action="revision", description="Synthetic revision."
    )
    raw["provenance"]["prepared_by"]["actor"]["model"] = "synthetic-model-v2"
    second_args = prepare(tmp_path, raw, version=1)
    second_args["approval"] = second_args["approval"].model_copy(
        update={"approved_by": "shunhong_deng"}
    )
    second = approved_save(**second_args)
    target = args["target"]
    assert (
        target.read_current_answer(raw["document_id"])[
            1
        ].provenance.original_approval_version
        == 1
    )
    assert (
        target.read_current_answer(raw["document_id"])[
            1
        ].provenance.prepared_by.actor.model
        == "synthetic-model-v2"
    )
    approval1 = json.loads(
        (
            target.root
            / next(f.path for f in first.files if f.path.endswith("approval.json"))
        ).read_text()
    )
    approval2 = json.loads(
        (
            target.root
            / next(f.path for f in second.files if f.path.endswith("approval.json"))
        ).read_text()
    )
    assert (approval1["approved_by"], approval2["approved_by"]) == (
        "will",
        "shunhong_deng",
    )
    provenance = json.loads(
        (
            target.root
            / next(f.path for f in second.files if f.path.endswith("provenance.json"))
        ).read_text()
    )
    assert provenance["original_expert_approval"].endswith("/v1/approval.json")
    assert query_operation(operation_id="op-1", target=target).state == "saved"


def test_migration_preserves_raw_identity_and_confirmed_human(tmp_path):
    draft(tmp_path)
    old = tmp_path / "old.json"
    old.write_text(json.dumps(build_legacy_v4_doc()))
    before = old.read_bytes()
    result = migrate_legacy_annotation(
        legacy_path=old,
        draft_dir=tmp_path / "migration",
        known_pdfs=[tmp_path / "paper.pdf"],
    )
    assert (
        result.draft.provenance.original_expert is None
    )  # never infer human from legacy annotator
    assert result.draft.provenance.legacy.annotator == "synthetic-annotator"
    raw = add_synthetic_metadata(result.draft.model_dump(mode="json", by_alias=True))
    raw["provenance"]["original_expert"] = {
        "expert": {"actor_id": "will", "kind": "human"},
        "annotated_at": "2026-01-01T00:00:00Z",
        "basis": "Synthetic original human review record.",
    }
    args = prepare(tmp_path, raw)
    args["approval"] = args["approval"].model_copy(
        update={"approved_by": "shunhong_deng"}
    )
    approved_save(**args)
    raw["provenance"]["original_expert"]["expert"]["actor_id"] = "another-expert"
    with pytest.raises(GoldTargetError, match="原始人类专家"):
        prepare(tmp_path, raw, version=1)
    assert old.read_bytes() == before


def test_unknown_historical_time_can_be_completed_without_replacing_expert(tmp_path):
    draft(tmp_path)
    old = tmp_path / "old.json"
    old.write_text(json.dumps(build_legacy_v4_doc()))
    result = migrate_legacy_annotation(
        legacy_path=old,
        draft_dir=tmp_path / "migration",
        known_pdfs=[tmp_path / "paper.pdf"],
    )
    raw = add_synthetic_metadata(result.draft.model_dump(mode="json", by_alias=True))
    raw["provenance"]["original_expert"] = {
        "expert": {"actor_id": "will", "kind": "human"},
        "annotated_at": None,
        "basis": "Synthetic original identity record.",
        "unknown_reason": "Original date not yet confirmed.",
    }
    args = prepare(tmp_path, raw)
    assert "original_expert_unknown" in args["approval"].accepted_pending
    approved_save(**args)
    raw["provenance"]["original_expert"].update(
        annotated_at="2026-01-01T00:00:00Z",
        unknown_reason=None,
        basis="Synthetic original identity record and recovered timestamp.",
    )
    revision = prepare(tmp_path, raw, version=1)
    assert "original_expert_unknown" not in revision["approval"].accepted_pending
    assert approved_save(**revision).version == 2


@pytest.mark.parametrize("field", ["maker", "paper_version", "differences", "checker"])
def test_attribution_and_material_changes_invalidate_approval(tmp_path, field):
    raw = draft(tmp_path)
    args = prepare(tmp_path, raw)
    if field == "maker":
        raw["provenance"]["prepared_by"]["actor"]["actor_id"] = "other-maker"
    elif field == "paper_version":
        raw["paper"]["version"] = "synthetic-v2"
    elif field == "differences":
        raw["material_correspondence"]["differences"] = [
            "Additional synthetic appendix."
        ]
    else:
        raw["material_correspondence"]["checked_by"]["actor_id"] = "other-checker"
    args["draft_path"].write_text(json.dumps(raw))
    with pytest.raises(GoldApprovalError):
        approved_save(**args)
    assert args["target"].read_entry(raw["document_id"]) is None


def test_latex_replaced_after_preparation_rejected(tmp_path):
    args = prepare(tmp_path, draft(tmp_path))
    (tmp_path / "paper.tex").write_text("replaced")
    with pytest.raises(GoldCheckError, match="LaTeX"):
        approved_save(**args)


def test_private_repository_root_is_not_managed_target(tmp_path):
    args = prepare(tmp_path, draft(tmp_path))
    repo = args["target"].root.parent
    before = (repo / "README.md").read_bytes()
    with pytest.raises(GoldTargetError):
        GoldTarget(repo).initialize()
    receipt = approved_save(**args)
    assert receipt.files[0].path.startswith("annotations/")
    assert (repo / "README.md").read_bytes() == before and (repo / ".git").is_dir()


def test_known_differences_require_explicit_pending_acceptance(tmp_path):
    raw = draft(tmp_path)
    raw["material_correspondence"]["differences"] = [
        "Synthetic PDF appendix absent from source."
    ]
    args = prepare(tmp_path, raw)
    assert "material_differences" in args["approval"].accepted_pending
    rejected = dict(
        args, approval=args["approval"].model_copy(update={"accepted_pending": []})
    )
    with pytest.raises(GoldApprovalError):
        approved_save(**rejected)
    assert approved_save(**args).version == 1
