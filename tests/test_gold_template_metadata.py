"""Typed templates, explicit identities/materials, and read-only migration lineage."""

import copy
import json
import re

import pytest
from evaluation_helpers import (
    add_synthetic_metadata,
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)
from gold_save_helpers import authorize, flow, make_pair

from hvs_catalog.evaluation import (
    GoldApprovalError,
    GoldFormatError,
    GoldTarget,
    approved_save,
    check_gold_annotation,
    check_save,
    migrate_legacy_annotation,
    parse_gold_annotation,
)
from hvs_catalog.evaluation.generate import build_outputs
from hvs_catalog.evaluation.models import GoldAnnotation


def draft(tmp_path):
    digest = write_synthetic_pdf(tmp_path / "paper.pdf")
    return build_fresh_draft(pdf_sha256=digest)


def test_generated_template_is_complete_json_and_valid_draft(tmp_path):
    outputs = build_outputs()
    raw = json.loads(outputs["legacy/templates/gold-annotation.json"])
    markdown_raw = (
        outputs["legacy/templates/gold-annotation.md"]
        .split("```json\n")[1]
        .split("```", 1)[0]
    )
    assert json.loads(markdown_raw) == raw
    parsed = GoldAnnotation.model_validate(raw)
    assert parsed.model_dump(mode="json", by_alias=True) == raw
    report = check_gold_annotation(parsed, material_root=tmp_path)
    assert report.blocking_count > 0
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


def test_template_completed_and_explicitly_converted_to_single_save_path(tmp_path):
    raw = json.loads(build_outputs()["legacy/templates/gold-annotation.json"])
    raw["files"][0]["sha256"] = write_synthetic_pdf(
        tmp_path / "materials" / "paper.pdf"
    )
    add_synthetic_metadata(raw)
    raw["paper"]["arxiv_id"] = "2501.00001"  # explicitly supplied synthetic identity
    material, path, pair = make_pair(tmp_path, raw=raw)
    target = GoldTarget(tmp_path / "target")
    checked = check_save(pair_path=path, material_root=material, target=target)
    approved_save(
        pair_path=path,
        material_root=material,
        target=target,
        approval=authorize(checked.binding),
    )
    assert target.read_current()[0].answer == pair.answer


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
def test_missing_draft_metadata_is_still_a_blocking_gap(tmp_path, missing):
    raw = draft(tmp_path)
    if missing == "maker":
        raw["provenance"].pop("prepared_by")
    elif missing == "paper_version":
        raw["paper"].pop("version")
    else:
        raw["material_correspondence"] = {"status": "pending"}
    assert (
        check_gold_annotation(
            parse_gold_annotation(raw), material_root=tmp_path
        ).blocking_count
        > 0
    )


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


def test_migration_keeps_raw_identity_no_inferred_expert_and_explicit_conversion(
    tmp_path,
):
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    old = tmp_path / "old.json"
    old.write_text(json.dumps(build_legacy_v4_doc()))
    before = old.read_bytes()
    result = migrate_legacy_annotation(
        legacy_path=old,
        draft_dir=tmp_path / "migration",
        known_pdfs=[tmp_path / "materials" / "paper.pdf"],
    )
    assert result.draft.provenance.original_expert is None
    assert result.draft.provenance.legacy.annotator == "synthetic-annotator"
    raw = add_synthetic_metadata(result.draft.model_dump(mode="json", by_alias=True))
    raw["files"][0]["sha256"] = digest
    raw["provenance"]["original_expert"] = {
        "expert": {"actor_id": "synthetic-human", "kind": "human"},
        "annotated_at": None,
        "basis": "Synthetic original identity record",
        "unknown_reason": "Original date unconfirmed",
    }
    parsed = parse_gold_annotation(raw)
    assert parsed.provenance.original_expert.annotated_at is None
    assert parsed.provenance.original_expert.expert.actor_id == "synthetic-human"
    # No old-format production writer remains. Explicit expert dispositions
    # and identity supplied to conversion are required, never inferred.
    from hvs_catalog.evaluation import compact_gold_annotation, write_compact_gold_pair

    pair = compact_gold_annotation(
        parsed,
        title="Synthetic migration",
        original_annotator="synthetic-human",
        reviewer="synthetic-reviewer",
        handled_pending_ids=[p.item_id for p in parsed.pending],
        material_review={
            "value": "checked",
            "basis": "Synthetic material review",
            "record_sha256": "a" * 64,
        },
        scope_review={
            "value": "complete",
            "basis": "Synthetic new scope review",
            "record_sha256": "a" * 64,
        },
    )
    path, _ = write_compact_gold_pair(pair, tmp_path / "converted")
    target = GoldTarget(tmp_path / "target")
    checked = check_save(
        pair_path=path, material_root=tmp_path / "materials", target=target
    )
    approved_save(
        pair_path=path,
        material_root=tmp_path / "materials",
        target=target,
        approval=authorize(checked.binding),
    )
    assert (
        target.read_current()[0].metadata.annotation.original_annotator
        == "synthetic-human"
    )
    assert old.read_bytes() == before


def test_known_differences_require_exact_pending_acceptance(tmp_path):
    raw = draft(tmp_path)
    raw["material_correspondence"]["differences"] = [
        "Synthetic PDF appendix absent from source."
    ]
    args = flow(tmp_path, raw=raw)
    assert "material_differences" in args["approval"].accepted_pending
    with pytest.raises(GoldApprovalError, match="science_pending"):
        approved_save(
            **dict(
                args,
                approval=args["approval"].model_copy(update={"accepted_pending": []}),
            )
        )
    approved_save(**args)
