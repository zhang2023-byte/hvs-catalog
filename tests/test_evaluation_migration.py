"""Synthetic migration scenarios 1-5: verbatim drafts, pending checks, and read-only legacy
input.
"""

from __future__ import annotations

import json
import os

import pytest
from evaluation_helpers import (
    build_legacy_contribution,
    build_legacy_range_value,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    migrate_legacy_annotation,
    read_legacy_annotation,
)


def _write_legacy(tmp_path, document, name="legacy.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def test_scenario_1_value_strings_preserved_verbatim(tmp_path):
    """Preserve SYN-A's legacy value 612.0, error 8.0, and unit km/s verbatim."""
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    (group,) = contribution.quantities
    (value,) = group.values
    assert (value.value, value.error, value.unit) == ("612.0", "8.0", "km/s")
    assert value.value != 612  # Do not convert to a number.
    # Continue reporting evidence gaps when known materials were not supplied.
    check = result.check
    assert check is not None
    assert check.problems_of("material")
    assert any(item.code == "evidence_unbound" for item in check.problems)


def test_scenario_2_not_assessed_preserved_with_review_note(tmp_path):
    legacy = _write_legacy(
        tmp_path,
        build_legacy_v4_doc(
            contributions=[
                build_legacy_contribution(
                    boundness_status="not_assessed", contribution_type="follow_up"
                )
            ]
        ),
    )
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    assert (
        contribution.paper_boundness.status is None
    )  # Do not automatically approve a new status.
    assert contribution.paper_boundness.legacy_status == "not_assessed"
    pending_ids = [item.item_id for item in result.draft.pending]
    assert any("bound" in item for item in pending_ids)
    assert any(
        item.code == "legacy_not_assessed"
        for item in result.check.problems_of("science_pending")
    )
    review_items = [
        item for item in result.report.items if item.category == "science_review"
    ]
    assert any("not_assessed" in item.description for item in review_items)
    # The program does not infer new statuses.
    assert "possibly_bound" not in contribution.paper_boundness.model_dump_json()


def test_scenario_3_location_preserved_quote_not_fabricated(tmp_path):
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    (value,) = contribution.quantities[0].values
    (evidence,) = value.evidence
    assert evidence.location == "Table 2, row SYN-A"
    assert (
        evidence.quote == ""
    )  # Leave missing quotes empty rather than inventing them.
    assert evidence.page is None  # Do not guess page ordinals with regular expressions.
    assert evidence.file_id is None  # Do not bind materials that were not supplied.


def test_scenario_4_no_contributions_conclusion_kept(tmp_path):
    legacy = _write_legacy(
        tmp_path, build_legacy_v4_doc(contributions=[], status="no_contributions")
    )
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    assert result.draft.status == "no_contributions"
    assert result.draft.contributions == []
    assert any(
        item.code == "no_contributions_conclusion"
        for item in result.check.problems_of("science_pending")
    )
    # Do not fabricate claims such as a complete empty new roster.
    serialized = result.draft.model_dump_json()
    assert "complete" not in serialized or "名单完整" not in serialized


def test_scenario_5_conflict_kept_without_fabricated_values(tmp_path):
    conflict = {
        "quantity": "observed_phase_space.distance",
        "reason": "two printed distances remain unreconciled",
        "evidence": [
            {"location": "Table 3", "quote": ""},
            {"location": "Section 4.2", "quote": ""},
        ],
    }
    legacy = _write_legacy(
        tmp_path,
        build_legacy_v4_doc(
            contributions=[build_legacy_contribution(conflicts=[conflict])]
        ),
    )
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    (kept,) = contribution.quantity_conflicts
    assert kept.conflict_id == "k1"
    assert kept.quantity == "observed_phase_space.distance"
    assert kept.reason == conflict["reason"]
    assert [e.location for e in kept.evidence] == ["Table 3", "Section 4.2"]
    assert kept.alternatives == []  # Do not guess two sets of values.
    assert any(
        item.code == "conflict_structure_pending"
        for item in result.check.problems_of("science_pending")
    )
    assert any(
        item.item_id == "p-conflict-k1"
        and item.subject == "contributions/c1/quantity_conflicts/k1"
        for item in result.draft.pending
    )
    assert any(
        item.old_path == "contributions[0]/quantity_conflicts[0]"
        and item.new_path == "contributions/c1/quantity_conflicts/k1"
        for item in result.report.items
    )


def test_conflict_ids_unique_across_contributions_with_consistent_links(tmp_path):
    def conflict(quantity, label):
        return {
            "quantity": quantity,
            "reason": label,
            "evidence": [
                {"location": f"{label} Table", "quote": ""},
                {"location": f"{label} prose", "quote": ""},
            ],
        }

    contributions = [
        build_legacy_contribution(
            name="SYN-A",
            conflicts=[
                conflict("observed_phase_space.distance", "synthetic A distance"),
                conflict("observed_phase_space.parallax", "synthetic A parallax"),
            ],
        ),
        build_legacy_contribution(name="SYN-B"),
        build_legacy_contribution(
            name="SYN-C",
            conflicts=[
                conflict("observed_phase_space.distance", "synthetic C distance")
            ],
        ),
    ]
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc(contributions=contributions))
    original = legacy.read_bytes()
    result = migrate_legacy_annotation(legacy_path=legacy, draft_dir=tmp_path / "draft")
    assert legacy.read_bytes() == original

    mapped = [
        (contribution.contribution_id, item)
        for contribution in result.draft.contributions
        for item in contribution.quantity_conflicts
    ]
    assert [item.conflict_id for _, item in mapped] == ["k1", "k2", "k3"]
    expected_paths = [
        f"contributions/{owner}/quantity_conflicts/{item.conflict_id}"
        for owner, item in mapped
    ]
    assert expected_paths == [
        "contributions/c1/quantity_conflicts/k1",
        "contributions/c1/quantity_conflicts/k2",
        "contributions/c3/quantity_conflicts/k3",
    ]
    conflict_pending = [
        item for item in result.draft.pending if "/quantity_conflicts/" in item.subject
    ]
    assert [item.item_id for item in conflict_pending] == [
        "p-conflict-k1",
        "p-conflict-k2",
        "p-conflict-k3",
    ]
    assert [item.subject for item in conflict_pending] == expected_paths
    report_conflicts = [
        item
        for item in result.report.items
        if "/quantity_conflicts/" in (item.new_path or "")
    ]
    assert [item.new_path for item in report_conflicts] == expected_paths
    assert [item.old_path for item in report_conflicts] == [
        "contributions[0]/quantity_conflicts[0]",
        "contributions[0]/quantity_conflicts[1]",
        "contributions[2]/quantity_conflicts[0]",
    ]

    original_conflicts = [
        item
        for contribution in contributions
        for item in contribution["quantity_conflicts"]
    ]
    for (_, current), old in zip(mapped, original_conflicts, strict=True):
        assert current.quantity == old["quantity"]
        assert current.reason == old["reason"]
        assert [e.location for e in current.evidence] == [
            e["location"] for e in old["evidence"]
        ]
        assert current.alternatives == []


def test_exclusions_preserved_as_pending_mapping(tmp_path):
    document = build_legacy_v4_doc(
        reviewed_exclusions=[
            {
                "reason": "object not a hypervelocity candidate",
                "evidence": [{"location": "Appendix A", "quote": ""}],
            }
        ]
    )
    legacy = _write_legacy(tmp_path, document)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (exclusion,) = result.draft.reviewed_exclusions
    assert exclusion.reason == "object not a hypervelocity candidate"
    assert exclusion.evidence[0].location == "Appendix A"
    assert any(
        item.code == "exclusions_pending_mapping"
        for item in result.check.problems_of("science_pending")
    )


def test_interval_metadata_preserved(tmp_path):
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantities"].append(
        {
            "quantity": "observed_phase_space.distance",
            "values": [build_legacy_range_value()],
        }
    )
    legacy = _write_legacy(tmp_path, document)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    (group,) = [
        g
        for g in contribution.quantities
        if g.quantity == "observed_phase_space.distance"
    ]
    (value,) = group.values
    assert value.limit_kind == "range"
    assert (value.range_lower, value.range_upper) == ("55.0", "75.0")
    assert value.interval_kind == "confidence"
    assert value.coverage.value == "90"
    assert value.coverage.unit == "%"
    assert (
        value.value is None
    )  # Convert legacy empty strings to the new null representation.


def test_migration_binds_known_pdf_and_is_reproducible(tmp_path):
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    first = migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=tmp_path / "draft1",
        known_pdfs=[material / "paper.pdf"],
        material_root=material,
    )
    second = migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=tmp_path / "draft2",
        known_pdfs=[material / "paper.pdf"],
        material_root=material,
    )
    assert first.draft.files[0].sha256 == digest  # Hash the actual file.
    (value,) = first.draft.contributions[0].quantities[0].values
    assert value.evidence[0].file_id == "pdf-1"
    # Technical identifiers and draft contents are reproducible.
    assert first.draft.model_dump(mode="json") == second.draft.model_dump(mode="json")
    assert first.report.model_dump(mode="json") == second.report.model_dump(mode="json")
    # Binding known materials resolves unbound-evidence issues.
    assert not any(item.code == "evidence_unbound" for item in first.check.problems)
    assert first.report.canary_verified


def test_legacy_input_read_only(tmp_path):
    document = build_legacy_v4_doc()
    legacy = _write_legacy(tmp_path, document)
    stat_before = legacy.stat()
    bytes_before = legacy.read_bytes()
    migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=tmp_path / "draft",
        known_pdfs=None,
        material_root=None,
    )
    assert legacy.read_bytes() == bytes_before
    assert legacy.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert legacy.stat().st_mode == stat_before.st_mode


def test_report_covers_every_legacy_field(tmp_path):
    document = build_legacy_v4_doc(
        reviewed_exclusions=[
            {
                "reason": "not a candidate",
                "evidence": [{"location": "Appendix A", "quote": ""}],
            }
        ]
    )
    legacy = _write_legacy(tmp_path, document)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    covered = {item.old_path for item in result.report.items}
    for required in (
        "arxiv_id/annotator/annotated_at/guideline_version/annotation_process",
        "canary",
        "status",
        "contributions[0]",
        "contributions[0]/identifiers",
        "contributions[0]/paper_boundness/status",
        "contributions[0]/quantities[0]",
        "reviewed_exclusions",
    ):
        assert required in covered, required
    assert set(result.report.summary) == {
        "preserved",
        "format_adjusted",
        "missing_info",
        "science_review",
    }


def test_migration_with_corrupt_canary_still_reads_and_reports(tmp_path):
    document = build_legacy_v4_doc()
    document["canary"] = document["canary"][:-4] + "0000"
    legacy = _write_legacy(tmp_path, document)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    assert not result.report.canary_verified  # Report verification failure accurately.
    read_legacy_annotation(
        legacy
    )  # A canary mismatch does not by itself prevent reading.


def test_draft_written_as_parsable_gold_document(tmp_path):
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    assert result.draft_path.is_file()
    assert result.report_path.is_file()
    from hvs_catalog.evaluation import read_gold_annotation

    loaded = read_gold_annotation(result.draft_path)
    assert loaded.kind == "draft"
    assert loaded.provenance.origin == "migration"
    assert loaded.provenance.legacy.annotator == "synthetic-annotator"
    assert loaded.contributions[0].legacy_index == 0


@pytest.mark.skipif(os.name != "posix", reason="仅 POSIX 平台验证权限")
def test_migration_does_not_write_legacy_directory(tmp_path):
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    before = sorted(p.name for p in tmp_path.iterdir())
    migrate_legacy_annotation(legacy_path=legacy, draft_dir=tmp_path / "draft")
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == ["legacy.json"]
    assert after == ["draft", "legacy.json"]
