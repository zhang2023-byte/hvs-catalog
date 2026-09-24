"""Synthetic checks for the scoring-facing two-file Gold contract."""

from __future__ import annotations

import json

import pytest
from evaluation_helpers import (
    build_approval_payload,
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    CompactGoldAnswer,
    CompactGoldMetadata,
    CompactGoldPair,
    GoldApprovalError,
    GoldFormatError,
    GoldIntegrityError,
    GoldTarget,
    SaveApproval,
    approved_save,
    check_compact_gold_pair,
    compact_gold_annotation,
    compare_legacy_with_current,
    parse_gold_annotation,
    prepare_save,
    read_compact_gold_pair,
    write_compact_gold_pair,
)
from hvs_catalog.evaluation.generate import build_outputs


def _pair(tmp_path, *, scope="complete"):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = parse_gold_annotation(build_fresh_draft(pdf_sha256=digest))
    limitations = (
        [
            {
                "subject": "Table 3",
                "value": "One table was not covered by this reference answer.",
            }
        ]
        if scope == "partial"
        else []
    )
    pair = compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review={
            "value": "checked",
            "basis": "Synthetic fixed PDF and source correspondence",
            "record_sha256": "a" * 64,
        },
        scope_review={
            "value": scope,
            "basis": "Synthetic explicit scope review",
            "record_sha256": "a" * 64,
        },
        scope_limitations=limitations,
    )
    return material, draft, pair


def test_compact_conversion_preserves_science_and_orders_fields(tmp_path):
    material, draft, pair = _pair(tmp_path)
    answer_path, metadata_path = write_compact_gold_pair(pair, tmp_path / "pair")
    loaded = read_compact_gold_pair(answer_path)
    assert loaded.answer == pair.answer
    assert loaded.metadata == pair.metadata
    assert pair.contribution_id_map == {"c1": "2501.00001:cand01"}
    answer_text = answer_path.read_text(encoding="utf-8")
    assert answer_text.index('"value": "612.0"') < answer_text.index(
        '"evidence": [', answer_text.index('"value": "612.0"')
    )
    assert '"legacy_index"' not in answer_text
    assert '"legacy_status"' not in answer_text
    assert '"document_id"' not in answer_text
    assert '"kind"' not in answer_text
    assert '"pending"' not in answer_text
    assert '"provenance"' not in answer_text
    assert '"error": null' not in answer_text
    assert '"limit_kind": "none"' not in answer_text
    assert '"quote": ""' not in answer_text
    metadata_text = metadata_path.read_text(encoding="utf-8")
    assert '"legacy"' not in metadata_text
    assert '"paper.pdf"' in metadata_text
    old_value = draft.contributions[0].quantities[0].values[0]
    new_value = loaded.answer.contributions[0].quantities[0].values[0]
    assert new_value.model_dump(mode="json") == old_value.model_dump(mode="json")
    assert check_compact_gold_pair(loaded, material_root=material).blocking_count == 0


def test_compact_save_requires_exact_human_science_review_and_two_files(tmp_path):
    material, _, pair = _pair(tmp_path)
    answer_path, _ = write_compact_gold_pair(pair, tmp_path / "pair")
    target = GoldTarget(tmp_path / "target")
    preparation = prepare_save(
        draft_path=answer_path,
        material_root=material,
        work_dir=tmp_path / "work",
        target=target,
    )
    assert preparation.manifest.format_version == 2
    assert preparation.manifest.metadata_sha256
    approval = build_approval_payload(preparation)
    with pytest.raises(GoldApprovalError, match="scientific_review"):
        approved_save(
            draft_path=answer_path,
            material_root=material,
            target=target,
            preparation_dir=preparation.directory,
            approval=SaveApproval.model_validate(approval),
            operation_id="op-compact",
        )
    approval["scientific_review"] = True
    receipt = approved_save(
        draft_path=answer_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=SaveApproval.model_validate(approval),
        operation_id="op-compact",
    )
    assert receipt.version == 1
    assert {item.path.rsplit("/", 1)[-1] for item in receipt.files} == {
        "answer.json",
        "metadata.json",
    }
    assert not (target.root / "annotations/gold-2501.00001/v1/provenance.json").exists()
    assert not (target.root / "annotations/gold-2501.00001/v1/approval.json").exists()
    assert (target.root / "operations/op-compact.approval.json").is_file()
    entry, answer = target.read_current_answer("gold-2501.00001")
    assert entry.version == 1
    assert answer.contributions[0].contribution_id == "2501.00001:cand01"
    replay = approved_save(
        draft_path=answer_path,
        material_root=material,
        target=target,
        preparation_dir=preparation.directory,
        approval=SaveApproval.model_validate(approval),
        operation_id="op-compact",
    )
    assert replay.replayed
    (target.root / "operations/op-compact.approval.json").unlink()
    with pytest.raises(GoldIntegrityError, match="approval"):
        target.read_current_answer("gold-2501.00001")


def test_partial_zero_contributions_stays_distinct_from_complete_zero(tmp_path):
    material, draft, _ = _pair(tmp_path)
    raw = draft.model_dump(mode="json", by_alias=True)
    raw["status"] = "no_contributions"
    raw["contributions"] = []
    draft = parse_gold_annotation(raw)
    pair = compact_gold_annotation(
        draft,
        title="Synthetic partial paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review={
            "value": "checked",
            "basis": "Synthetic review",
            "record_sha256": "b" * 64,
        },
        scope_review={
            "value": "partial",
            "basis": "Synthetic partial review",
            "record_sha256": "b" * 64,
        },
        scope_limitations=[
            {"subject": "Supplement", "value": "The supplement was unavailable."}
        ],
    )
    answer_path, _ = write_compact_gold_pair(pair, tmp_path / "partial")
    payload = json.loads(answer_path.read_text(encoding="utf-8"))
    assert payload["contributions"] == []
    assert payload["coverage"]["value"] == "partial"
    assert payload["coverage"]["limitations"]
    assert (
        "partial_coverage"
        in check_compact_gold_pair(pair, material_root=material).pending_ids()
    )
    with pytest.raises(GoldFormatError, match="named limitations"):
        compact_gold_annotation(
            draft,
            title="Synthetic partial paper",
            original_annotator="synthetic-expert",
            reviewer="synthetic-reviewer",
            material_review={
                "value": "checked",
                "basis": "Synthetic review",
                "record_sha256": "b" * 64,
            },
            scope_review={
                "value": "partial",
                "basis": "Synthetic partial review",
                "record_sha256": "b" * 64,
            },
        )


def test_v2_compare_requires_explicit_v4_mapping(tmp_path):
    _, _, pair = _pair(tmp_path)
    answer_path, _ = write_compact_gold_pair(pair, tmp_path / "pair")
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(json.dumps(build_legacy_v4_doc()), encoding="utf-8")
    with pytest.raises(GoldFormatError, match="explicit --mapping"):
        compare_legacy_with_current(legacy_path=legacy_path, current_path=answer_path)
    mapping_path = tmp_path / "legacy-map.json"
    mapping_path.write_text(
        json.dumps({"legacy_index_by_contribution_id": {"2501.00001:cand01": 0}}),
        encoding="utf-8",
    )
    report = compare_legacy_with_current(
        legacy_path=legacy_path,
        current_path=answer_path,
        mapping_path=mapping_path,
    )
    assert report.mapping_sha256
    assert report.unmatched_old == []
    assert report.unmatched_new == []
    assert any("radial_velocity" in item.subject for item in report.items)
    mapping_path.write_text(
        json.dumps({"legacy_index_by_contribution_id": {"2501.00001:cand01": 1}}),
        encoding="utf-8",
    )
    with pytest.raises(GoldFormatError, match="invalid v4 index"):
        compare_legacy_with_current(
            legacy_path=legacy_path,
            current_path=answer_path,
            mapping_path=mapping_path,
        )


def test_compact_conflict_cannot_confirm_the_same_pdf_evidence(tmp_path):
    _, _, pair = _pair(tmp_path)
    data = pair.answer.model_dump(mode="json")
    direct = data["contributions"][0]["quantities"][0]["values"][0]["evidence"][0]
    other = dict(direct, location="PDF p. 2, conflicting row")
    data["contributions"][0]["quantity_conflicts"] = [
        {
            "conflict_id": "k1",
            "quantity": "observed_phase_space.radial_velocity",
            "reason": {"value": "Synthetic conflict", "evidence": [direct, other]},
            "alternatives": [],
        }
    ]
    with pytest.raises(ValueError, match="待决证据"):
        CompactGoldAnswer.model_validate(data)


def test_compact_exclusion_reason_keeps_its_pdf_evidence(tmp_path):
    material, draft, _ = _pair(tmp_path)
    raw = draft.model_dump(mode="json", by_alias=True)
    raw["reviewed_exclusions"] = [
        {
            "exclusion_id": "x1",
            "reason": "Synthetic object is outside the selected scope.",
            "evidence": [
                {"file_id": "pdf-1", "location": "PDF p. 3, exclusion note", "page": 3}
            ],
        }
    ]
    draft = parse_gold_annotation(raw)
    pair = compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review={
            "value": "checked",
            "basis": "Synthetic review",
            "record_sha256": "c" * 64,
        },
        scope_review={
            "value": "complete",
            "basis": "Synthetic review",
            "record_sha256": "c" * 64,
        },
    )
    path, _ = write_compact_gold_pair(pair, tmp_path / "excluded")
    exclusion = json.loads(path.read_text(encoding="utf-8"))["reviewed_exclusions"][0]
    assert exclusion["reason"]["value"] == raw["reviewed_exclusions"][0]["reason"]
    assert exclusion["reason"]["evidence"][0]["location"] == "PDF p. 3, exclusion note"
    assert check_compact_gold_pair(pair, material_root=material).blocking_count == 0


def test_generated_compact_pair_parses_and_round_trips(tmp_path):
    outputs = build_outputs()
    answer = CompactGoldAnswer.model_validate(
        json.loads(outputs["templates/gold-answer.json"])
    )
    metadata = CompactGoldMetadata.model_validate(
        json.loads(outputs["templates/gold-metadata.json"])
    )
    pair = CompactGoldPair(answer=answer, metadata=metadata)
    path, _ = write_compact_gold_pair(pair, tmp_path / "template")
    assert read_compact_gold_pair(path).answer == answer
