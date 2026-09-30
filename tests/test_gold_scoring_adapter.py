"""集成：既有 Gold 共同定义 → 评分输入的纯内存适配（C07）。

覆盖 compact v2 正式答案与 v1 标注对象两条路径：字段映射、原报/条件/
证据保留、冲突备选进入同一物理量集合、部分覆盖需要显式子范围、
草稿与待办被拒绝；适配不改写输入对象。
"""

from __future__ import annotations

import copy

import pytest
from evaluation_helpers import build_fresh_draft, write_synthetic_pdf

from hvs_catalog.evaluation import (
    GoldAdaptationError,
    adapt_compact_answer,
    adapt_compact_gold_pair,
    gold_entries_from_annotation,
    gold_entries_from_compact_answer,
    gold_entries_from_compact_pair,
)
from hvs_catalog.evaluation.compact import (
    CompactGoldPair,
    check_compact_gold_pair,
    compact_gold_annotation,
)
from hvs_catalog.evaluation.models import parse_gold_annotation
from hvs_catalog.evaluation.scoring import ConfirmedRange

RV = "observed_phase_space.radial_velocity"


def _pair(tmp_path, *, scope="complete", draft=None):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    if draft is None:
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
    return material, compact_gold_annotation(
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


def test_compact_pair_maps_fields_and_preserves_originals(tmp_path):
    _, pair = _pair(tmp_path)
    entries = gold_entries_from_compact_pair(pair)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.paper_key == "2501.00001"
    assert entry.coverage == "complete" and entry.confirmed_ranges == []
    (obj,) = entry.objects
    assert obj.label == "2501.00001:cand01"
    assert obj.identifiers == ["SYN-A"]
    assert obj.contribution_type == "candidates_found"
    assert obj.author_judgment == "unbound"
    (group,) = obj.quantities
    assert group.quantity_path == RV
    (value,) = group.values
    measured = value.value
    assert (measured.value, measured.error, measured.unit) == ("612.0", "8.0", "km/s")
    assert measured.condition == "heliocentric, adopted solution"
    assert measured.source == "this_paper"
    assert measured.limit_kind == "none"
    # 证据位置与引文逐字保留在诊断 sidecar
    assert value.context_evidence == ["Table 2, row SYN-A — SYN-A 612.0 +- 8.0 km/s"]
    # 数值组件证据与原报字符串一致
    parts = {part.part: part.raw_fragment for part in measured.direct_evidence}
    assert parts == {"value": "612.0", "error": "8.0"}


def test_compact_condition_none_is_rejected_as_preparation_gap(tmp_path):
    """缺失条件不能破例当成“原文未报告条件”的科学断言。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantities"][0]["values"][0]["condition"] = None
    draft = parse_gold_annotation(payload)
    _, pair = _pair(tmp_path, draft=draft)
    with pytest.raises(GoldAdaptationError, match="条件"):
        gold_entries_from_compact_pair(pair)
    # 已明确填写的未报条件说明（非空字符串）照原文逐字保留
    payload["contributions"][0]["quantities"][0]["values"][0]["condition"] = (
        "The paper reports no additional condition for this measurement."
    )
    draft = parse_gold_annotation(payload)
    _, pair = _pair(tmp_path, draft=draft)
    entry = gold_entries_from_compact_pair(pair)[0]
    measured = entry.objects[0].quantities[0].values[0].value
    assert (
        measured.condition
        == "The paper reports no additional condition for this measurement."
    )


def test_partial_coverage_requires_explicit_confirmed_ranges(tmp_path):
    _, pair = _pair(tmp_path, scope="partial")
    with pytest.raises(GoldAdaptationError, match="封闭子范围"):
        gold_entries_from_compact_pair(pair)
    ranges = [ConfirmedRange(object_identifiers=["SYN-A"], quantity_paths=[RV])]
    entry = gold_entries_from_compact_pair(
        pair, confirmed_ranges={"2501.00001": ranges}
    )[0]
    assert entry.coverage == "partial"
    assert entry.confirmed_ranges == ranges


def test_conflict_alternatives_join_the_quantity_pool(tmp_path):
    evidence = {
        "file_id": "pdf-1",
        "location": "Table 2, row SYN-A",
        "page": 2,
        "quote": "",
    }

    def alt(value: str) -> dict:
        return {
            "value": value,
            "error": "8.0",
            "lower_error": None,
            "upper_error": None,
            "unit": "km/s",
            "limit_kind": "none",
            "range_lower": None,
            "range_upper": None,
            "coordinate_format": None,
            "condition": "heliocentric, adopted solution",
            "interval_kind": None,
            "coverage": None,
            "lower_quantile": None,
            "upper_quantile": None,
            "error_components": [],
            "representation": None,
            "source": "this_paper",
            "source_note": None,
            "evidence": [dict(evidence)],
            "context_evidence": [],
        }

    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantity_conflicts"] = [
        {
            "conflict_id": "cf1",
            "quantity": RV,
            "reason": "Two printings disagree.",
            "evidence": [
                {"file_id": "pdf-1", "location": "Table 2", "page": 2},
                {"file_id": "pdf-1", "location": "Table 5", "page": 6},
            ],
            "alternatives": [alt("600.0"), alt("620.0")],
        }
    ]
    draft = parse_gold_annotation(payload)
    _, pair = _pair(tmp_path, draft=draft)
    entry = gold_entries_from_compact_pair(pair)[0]
    (group,) = entry.objects[0].quantities
    record_ids = [value.record_id for value in group.values]
    # 确认值 + 冲突双方都在同一物理量集合；record_id 唯一定位诊断
    assert record_ids == [
        "2501.00001:cand01:observed_phase_space.radial_velocity:0",
        "2501.00001:cand01:cf1:0",
        "2501.00001:cand01:cf1:1",
    ]
    assert group.values[1].value.value == "600.0"
    assert group.values[2].value.value == "620.0"


def test_v1_annotation_answer_maps_and_draft_is_rejected(tmp_path):
    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    draft = parse_gold_annotation(payload)
    with pytest.raises(GoldAdaptationError, match="草稿"):
        gold_entries_from_annotation(draft)
    payload["kind"] = "answer"
    answer = parse_gold_annotation(payload)
    entry = gold_entries_from_annotation(answer)[0]
    assert entry.paper_key == "2501.00001"
    assert entry.coverage == "complete"
    assert entry.objects[0].identifiers == ["SYN-A"]
    measured = entry.objects[0].quantities[0].values[0].value
    assert measured.value == "612.0"
    # 草稿即使结构合法，未决待办也随“草稿不能评分”一并拒绝
    pending_payload = payload | {"kind": "draft"}
    pending_payload["pending"] = [
        {
            "item_id": "p1",
            "category": "science_review",
            "subject": "boundness",
            "description": "needs expert review",
        }
    ]
    with pytest.raises(GoldAdaptationError, match="草稿"):
        gold_entries_from_annotation(parse_gold_annotation(pending_payload))


def test_no_contributions_answer_maps_to_correct_empty_gold(tmp_path):
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["status"] = "no_contributions"
    payload["contributions"] = []
    draft = parse_gold_annotation(payload)
    _, pair = _pair(tmp_path, draft=draft)
    entry = gold_entries_from_compact_pair(pair)[0]
    assert entry.objects == []
    assert entry.coverage == "complete"


def test_adaptation_does_not_mutate_input(tmp_path):
    _, pair = _pair(tmp_path)
    before = copy.deepcopy(pair.answer.model_dump(mode="json"))
    gold_entries_from_compact_pair(pair)
    gold_entries_from_compact_pair(pair)
    assert pair.answer.model_dump(mode="json") == before


# ---- 配对绑定校验、未决待办与 answer-only 边界 ----


def _review_payloads():
    return (
        {
            "value": "checked",
            "basis": "Synthetic fixed PDF and source correspondence",
            "record_sha256": "a" * 64,
        },
        {
            "value": "complete",
            "basis": "Synthetic explicit scope review",
            "record_sha256": "a" * 64,
        },
    )


def test_pair_with_mismatched_arxiv_is_rejected(tmp_path):
    _, pair = _pair(tmp_path)
    tampered = pair.metadata.model_copy(deep=True)
    tampered.paper.arxiv_id = "2501.99999"
    broken = CompactGoldPair(answer=pair.answer, metadata=tampered)
    with pytest.raises(GoldAdaptationError, match="绑定"):
        gold_entries_from_compact_pair(broken)


def test_pair_with_wrong_answer_sha256_is_rejected(tmp_path):
    _, pair = _pair(tmp_path)
    tampered = pair.metadata.model_copy(deep=True)
    tampered.review.answer_sha256 = "b" * 64
    broken = CompactGoldPair(answer=pair.answer, metadata=tampered)
    with pytest.raises(GoldAdaptationError, match="绑定"):
        gold_entries_from_compact_pair(broken)


def test_pair_with_scope_conflict_is_rejected(tmp_path):
    _, pair = _pair(tmp_path)
    tampered = pair.metadata.model_copy(deep=True)
    tampered.review.scope.value = "partial"
    broken = CompactGoldPair(answer=pair.answer, metadata=tampered)
    with pytest.raises(GoldAdaptationError, match="绑定"):
        gold_entries_from_compact_pair(broken)


def test_pair_with_unresolved_items_is_not_ready(tmp_path):
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    draft = parse_gold_annotation(build_fresh_draft(pdf_sha256=digest))
    material_review, scope_review = _review_payloads()
    pair = compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review=material_review,
        scope_review=scope_review,
        unresolved=[
            {
                "subject": "boundness under model M2",
                "value": "Author statements conflict; not resolved.",
                "evidence": [{"file_id": "pdf-1", "location": "Section 5", "page": 3}],
            }
        ],
    )
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.formal_readiness is False
    assert any("unresolved_1" in gap for gap in adaptation.readiness_gaps)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)


def test_binding_is_preserved_for_report_checks(tmp_path):
    _, pair = _pair(tmp_path)
    adaptation = adapt_compact_gold_pair(pair)
    binding = adaptation.binding
    assert adaptation.formal_readiness is True
    assert binding["paper_key"] == "2501.00001"
    assert binding["answer_sha256"] == pair.metadata.review.answer_sha256
    assert binding["rules"]["version"] == pair.metadata.rules.version
    assert binding["files"][0]["file_id"] == "pdf-1"
    assert binding["coverage"] == "complete"


def test_answer_only_adaptation_is_not_formal_readiness(tmp_path):
    """answer-only 输入缺 metadata 绑定，不冒称正式就绪。"""
    _, pair = _pair(tmp_path)
    adaptation = adapt_compact_answer(pair.answer)
    assert adaptation.entries
    assert adaptation.binding is None
    assert adaptation.formal_readiness is False
    assert adaptation.readiness_gaps == []
    assert any("metadata" in note for note in adaptation.limitations)
    assert adaptation.entries[0].objects[0].identifiers == ["SYN-A"]


# ---- 材料/科学准备缺口必须实际阻止就绪 ----


def _draft(tmp_path, **overrides):
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    for key, value in overrides.items():
        payload[key] = value
    return parse_gold_annotation(payload)


def _pair_from_draft(
    draft, *, material_review=None, scope_review=None, unresolved=None
):
    if material_review is None:
        material_review = {
            "value": "checked",
            "basis": "Synthetic fixed PDF and source correspondence",
            "record_sha256": "a" * 64,
        }
    if scope_review is None:
        scope_review = {
            "value": "complete",
            "basis": "Synthetic explicit scope review",
            "record_sha256": "a" * 64,
        }
    return compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review=material_review,
        scope_review=scope_review,
        unresolved=unresolved,
    )


def test_source_missing_is_a_preparation_gap(tmp_path):
    """source=None 是材料准备缺口，不能当就绪评分输入。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    # 无 LaTeX 来源对应：v1 草稿只能保持 pending，材料缺口真实存在
    payload["material_correspondence"]["latex"] = None
    payload["material_correspondence"]["status"] = "pending"
    draft = parse_gold_annotation(payload)
    pair = _pair_from_draft(draft)
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.binding is not None  # 绑定结构仍可核对
    assert adaptation.formal_readiness is False
    assert any("source_missing" in gap for gap in adaptation.readiness_gaps)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)


def test_material_differences_is_a_preparation_gap(tmp_path):
    """审定记录在案的 PDF/源材料差异是科学准备缺口。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["material_correspondence"]["differences"] = [
        "Table numbering differs between the PDF and the LaTeX source."
    ]
    draft = parse_gold_annotation(payload)
    material_review = {
        "value": "checked",
        "basis": "Synthetic fixed PDF and source correspondence",
        "record_sha256": "a" * 64,
        "differences": [
            "Table numbering differs between the PDF and the LaTeX source."
        ],
    }
    pair = _pair_from_draft(draft, material_review=material_review)
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.formal_readiness is False
    assert any("material_differences" in gap for gap in adaptation.readiness_gaps)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)


def test_missing_value_evidence_is_a_preparation_gap(tmp_path):
    """数值无证据是材料准备缺口。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantities"][0]["values"][0]["evidence"] = []
    draft = parse_gold_annotation(payload)
    pair = _pair_from_draft(draft)
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.formal_readiness is False
    assert any("missing_value_evidence" in gap for gap in adaptation.readiness_gaps)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)


def test_related_memory_gaps_all_block_readiness(tmp_path):
    """纯内存中的各类材料/科学缺口同样阻止就绪。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    contribution = payload["contributions"][0]
    contribution["contribution_evidence"] = []  # missing_contribution_evidence
    contribution["identifiers"][0]["evidence"] = []  # missing_identifier_evidence
    contribution["paper_boundness"]["evidence"] = []  # missing_boundness_evidence
    contribution["quantity_conflicts"] = [
        {
            "conflict_id": "cf1",
            "quantity": RV,
            "reason": "Two printings disagree.",
            "evidence": [
                {"file_id": "pdf-1", "location": "Table 2", "page": 2},
                {"file_id": "pdf-1", "location": "Table 5", "page": 6},
            ],
            "alternatives": [],  # conflict_structure_unknown：未结构化
        }
    ]
    draft = parse_gold_annotation(payload)
    pair = _pair_from_draft(draft)
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.formal_readiness is False
    joined = " ".join(adaptation.readiness_gaps)
    for code in (
        "missing_contribution_evidence",
        "missing_identifier_evidence",
        "missing_boundness_evidence",
        "conflict_structure_unknown",
    ):
        assert code in joined
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(pair)


def test_approved_structured_conflicts_are_not_gaps(tmp_path):
    """已审定、结构完整的冲突双方不是待办（配对仍就绪）。"""
    evidence = {
        "file_id": "pdf-1",
        "location": "Table 2, row SYN-A",
        "page": 2,
        "quote": "",
    }

    def alt(value: str) -> dict:
        return {
            "value": value,
            "error": "8.0",
            "lower_error": None,
            "upper_error": None,
            "unit": "km/s",
            "limit_kind": "none",
            "range_lower": None,
            "range_upper": None,
            "coordinate_format": None,
            "condition": "heliocentric, adopted solution",
            "interval_kind": None,
            "coverage": None,
            "lower_quantile": None,
            "upper_quantile": None,
            "error_components": [],
            "representation": None,
            "source": "this_paper",
            "source_note": None,
            "evidence": [dict(evidence)],
            "context_evidence": [],
        }

    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantity_conflicts"] = [
        {
            "conflict_id": "cf1",
            "quantity": RV,
            "reason": "Two printings disagree.",
            "evidence": [
                {"file_id": "pdf-1", "location": "Table 2", "page": 2},
                {"file_id": "pdf-1", "location": "Table 5", "page": 6},
            ],
            "alternatives": [alt("600.0"), alt("620.0")],
        }
    ]
    draft = parse_gold_annotation(payload)
    pair = _pair_from_draft(draft)
    adaptation = adapt_compact_gold_pair(pair)
    assert adaptation.readiness_gaps == []
    assert adaptation.formal_readiness is True
    entries = gold_entries_from_compact_pair(pair)
    assert len(entries[0].objects[0].quantities[0].values) == 3


def test_partial_scope_with_confirmed_ranges_does_not_clear_other_gaps(tmp_path):
    """显式封闭子范围只解决 partial_coverage，其余缺口照实保留。"""
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantities"][0]["values"][0]["evidence"] = []
    draft = parse_gold_annotation(payload)
    material_review, scope_review = _review_payloads()
    scope_review = dict(scope_review)
    scope_review["value"] = "partial"
    pair = compact_gold_annotation(
        draft,
        title="Synthetic candidate paper",
        original_annotator="synthetic-expert",
        reviewer="synthetic-reviewer",
        material_review=material_review,
        scope_review=scope_review,
        scope_limitations=[
            {
                "subject": "Table 3",
                "value": "One table was not covered by this reference answer.",
            }
        ],
        unresolved=[
            {
                "subject": "boundness under model M2",
                "value": "Author statements conflict; not resolved.",
                "evidence": [{"file_id": "pdf-1", "location": "Section 5", "page": 3}],
            }
        ],
    )
    ranges = [ConfirmedRange(object_identifiers=["SYN-A"], quantity_paths=[RV])]
    adaptation = adapt_compact_gold_pair(pair, confirmed_ranges={"2501.00001": ranges})
    joined = " ".join(adaptation.readiness_gaps)
    assert "partial_coverage" not in joined  # 既定封闭子范围规则解决覆盖缺口
    assert "missing_value_evidence" in joined  # 其他材料缺口不被顺带清除
    assert "unresolved" in joined  # 科学待办不被顺带清除
    assert adaptation.formal_readiness is False


def test_full_checker_reports_material_gaps_and_unresolved(tmp_path):
    """完整检查器按固定预期报告材料缺口与未决项。"""
    import hashlib

    material = tmp_path / "materials"
    digest = write_synthetic_pdf(material / "paper.pdf")
    (material / "paper.tex").write_bytes(b"Synthetic source material.")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantities"][0]["values"][0]["evidence"] = []
    payload["contributions"][0]["identifiers"][0]["evidence"] = []
    draft = parse_gold_annotation(payload)
    pair = _pair_from_draft(
        draft,
        unresolved=[
            {
                "subject": "boundness under model M2",
                "value": "Author statements conflict; not resolved.",
                "evidence": [{"file_id": "pdf-1", "location": "Section 5", "page": 3}],
            }
        ],
    )
    report = check_compact_gold_pair(pair, material_root=material)
    codes = sorted(problem.code for problem in report.problems)
    assert codes == [
        "missing_identifier_evidence",
        "missing_value_evidence",
        "unresolved_1",
    ]
    assert report.blocking_count == 2  # 两个 material
    assert report.pending_ids() == ["unresolved_1"]
    assert all(problem.message for problem in report.problems)
    del hashlib


# ---- 严格入口必须携带就绪状态 ----


def test_answer_only_strict_entry_rejects_pointing_to_informal_api(tmp_path):
    """缺 metadata 的 answer-only 不能经严格入口拿到可当 ready 的裸条目。"""
    _, pair = _pair(tmp_path)
    with pytest.raises(GoldAdaptationError, match="adapt_compact_answer"):
        gold_entries_from_compact_answer(pair.answer)
    # 非正式结构映射 API 仍可用，且自身不冒称就绪
    adaptation = adapt_compact_answer(pair.answer)
    assert adaptation.binding is None
    assert adaptation.formal_readiness is False
    assert adaptation.entries  # 结构映射仍完整，限制显式可见


def test_strict_entries_still_reject_gaps_and_keep_legit_pair_ready(tmp_path):
    """严格入口=绑定+就绪双门禁；合法已绑定配对不受影响。"""
    _, pair = _pair(tmp_path)
    entries = gold_entries_from_compact_pair(pair)
    assert entries[0].coverage == "complete"
    # 带 gap 的配对（answer-only 无绑定同理）拒绝，不无声降级
    digest = write_synthetic_pdf(tmp_path / "materials" / "paper.pdf")
    payload = build_fresh_draft(pdf_sha256=digest)
    payload["contributions"][0]["quantities"][0]["values"][0]["evidence"] = []
    draft = parse_gold_annotation(payload)
    gapped = _pair_from_draft(draft)
    with pytest.raises(GoldAdaptationError, match="准备缺口"):
        gold_entries_from_compact_pair(gapped)
