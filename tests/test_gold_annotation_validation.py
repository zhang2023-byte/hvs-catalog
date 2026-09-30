"""Gold annotation validation: migration, science, PDF and comparison integrity.

All materials are synthetic. PDF tests use ReportLab and an independent
constructor, not fixtures produced by the parser under test.
"""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from io import BytesIO

import pytest
from evaluation_helpers import (
    build_fresh_draft,
    build_legacy_v4_doc,
    write_synthetic_pdf,
)

from hvs_catalog.evaluation import (
    GoldOutputConflictError,
    check_gold_annotation,
    compare_legacy_with_current,
    migrate_legacy_annotation,
    parse_gold_annotation,
)
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import DirectEvidencePart, ErrorComponent, MeasuredValue

# ---- 共用准备 ----


def _material(tmp_path, pages=None, name="paper.pdf"):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / name, pages=pages)
    return material, digest


def _write_draft(tmp_path, draft, name="draft.json"):
    path = tmp_path / name
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return path


# ---- 迁移输出与旧输入重合 ----


def test_migration_refuses_output_equal_to_legacy_input(tmp_path):
    """旧 v4 恰好放在输出目录的 draft.json：拒绝写入，旧输入逐字节不变。"""
    legacy = tmp_path / "legacy.json"
    payload = json.dumps(build_legacy_v4_doc(), ensure_ascii=False)
    legacy.write_text(payload, encoding="utf-8")
    draft_dir = tmp_path
    (draft_dir / "draft.json").write_text(payload, encoding="utf-8")
    before = (draft_dir / "draft.json").read_bytes()
    stat_before = (draft_dir / "draft.json").stat()
    with pytest.raises(GoldOutputConflictError, match="只读输入"):
        migrate_legacy_annotation(
            legacy_path=draft_dir / "draft.json", draft_dir=draft_dir
        )
    after = (draft_dir / "draft.json").read_bytes()
    assert after == before
    stat_after = (draft_dir / "draft.json").stat()
    assert (stat_after.st_mode, stat_after.st_mtime_ns) == (
        stat_before.st_mode,
        stat_before.st_mtime_ns,
    )


@pytest.mark.skipif(os.name != "posix", reason="硬链接与符号链接仅 POSIX 验证")
def test_migration_refuses_hardlink_and_symlink_aliases(tmp_path):
    legacy = tmp_path / "legacy.json"
    payload = json.dumps(build_legacy_v4_doc(), ensure_ascii=False)
    legacy.write_text(payload, encoding="utf-8")
    # 硬链接别名：draft.json 与旧输入同一 inode
    hard_dir = tmp_path / "hard"
    hard_dir.mkdir()
    os.link(legacy, hard_dir / "draft.json")
    with pytest.raises(GoldOutputConflictError, match="硬链接"):
        migrate_legacy_annotation(legacy_path=legacy, draft_dir=hard_dir)
    assert legacy.read_bytes() == payload.encode("utf-8")
    # 符号链接别名：draft.json 指向旧输入
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / "draft.json").symlink_to(legacy)
    with pytest.raises(GoldOutputConflictError, match="符号链接"):
        migrate_legacy_annotation(legacy_path=legacy, draft_dir=link_dir)
    assert legacy.read_bytes() == payload.encode("utf-8")


def test_migration_refuses_overwriting_different_existing_draft(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(build_legacy_v4_doc(), ensure_ascii=False), encoding="utf-8"
    )
    draft_dir = tmp_path / "draft"
    draft_dir.mkdir()
    existing = "不是迁移输出".encode()
    (draft_dir / "draft.json").write_bytes(existing)
    with pytest.raises(GoldOutputConflictError, match="内容不同"):
        migrate_legacy_annotation(legacy_path=legacy, draft_dir=draft_dir)
    assert (draft_dir / "draft.json").read_bytes() == existing  # 不静默截断


def test_migration_identical_existing_output_is_idempotent(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(build_legacy_v4_doc(), ensure_ascii=False), encoding="utf-8"
    )
    draft_dir = tmp_path / "draft"
    first = migrate_legacy_annotation(legacy_path=legacy, draft_dir=draft_dir)
    stat_before = first.draft_path.stat()
    second = migrate_legacy_annotation(legacy_path=legacy, draft_dir=draft_dir)
    assert second.draft_path.read_bytes() == first.draft_path.read_bytes()
    stat_after = second.draft_path.stat()
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns  # 重试不重写
    assert legacy.read_bytes() == legacy.read_bytes()


def test_migration_output_never_overlaps_known_pdf(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(build_legacy_v4_doc(), ensure_ascii=False), encoding="utf-8"
    )
    # 一份恰好叫 draft.json 的 PDF 材料与迁移草稿输出同名：拒绝写入
    work = tmp_path / "work"
    work.mkdir()
    pdf_bytes = write_synthetic_pdf(work / "draft.json")
    with pytest.raises(GoldOutputConflictError, match="只读输入"):
        migrate_legacy_annotation(
            legacy_path=legacy,
            draft_dir=work,
            known_pdfs=[work / "draft.json"],
        )
    assert hashlib.sha256((work / "draft.json").read_bytes()).hexdigest() == pdf_bytes


# ---- 草稿待办的完整去向 ----


def _draft_with_pending(tmp_path, category):
    material, digest = _material(tmp_path)
    draft = build_fresh_draft(pdf_sha256=digest)
    draft["pending"] = [
        {
            "item_id": "must-check-identity",
            "category": category,
            "subject": "contributions/c1/identifiers",
            "description": "identity must be checked by an expert",
        }
    ]
    return material, _write_draft(tmp_path, draft)


def test_science_review_pending_reported(tmp_path):
    material, draft_path = _draft_with_pending(tmp_path, "science_review")
    report = check_gold_annotation(
        parse_gold_annotation(draft_path), material_root=material
    )
    assert report.blocking_count == 0
    assert "must-check-identity" in report.pending_ids()


@pytest.mark.parametrize("category", ["missing_info", "structural_gap"])
def test_structural_and_missing_info_pending_block_validation(tmp_path, category):
    material, draft_path = _draft_with_pending(tmp_path, category)
    report = check_gold_annotation(
        parse_gold_annotation(draft_path), material_root=material
    )
    assert report.blocking_count == 1
    assert report.problems_of("structure")[0].code == f"pending_{category}"


# ---- Gold 与共同记录的科学规则一致 ----


def _draft_with_value(tmp_path, **overrides):
    material, digest = _material(tmp_path)
    draft = build_fresh_draft(pdf_sha256=digest)
    draft["contributions"][0]["quantities"][0]["values"][0].update(overrides)
    return material, draft


def _measured_value(**kwargs):
    """构造共同记录 MeasuredValue 的最小有效形态（证据片段与数值一致）。"""
    base = {
        "value_id": "v1",
        "value": "612.0",
        "error": None,
        "lower_error": None,
        "upper_error": None,
        "unit": "km/s",
        "limit_kind": "none",
        "range_lower": None,
        "range_upper": None,
        "lower_inclusive": None,
        "upper_inclusive": None,
        "interval_kind": None,
        "coverage": None,
        "lower_quantile": None,
        "upper_quantile": None,
        "error_components": [],
        "condition": "heliocentric",
        "frame": None,
        "epoch": None,
        "coordinate_format": None,
        "representation": None,
        "purpose": None,
        "source": "this_paper",
        "source_note": None,
        "author_usage": None,
        "direct_evidence": [],
        "context_evidence_refs": [],
    }
    base.update(kwargs)
    filled = {
        key
        for key in ("value", "error", "lower_error", "upper_error")
        if base[key] is not None
    }
    base["direct_evidence"] = [
        DirectEvidencePart(
            part=part,
            evidence_ref=RecordRef(record_id="rec/evidence", revision=1),
            raw_fragment=base[part],
        )
        for part in sorted(filled)
    ]
    return MeasuredValue.model_validate(base)


def test_one_sided_error_accepted_like_common_record(tmp_path):
    overrides = {
        "error": None,
        "lower_error": "5.0",
        "upper_error": None,
    }
    common = _measured_value(**overrides)
    assert common.lower_error == "5.0"  # 当前共同规则接受单侧误差
    _, draft = _draft_with_value(tmp_path, **overrides)
    annotation = parse_gold_annotation(draft)
    value = annotation.contributions[0].quantities[0].values[0]
    assert value.lower_error == "5.0" and value.upper_error is None


def test_total_error_with_components_accepted_like_common_record(tmp_path):
    overrides = {
        "error": "9.0",
        "error_components": [
            {
                "name": "statistical",
                "error": "5.0",
                "lower_error": None,
                "upper_error": None,
                "evidence": [
                    {
                        "file_id": "pdf-1",
                        "location": "Table 2 note",
                        "page": 2,
                        "quote": "",
                    }
                ],
            }
        ],
    }
    component = ErrorComponent.model_validate(
        {
            "name": "statistical",
            "error": "5.0",
            "lower_error": None,
            "upper_error": None,
            "direct_evidence": [
                DirectEvidencePart(
                    part="error",
                    evidence_ref=RecordRef(record_id="rec/evidence", revision=1),
                    raw_fragment="5.0",
                )
            ],
        }
    )
    common = _measured_value(error="9.0", error_components=[component])
    assert common.error == "9.0"  # 当前共同规则允许总误差与分量并存
    _, draft = _draft_with_value(tmp_path, **overrides)
    annotation = parse_gold_annotation(draft)
    value = annotation.contributions[0].quantities[0].values[0]
    assert value.error == "9.0"
    assert value.error_components[0].name == "statistical"


def test_component_one_sided_and_empty_shapes(tmp_path):
    _, draft = _draft_with_value(
        tmp_path,
        error=None,
        lower_error="5.0",
        upper_error=None,
        error_components=[
            {
                "name": "systematic",
                "error": None,
                "lower_error": "2.0",
                "upper_error": None,
                "evidence": [
                    {
                        "file_id": "pdf-1",
                        "location": "Section 3",
                        "page": 1,
                        "quote": "",
                    }
                ],
            }
        ],
    )
    annotation = parse_gold_annotation(draft)
    component = annotation.contributions[0].quantities[0].values[0].error_components[0]
    assert component.lower_error == "2.0"
    # 分量没有任何误差数值：拒绝
    _, empty = _draft_with_value(
        tmp_path,
        error=None,
        error_components=[
            {
                "name": "empty",
                "error": None,
                "lower_error": None,
                "upper_error": None,
                "evidence": [
                    {
                        "file_id": "pdf-1",
                        "location": "Section 3",
                        "page": 1,
                        "quote": "",
                    }
                ],
            }
        ],
    )
    with pytest.raises(Exception, match="至少提供一个误差数值"):
        parse_gold_annotation(empty)


def test_mixing_symmetric_and_asymmetric_still_rejected(tmp_path):
    _, draft = _draft_with_value(
        tmp_path, error="8.0", lower_error="5.0", upper_error="6.0"
    )
    with pytest.raises(Exception, match="不能同时使用"):
        parse_gold_annotation(draft)


def test_range_without_stated_interpretation_uses_unspecified(tmp_path):
    _, draft = _draft_with_value(
        tmp_path,
        value=None,
        error=None,
        limit_kind="range",
        range_lower="55.0",
        range_upper="75.0",
        interval_kind="unspecified",
    )
    annotation = parse_gold_annotation(draft)
    value = annotation.contributions[0].quantities[0].values[0]
    assert value.interval_kind == "unspecified"


# ---- PDF 结构、页码与引文检查 ----


def _reportlab_pdf(pages: list[list[str]], *, compress: bool = True) -> bytes:
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    maker = canvas.Canvas(buf, pagesize=(400, 300))
    maker.setPageCompression(1 if compress else 0)
    for lines in pages:
        for offset, line in enumerate(lines):
            maker.drawString(10, 250 - 20 * offset, line)
        maker.showPage()
    maker.save()
    return buf.getvalue()


def _independent_pdf(pages: list[list[str]], *, compressed: bool, reorder: bool):
    """与被检实现独立构造的 PDF：对象可乱序、内容流可选 Flate 压缩。"""

    def text_line(line: str) -> bytes:
        escaped = line.replace("(", "\\(").replace(")", "\\)")
        return f"({escaped}) Tj T*".encode()

    objects: dict[int, bytes] = {}
    page_numbers = [4 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    font_number = 3 if reorder else 1
    catalog_number = 1 if reorder else 2
    pages_number = 2 if reorder else 3
    # 页对象与内容流固定从 4 号开始；目录、页树与字体编号可交换（重排）
    objects[catalog_number] = f"<< /Type /Catalog /Pages {pages_number} 0 R >>".encode()
    objects[pages_number] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    )
    objects[font_number] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    for number, lines in zip(page_numbers, pages, strict=True):
        text = (
            b"BT /F1 12 Tf 72 720 Td\n"
            + b"\n".join(text_line(line) for line in lines)
            + b"\nET"
        )
        if compressed:
            stream = zlib.compress(text)
            filter_entry = " /Filter /FlateDecode"
        else:
            stream = text
            filter_entry = ""
        objects[number] = (
            f"<< /Type /Page /Parent {pages_number} 0 R /Contents"
            f" {number + 1} 0 R /Resources << /Font << /F1"
            f" {font_number} 0 R >> >> >>".encode()
        )
        objects[number + 1] = (
            f"<< /Length {len(stream)}{filter_entry} >>\nstream\n".encode()
            + stream
            + b"\nendstream"
        )
    header = b"%PDF-1.5\n"
    body = bytearray()
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(header) + len(body)
        body += f"{number} 0 obj\n".encode() + objects[number] + b"\nendobj\n"
    xref_offset = len(header) + len(body)
    out = bytearray(header)
    out += body
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for number in sorted(objects):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += (
        f"trailer << /Size {len(objects) + 1} /Root {catalog_number} 0 R"
        f" >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _analyze(data: bytes):
    from hvs_catalog.evaluation.pdfcheck import analyze_pdf

    return analyze_pdf(data)


def test_valid_pdf_with_page_marker_in_text_counts_real_pages(tmp_path):
    """正文里印出字面 /Type /Page 的有效 PDF：按真实页树计数，不靠正则。"""
    data = _reportlab_pdf(
        [
            ["introduction mentions /Type /Page literally"],
            ["Table 2: SYN-A 612.0 +- 8.0 km/s"],
        ]
    )
    analysis = _analyze(data)
    assert analysis.openable
    assert analysis.page_count == 2


@pytest.mark.parametrize("compressed", [True, False])
@pytest.mark.parametrize("reorder", [True, False])
def test_independent_pdfs_parsed_correctly(compressed, reorder):
    data = _independent_pdf(
        [
            ["independent page one"],
            ["independent page two with SYN-B 300.0 km/s"],
        ],
        compressed=compressed,
        reorder=reorder,
    )
    analysis = _analyze(data)
    assert analysis.openable, analysis.notes
    assert analysis.page_count == 2
    assert "SYN-B 300.0 km/s" in analysis.page_texts[1]


def test_fake_pdf_with_markers_rejected(tmp_path):
    fake = b"%PDF-1.4\n3 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n%%EOF\n"
    analysis = _analyze(fake)
    assert not analysis.openable
    assert analysis.page_count is None
    # 检查报告按材料问题报告，不当作通过
    material = tmp_path / "materials"
    material.mkdir()
    (material / "paper.pdf").write_bytes(fake)
    draft = build_fresh_draft(pdf_sha256=hashlib.sha256(fake).hexdigest())
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "file_not_pdf" in [item.code for item in report.problems_of("material")]


def test_truncated_pdf_reported_not_openable(tmp_path):
    data = _reportlab_pdf([["page one"], ["page two"], ["page three"]])
    truncated = data[: len(data) // 2]
    analysis = _analyze(truncated)
    assert not analysis.openable
    assert any("截断" in note or "解析器" in note for note in analysis.notes)


def test_boundary_page_ordinals(tmp_path):
    material, digest = _material(tmp_path)  # 3 页
    draft = build_fresh_draft(pdf_sha256=digest, page=3)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "page_out_of_range" not in [
        item.code for item in report.problems_of("material")
    ]
    draft = build_fresh_draft(pdf_sha256=digest, page=4)
    report = check_gold_annotation(parse_gold_annotation(draft), material_root=material)
    assert "page_out_of_range" in [item.code for item in report.problems_of("material")]


def test_quote_checks_on_compressed_reportlab_pdf(tmp_path):
    data = _reportlab_pdf(
        [["title page"], ["SYN-A 612.0 +- 8.0 km/s in table"], ["conclusions"]]
    )
    from hvs_catalog.evaluation.pdfcheck import check_quote

    result, _ = check_quote(data, page=2, quote="SYN-A 612.0 +- 8.0 km/s")
    assert result == "matched"
    result, _ = check_quote(data, page=2, quote="not printed anywhere")
    assert result == "not_found"


# ---- 逐项比较完整性 ----


def _migrated(tmp_path, document=None):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(document or build_legacy_v4_doc(), ensure_ascii=False),
        encoding="utf-8",
    )
    material, _ = _material(tmp_path)
    result = migrate_legacy_annotation(
        legacy_path=legacy,
        draft_dir=tmp_path / "draft",
        known_pdfs=[material / "paper.pdf"],
    )
    return legacy, result


def test_boundness_status_change_is_a_difference(tmp_path):
    legacy, result = _migrated(tmp_path)
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    draft["contributions"][0]["paper_boundness"]["status"] = "bound"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    assert report.difference_count > 0
    item = next(
        item for item in report.items if item.subject.endswith("paper_boundness")
    )
    assert item.status == "different"
    assert "unbound" in item.description and "bound" in item.description
    assert "专家审定" in item.description


def test_context_evidence_change_is_a_difference_with_values(tmp_path):
    legacy, result = _migrated(tmp_path)
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    value = draft["contributions"][0]["quantities"][0]["values"][0]
    value["context_evidence"] = [
        {
            "file_id": "pdf-1",
            "location": "Section 4.2, different paragraph",
            "page": None,
            "quote": "replaced quote text",
        }
    ]
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    item = next(item for item in report.items if "radial_velocity" in item.subject)
    assert item.status == "different"
    # 差异说明列出旧值/新值与确切位置，不只给计数
    assert "612.0" in item.description
    assert "Table 2, row SYN-A" in item.description
    assert "Section 4.2" in item.description


def test_conflict_content_differences_reported(tmp_path):
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantity_conflicts"] = [
        {
            "quantity": "observed_phase_space.distance",
            "reason": "two printed distances remain unreconciled",
            "evidence": [
                {"location": "Table 3", "quote": ""},
                {"location": "Section 4.2", "quote": ""},
            ],
        }
    ]
    legacy, result = _migrated(tmp_path, document)
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    conflict = draft["contributions"][0]["quantity_conflicts"][0]
    conflict["reason"] = "reason rewritten"
    conflict["evidence"][0]["location"] = "Table 9"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    item = next(
        item for item in report.items if item.subject.endswith("quantity_conflicts")
    )
    assert item.status == "different"
    assert "原因" in item.description
    assert "证据" in item.description


def test_conflict_added_alternatives_reported(tmp_path):
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantity_conflicts"] = [
        {
            "quantity": "observed_phase_space.distance",
            "reason": "two printed distances remain unreconciled",
            "evidence": [
                {"location": "Table 3", "quote": ""},
                {"location": "Section 4.2", "quote": ""},
            ],
        }
    ]
    legacy, result = _migrated(tmp_path, document)
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    conflict = draft["contributions"][0]["quantity_conflicts"][0]
    conflict["alternatives"] = [
        {
            "value": "55.0",
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
            "evidence": [
                {"file_id": "pdf-1", "location": "Table 3", "page": None, "quote": ""}
            ],
            "context_evidence": [],
        },
        {
            "value": "75.0",
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
            "evidence": [
                {
                    "file_id": "pdf-1",
                    "location": "Section 4.2",
                    "page": None,
                    "quote": "",
                }
            ],
            "context_evidence": [],
        },
    ]
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    item = next(
        item for item in report.items if item.subject.endswith("quantity_conflicts")
    )
    assert item.status == "different"
    assert "备选" in item.description


def test_exclusions_evidence_change_reported(tmp_path):
    document = build_legacy_v4_doc(
        reviewed_exclusions=[
            {
                "reason": "not a candidate",
                "evidence": [{"location": "Appendix A", "quote": ""}],
            }
        ]
    )
    legacy, result = _migrated(tmp_path, document)
    draft = json.loads(result.draft_path.read_text(encoding="utf-8"))
    draft["reviewed_exclusions"][0]["evidence"][0]["location"] = "Appendix B"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    report = compare_legacy_with_current(legacy_path=legacy, current_path=changed)
    item = next(item for item in report.items if item.subject == "reviewed_exclusions")
    assert item.status == "different"


def test_pure_migration_still_compares_identical(tmp_path):
    legacy, result = _migrated(tmp_path)
    report = compare_legacy_with_current(
        legacy_path=legacy, current_path=result.draft_path
    )
    assert report.difference_count == 0
    assert report.matched_count >= 4
