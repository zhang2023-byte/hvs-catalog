"""Read-only item comparisons of legacy input against drafts/new answers, including content
hashes.
"""

from __future__ import annotations

import json

from evaluation_helpers import build_legacy_v4_doc, write_synthetic_pdf

from hvs_catalog.evaluation import (
    compare_legacy_with_current,
    migrate_legacy_annotation,
)


def _setup(tmp_path, document=None):
    document = document or build_legacy_v4_doc()
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return legacy


def test_migrated_draft_compares_identical(tmp_path):
    """After verbatim migration, values, evidence locations, and quotes compare
    identically.
    """
    legacy = _setup(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    report = compare_legacy_with_current(
        legacy_path=legacy, current_path=result.draft_path
    )
    assert report.difference_count == 0
    assert report.matched_count > 0
    assert any(
        "radial_velocity" in item.subject and item.status == "identical"
        for item in report.items
    )
    assert report.legacy_sha256 and len(report.legacy_sha256) == 64
    assert report.current_sha256 and len(report.current_sha256) == 64


def test_changed_value_reported_as_difference(tmp_path):
    legacy = _setup(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"][0]["values"][0]["value"] = "613.0"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    assert report.difference_count > 0
    item = next(item for item in report.items if "radial_velocity" in item.subject)
    assert item.status == "different"
    # Comparison only reports and modifies neither side.
    assert (
        json.loads(legacy.read_text())["contributions"][0]["quantities"][0]["values"][
            0
        ]["value"]
        == "612.0"
    )


def test_added_quantity_and_unmatched_new_entries(tmp_path):
    legacy = _setup(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"].append(
        {
            "quantity": "observed_phase_space.distance",
            "values": [
                {
                    "value": "60.0",
                    "error": None,
                    "lower_error": None,
                    "upper_error": None,
                    "unit": "kpc",
                    "limit_kind": "none",
                    "range_lower": None,
                    "range_upper": None,
                    "coordinate_format": None,
                    "condition": "photometric",
                    "interval_kind": None,
                    "coverage": None,
                    "lower_quantile": None,
                    "upper_quantile": None,
                    "error_components": [],
                    "representation": None,
                    "source": "this_paper",
                    "source_note": None,
                    "evidence": draft["contributions"][0]["quantities"][0]["values"][0][
                        "evidence"
                    ],
                    "context_evidence": [],
                }
            ],
        }
    )
    new_contribution = json.loads(json.dumps(draft["contributions"][0]))
    new_contribution["contribution_id"] = "c-extra"
    new_contribution["legacy_index"] = None
    new_contribution["identifiers"] = [
        {"value": "SYN-B", "evidence": new_contribution["identifiers"][0]["evidence"]}
    ]
    draft["contributions"].append(new_contribution)
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    assert any(item.status == "added" for item in report.items)
    assert report.unmatched_new == ["c-extra"]
    assert report.unmatched_old == []


def test_removed_quantity_reported(tmp_path):
    legacy = _setup(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["quantities"] = []
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    assert any(item.status == "removed" for item in report.items)


def test_fresh_draft_without_legacy_mapping_reports_unmatched(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    from evaluation_helpers import build_fresh_draft

    draft = build_fresh_draft(pdf_sha256=digest)
    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    legacy = _setup(tmp_path)
    report = compare_legacy_with_current(legacy_path=legacy, current_path=fresh)
    # A fresh draft has no legacy counterpart: all legacy entries are unmatched.
    assert report.unmatched_old == ["contributions[0]"]
    assert report.unmatched_new == ["c1"]
