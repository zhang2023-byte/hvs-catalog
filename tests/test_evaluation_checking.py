"""Checking behavior: PDF files/locators, evidence gaps, pending science review, and
auxiliary quote checks (scenarios 6, 9, 10).
"""

from __future__ import annotations

import json

import pytest
from evaluation_helpers import (
    build_fresh_draft,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    GoldFormatError,
    check_gold_annotation,
    parse_gold_annotation,
)


def _draft_file(tmp_path, draft):
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


def _codes(report, category):
    return sorted(item.code for item in report.problems_of(category))


def test_fresh_draft_with_complete_pdf_passes(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0
    assert report.problems_of("science_pending") == []
    assert report.files[0].page_count == 3
    quotes = [q.result for q in report.quotes]
    assert "matched" in quotes  # Auxiliary quote check matches.


def test_scenario_10_empty_quote_alone_is_not_an_error(tmp_path):
    """With an explicit PDF location but no quote, retain the location and known validation
    results without a separate error.
    """
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest, include_quote=False)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0
    assert all(q.result == "skipped" for q in report.quotes)
    assert any("空引文" in q.note for q in report.quotes)


def test_scenario_10_unextractable_text_reported_not_passed(tmp_path):
    """Report unreliable page-text extraction accurately without claiming automatic
    semantic verification.
    """
    material = tmp_path / "materials"
    # Build a synthetic PDF with no text layer or display text on any page.
    write_synthetic_pdf(material / "paper.pdf", pages=[[""], [""], [""]])
    import hashlib

    digest = hashlib.sha256((material / "paper.pdf").read_bytes()).hexdigest()
    draft = build_fresh_draft(pdf_sha256=digest, include_quote=True)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0  # Auxiliary quote checking is not an error.
    results = {q.result for q in report.quotes}
    assert "not_extractable" in results
    assert any("未自动核验" in q.note for q in report.quotes)


def test_scenario_9_pdf_replaced_hash_mismatch(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    # Replace the PDF after saving.
    write_synthetic_pdf(
        material / "paper.pdf",
        pages=[["different content"], ["x"], ["y"]],
    )
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "file_hash_mismatch" in _codes(report, "material")


def test_scenario_9_page_ordinal_out_of_range(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")  # Three pages.
    draft = build_fresh_draft(pdf_sha256=digest, page=9)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "page_out_of_range" in _codes(report, "material")


def test_missing_material_file(tmp_path):
    draft = build_fresh_draft(pdf_sha256="a" * 64)
    report = check_gold_annotation(
        parse_gold_annotation(draft), material_root=tmp_path / "materials"
    )
    assert "file_missing" in _codes(report, "material")


def test_file_hash_pending_blocks_formal(tmp_path):
    material = tmp_path / "materials"
    write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=None)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "file_hash_pending" in _codes(report, "material")


def test_not_a_pdf_file(tmp_path):
    material = tmp_path / "materials"
    material.mkdir()
    (material / "paper.pdf").write_bytes(b"plain text, not a pdf" * 10)
    import hashlib

    digest = hashlib.sha256((material / "paper.pdf").read_bytes()).hexdigest()
    draft = build_fresh_draft(pdf_sha256=digest)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "file_not_pdf" in _codes(report, "material")


def test_truncated_pdf_detected(tmp_path):
    material = tmp_path / "materials"
    data = write_synthetic_pdf(material / "paper.pdf")

    full = (material / "paper.pdf").read_bytes()
    (material / "paper.pdf").write_bytes(full[: len(full) // 2])
    draft = build_fresh_draft(pdf_sha256=data)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    codes = _codes(report, "material")
    assert "file_hash_mismatch" in codes


def test_missing_required_evidence_reported(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft["contributions"][0]["quantities"][0]["values"][0]["evidence"] = []
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "missing_value_evidence" in _codes(report, "material")


def test_missing_condition_reported(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft["contributions"][0]["quantities"][0]["values"][0]["condition"] = None
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "missing_condition" in _codes(report, "material")


def test_unknown_quantity_path_rejected(tmp_path):
    draft = build_fresh_draft()
    draft["contributions"][0]["quantities"][0]["quantity"] = "made_up.quantity"
    with pytest.raises(GoldFormatError, match="未知的物理量编码"):
        parse_gold_annotation(draft)


def test_unknown_author_status_rejected(tmp_path):
    draft = build_fresh_draft()
    draft["contributions"][0]["paper_boundness"]["status"] = "possibly_boundish"
    with pytest.raises(GoldFormatError, match="未知的作者状态"):
        parse_gold_annotation(draft)


def test_evidence_referencing_unknown_file_rejected(tmp_path):
    draft = build_fresh_draft()
    draft["files"] = []
    with pytest.raises(GoldFormatError, match="未登记的材料文件"):
        parse_gold_annotation(draft)


def test_file_name_traversal_rejected(tmp_path):
    draft = build_fresh_draft()
    draft["files"][0]["name"] = "../escape.pdf"
    with pytest.raises(GoldFormatError, match="越界"):
        parse_gold_annotation(draft)


def test_material_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    write_synthetic_pdf(outside / "real.pdf")
    material = tmp_path / "materials"
    material.mkdir()
    (material / "paper.pdf").symlink_to(outside / "real.pdf")
    import hashlib

    digest = hashlib.sha256((outside / "real.pdf").read_bytes()).hexdigest()
    draft = build_fresh_draft(pdf_sha256=digest)
    from hvs_catalog.workspace.errors import WorkspaceIntegrityError

    with pytest.raises(WorkspaceIntegrityError):
        check_gold_annotation(parse_gold_annotation(draft), material_root=material)


def test_quote_not_found_is_auxiliary_only(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    draft["contributions"][0]["quantities"][0]["values"][0]["evidence"][0]["quote"] = (
        "this sentence does not exist"
    )
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert report.blocking_count == 0  # Auxiliary results do not block.
    assert any(q.result == "not_found" for q in report.quotes)


def test_scope_reports_unchecked_areas(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    draft = build_fresh_draft(pdf_sha256=digest)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    joined = "\n".join(report.scope)
    assert "引文匹配为辅助结果" in joined
    assert "未独立判定" in joined
    assert "固定 PDF 与 LaTeX 文件哈希" in joined
