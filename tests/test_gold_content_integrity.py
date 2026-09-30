"""Gold content integrity: extra contributions, nested evidence and material gaps."""

import copy
import json

import pytest
from evaluation_helpers import (
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)
from gold_save_helpers import approve_revision, edit_pair, flow, revise, snapshot
from reportlab.pdfgen.canvas import Canvas

from hvs_catalog.evaluation import (
    GoldCheckError,
    GoldIntegrityError,
    approved_save,
    check_compact_gold_pair,
    compare_legacy_with_current,
    migrate_legacy_annotation,
    read_compact_gold_pair,
)
from hvs_catalog.evaluation.pdfcheck import analyze_pdf, quote_result


def write(path, data):
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("index", [None, 0, 1, 99])
@pytest.mark.parametrize("insert_first", [False, True])
def test_every_extra_contribution_is_reported(tmp_path, index, insert_first):
    old = write(tmp_path / "old.json", build_legacy_v4_doc())
    migrated = migrate_legacy_annotation(
        legacy_path=old, draft_dir=tmp_path / "migration"
    )
    data = migrated.draft.model_dump(mode="json", by_alias=True)
    extra = copy.deepcopy(data["contributions"][0])
    extra.update(contribution_id="extra", legacy_index=index)
    extra["identifiers"][0]["value"] = "EXTRA-OBJECT"
    extra["quantities"][0]["values"][0]["value"] = "999.99"
    data["contributions"].insert(0 if insert_first else 1, extra)
    new = write(tmp_path / "new.json", data)
    before = old.read_bytes(), new.read_bytes()
    report = compare_legacy_with_current(legacy_path=old, current_path=new)
    assert "extra" in report.unmatched_new or any(
        "extra" in item.subject and item.status != "identical" for item in report.items
    )
    assert before == (old.read_bytes(), new.read_bytes())


def inject_evidence(data, location, evidence):
    contribution = data["contributions"][0]
    value = contribution["quantities"][0]["values"][0]
    if location == "exclusion":
        data["reviewed_exclusions"] = [
            {
                "exclusion_id": "x1",
                "reason": {"value": "Synthetic exclusion", "evidence": [evidence]},
            }
        ]
    elif location == "identifier":
        contribution["identifiers"][0]["evidence"] = [evidence]
    else:
        alternative = copy.deepcopy(value)
        if location == "context":
            alternative["context_evidence"] = [evidence]
        elif location == "component":
            alternative["error_components"] = [
                {"name": "statistical", "error": "1.0", "evidence": [evidence]}
            ]
        else:
            alternative.update(
                value=None,
                error=None,
                limit_kind="range",
                range_lower="600.0",
                range_upper="700.0",
                interval_kind="credible",
            )
            alternative["coverage"] = {
                "value": "0.68",
                "unit": "fraction",
                "evidence": [evidence],
            }
        second = copy.deepcopy(alternative)
        second["range_upper" if location == "coverage" else "value"] = "702.20"
        contribution["quantities"] = []
        contribution["quantity_conflicts"] = [
            {
                "conflict_id": "k1",
                "quantity": "observed_phase_space.radial_velocity",
                "reason": {
                    "value": "Synthetic contradiction",
                    "evidence": [
                        value["evidence"][0],
                        dict(value["evidence"][0], location="Section S"),
                    ],
                },
                "alternatives": [alternative, second],
            }
        ]


@pytest.mark.parametrize(
    "location", ["exclusion", "identifier", "context", "component", "coverage"]
)
@pytest.mark.parametrize("defect", ["page", "unbound", "unknown", "valid"])
def test_all_nested_evidence_paths_block_invalid_save(tmp_path, location, defect):
    args = flow(tmp_path)
    approved_save(**args)
    before = snapshot(args["target"].root)
    revised = revise(args)
    answer = read_compact_gold_pair(revised["pair_path"]).answer.model_dump(mode="json")
    evidence = {
        "file_id": "pdf-1",
        "location": "Synthetic locator",
        "page": 2,
        "quote": "SYN-A",
    }
    if defect == "page":
        evidence["page"] = 999
    elif defect == "unbound":
        evidence["file_id"] = None
    elif defect == "unknown":
        evidence["file_id"] = "unknown"
    inject_evidence(answer, location, evidence)
    if defect in {"unknown", "unbound"}:
        with pytest.raises(GoldIntegrityError, match="unknown PDF"):
            approve_revision(edit_pair(revised, answer=answer))
    elif defect != "valid":
        changed = edit_pair(revised, answer=answer)
        report = check_compact_gold_pair(
            read_compact_gold_pair(changed["pair_path"]),
            material_root=args["material_root"],
        )
        assert report.blocking_count > 0
        assert any(
            p.code == ("page_out_of_range" if defect == "page" else "evidence_unbound")
            for p in report.problems
        )
        with pytest.raises(GoldCheckError):
            approve_revision(changed)
    else:
        changed = edit_pair(revised, answer=answer)
        report = check_compact_gold_pair(
            read_compact_gold_pair(changed["pair_path"]),
            material_root=args["material_root"],
        )
        assert report.blocking_count == 0
        assert any(
            q.quote == evidence["quote"] and q.result == "matched"
            for q in report.quotes
        )
        approved_save(**approve_revision(changed))
        return
    assert snapshot(args["target"].root) == before


@pytest.mark.parametrize("category", ["missing_info", "structural_gap"])
def test_old_draft_gaps_do_not_become_accepted_scientific_uncertainty(
    tmp_path, category
):
    digest = write_synthetic_pdf(tmp_path / "paper.pdf")
    raw = build_fresh_draft(pdf_sha256=digest)
    raw["pending"] = [
        {
            "item_id": "p1",
            "category": category,
            "subject": "SYN-A",
            "description": "Missing synthetic information",
        }
    ]
    from hvs_catalog.evaluation import check_gold_annotation, parse_gold_annotation

    assert (
        check_gold_annotation(
            parse_gold_annotation(raw), material_root=tmp_path
        ).blocking_count
        > 0
    )


def test_mixed_pdf_reports_unextractable_scope(tmp_path):
    pdf = tmp_path / "mixed.pdf"
    canvas = Canvas(str(pdf))
    canvas.drawString(30, 700, "Synthetic text")
    canvas.showPage()
    canvas.rect(10, 10, 100, 100)
    canvas.showPage()
    canvas.save()
    result = analyze_pdf(pdf.read_bytes())
    assert result.openable and result.page_count == 2
    assert (
        quote_result(result.page_texts, page=2, quote="quote")[0] == "not_extractable"
    )
    assert (
        quote_result(result.page_texts, page=None, quote="quote")[0]
        == "not_extractable"
    )
    assert quote_result(result.page_texts, page=1, quote="quote")[0] == "not_found"
    assert (
        quote_result(result.page_texts, page=None, quote="Synthetic text")[0]
        == "matched"
    )
