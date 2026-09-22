"""迁移行为：合成场景一到五，草稿逐字保留、待核查项与旧输入只读。"""

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
    """对象 SYN-A 的旧值为 612.0、误差 8.0、单位 km/s：逐字保留。"""
    legacy = _write_legacy(tmp_path, build_legacy_v4_doc())
    result = migrate_legacy_annotation(
        legacy_path=legacy, draft_dir=tmp_path / "draft", material_root=tmp_path
    )
    (contribution,) = result.draft.contributions
    (group,) = contribution.quantities
    (value,) = group.values
    assert (value.value, value.error, value.unit) == ("612.0", "8.0", "km/s")
    assert value.value != 612  # 不改成数字
    # 未提供已知材料时证据缺口继续报告
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
    assert contribution.paper_boundness.status is None  # 不自动审定新状态
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
    # 程序不推导新增状态
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
    assert evidence.quote == ""  # 引文为空，不虚构
    assert evidence.page is None  # 不通过正则猜页序号
    assert evidence.file_id is None  # 未提供已知材料时不绑定


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
    # 不能生成“新名单完整且为空”一类声明
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
    assert kept.quantity == "observed_phase_space.distance"
    assert kept.reason == conflict["reason"]
    assert [e.location for e in kept.evidence] == ["Table 3", "Section 4.2"]
    assert kept.alternatives == []  # 不猜测两组数值
    assert any(
        item.code == "conflict_structure_pending"
        for item in result.check.problems_of("science_pending")
    )


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
    assert value.value is None  # 旧空字符串转为新格式空值


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
    assert first.draft.files[0].sha256 == digest  # 哈希取自实际文件
    (value,) = first.draft.contributions[0].quantities[0].values
    assert value.evidence[0].file_id == "pdf-1"
    # 技术编号与草稿内容可复现
    assert first.draft.model_dump(mode="json") == second.draft.model_dump(mode="json")
    assert first.report.model_dump(mode="json") == second.report.model_dump(mode="json")
    # 绑定已知材料后不再有未绑定证据问题
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
    assert not result.report.canary_verified  # 如实报告核对失败
    read_legacy_annotation(legacy)  # 读取本身不因 canary 不符而拒绝


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
