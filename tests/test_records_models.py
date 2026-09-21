"""保存形式的字段与形式约束：数值表达、名单条目、完成情况与引用类型。"""

from __future__ import annotations

import pytest
from records_helpers import EV_VALUE, point_value, synthetic_source

from hvs_catalog.records import (
    ArtifactRef,
    PaperBoundness,
    QuantityConflict,
    QuantityCoverageEntry,
    RosterEntry,
    StepContext,
    expand_coverage,
    quantity_paths,
)
from hvs_catalog.records.submission import CoverageDeclaration


def test_point_value_requires_value():
    with pytest.raises(ValueError, match="必须提供 value"):
        point_value(value=None)


def test_symmetric_and_asymmetric_error_exclusive():
    with pytest.raises(ValueError, match="对称误差和非对称误差"):
        point_value(error="10", lower_error="1", upper_error="2")


def test_single_sided_error_kept_as_reported():
    value = point_value(error=None, upper_error="0.5")
    assert value.error is None
    assert value.lower_error is None
    assert value.upper_error == "0.5"


def test_zero_error_not_faked_for_unreported_side():
    value = point_value(error=None, lower_error="0.2")
    assert value.upper_error is None  # 未报告一侧保持 null，不补 0


def test_pure_range_never_gets_central_value():
    with pytest.raises(ValueError, match="不能补中心值"):
        point_value(
            value="8",
            error=None,
            limit_kind="range",
            range_lower="7",
            range_upper="10",
            interval_kind="credible",
        )


def test_pure_range_requires_both_endpoints():
    with pytest.raises(ValueError, match="必须提供两端端点"):
        point_value(
            value=None,
            error=None,
            limit_kind="range",
            range_lower="7",
            range_upper=None,
            interval_kind="credible",
        )


def test_pure_range_requires_interval_kind():
    with pytest.raises(ValueError, match="区间解释"):
        point_value(
            value=None,
            error=None,
            limit_kind="range",
            range_lower="7",
            range_upper="10",
            interval_kind=None,
        )
    unspecified = point_value(
        value=None,
        error=None,
        limit_kind="range",
        range_lower="8",
        range_upper="12",
        interval_kind="unspecified",
    )
    assert unspecified.interval_kind == "unspecified"


def test_limit_uses_value_not_endpoints():
    upper = point_value(value="500", error=None, limit_kind="upper_limit")
    assert upper.value == "500"
    with pytest.raises(ValueError, match="区间端点"):
        point_value(value="500", error=None, limit_kind="upper_limit", range_lower="1")


def test_every_filled_component_needs_evidence_part():
    with pytest.raises(ValueError, match="直接证据"):
        point_value(
            direct_evidence=[
                {
                    "part": "value",
                    "evidence_ref": EV_VALUE.model_dump(mode="json"),
                    "raw_fragment": "520",
                }
            ]
        )


def test_evidence_part_for_unfilled_component_rejected():
    with pytest.raises(ValueError, match="未填写的组件"):
        point_value(
            direct_evidence=[
                {
                    "part": "value",
                    "evidence_ref": EV_VALUE.model_dump(mode="json"),
                    "raw_fragment": "520",
                },
                {
                    "part": "error",
                    "evidence_ref": EV_VALUE.model_dump(mode="json"),
                    "raw_fragment": "10",
                },
                {
                    "part": "range_lower",
                    "evidence_ref": EV_VALUE.model_dump(mode="json"),
                    "raw_fragment": "7",
                },
            ]
        )


def test_error_component_evidence_coverage():
    component = {
        "name": "statistical",
        "error": "0.3",
        "lower_error": None,
        "upper_error": None,
        "direct_evidence": [
            {
                "part": "error",
                "evidence_ref": EV_VALUE.model_dump(mode="json"),
                "raw_fragment": "0.3",
            }
        ],
    }
    value = point_value(error_components=[component])
    assert value.error_components[0].name == "statistical"
    with pytest.raises(ValueError):
        point_value(
            error_components=[
                {
                    "name": "systematic",
                    "error": "0.4",
                    "lower_error": None,
                    "upper_error": None,
                    "direct_evidence": [],
                }
            ]
        )


def test_coverage_report_needs_direct_evidence():
    with pytest.raises(ValueError, match="覆盖概率"):
        point_value(
            limit_kind="range",
            value=None,
            error=None,
            range_lower="7",
            range_upper="10",
            interval_kind="credible",
            coverage={
                "value": "95",
                "unit": "%",
                "direct_evidence": [
                    {
                        "part": "error",
                        "evidence_ref": EV_VALUE.model_dump(mode="json"),
                        "raw_fragment": "95",
                    }
                ],
            },
        )


def test_total_error_and_named_components_coexist():
    value = point_value(
        error="0.5",
        error_components=[
            {
                "name": "statistical",
                "error": "0.3",
                "lower_error": None,
                "upper_error": None,
                "direct_evidence": [
                    {
                        "part": "error",
                        "evidence_ref": EV_VALUE.model_dump(mode="json"),
                        "raw_fragment": "0.3",
                    }
                ],
            }
        ],
    )
    assert value.error == "0.5"
    assert value.error_components[0].error == "0.3"


def test_inclusive_flags_only_with_bounds():
    with pytest.raises(ValueError, match="端点包含"):
        point_value(lower_inclusive=True)
    ranged = point_value(
        value=None,
        error=None,
        limit_kind="range",
        range_lower="7",
        range_upper="10",
        interval_kind="ordinary_range",
        lower_inclusive=True,
        upper_inclusive=False,
    )
    assert ranged.lower_inclusive is True


def test_unknown_quantity_path_rejected():
    with pytest.raises(ValueError, match="未知的物理量编码"):
        QuantityCoverageEntry.model_validate(
            {
                "quantity": "fictional.quantity",
                "state": "complete",
                "checked_file_ids": ["main"],
                "gap_refs": [],
                "issue_refs": [],
            }
        )


def test_complete_state_cannot_reference_gap():
    with pytest.raises(ValueError, match="完成声明"):
        QuantityCoverageEntry.model_validate(
            {
                "quantity": "observed_phase_space.ra",
                "state": "complete",
                "checked_file_ids": ["main"],
                "gap_refs": [
                    {"source_ref": {"record_id": "s", "revision": 1}, "gap_id": "g"}
                ],
                "issue_refs": [],
            }
        )


def test_checked_state_requires_files():
    with pytest.raises(ValueError, match="已检查文件"):
        QuantityCoverageEntry.model_validate(
            {
                "quantity": "observed_phase_space.ra",
                "state": "complete",
                "checked_file_ids": [],
                "gap_refs": [],
                "issue_refs": [],
            }
        )


def test_conflict_requires_distinct_alternatives():
    issue = EV_VALUE
    with pytest.raises(ValueError, match="两个不同的完整数值表达"):
        QuantityConflict.model_validate(
            {
                "conflict_id": "c1",
                "quantity": "observed_phase_space.radial_velocity",
                "reason": "Two printings disagree.",
                "alternatives": [
                    point_value("a1").model_dump(mode="json"),
                    point_value("a2").model_dump(mode="json"),
                ],
                "issue_ref": issue.model_dump(mode="json"),
            }
        )


def test_roster_entry_disposition_rules():
    identifier = {"value": "FIC-A", "evidence_refs": [EV_VALUE.model_dump(mode="json")]}
    with pytest.raises(ValueError, match="伪造已收录贡献"):
        RosterEntry.model_validate(
            {
                "item_id": "roster-A",
                "identifiers": [identifier],
                "disposition": "excluded",
                "evidence_refs": [EV_VALUE.model_dump(mode="json")],
                "issue_refs": [],
                "contribution_id": "contribution-A",
            }
        )
    with pytest.raises(ValueError, match="待决条目必须关联未决问题"):
        RosterEntry.model_validate(
            {
                "item_id": "roster-A",
                "identifiers": [identifier],
                "disposition": "pending",
                "evidence_refs": [],
                "issue_refs": [],
            }
        )
    with pytest.raises(ValueError, match="排除条目必须带有依据"):
        RosterEntry.model_validate(
            {
                "item_id": "roster-A",
                "identifiers": [identifier],
                "disposition": "excluded",
                "evidence_refs": [],
                "issue_refs": [],
            }
        )


def test_artifact_ref_path_cannot_escape_root():
    with pytest.raises(ValueError, match="越界"):
        ArtifactRef.model_validate({"path": "../outside.txt", "sha256": "0" * 64})
    with pytest.raises(ValueError, match="越界"):
        ArtifactRef.model_validate({"path": "/absolute.txt", "sha256": "0" * 64})


def test_boundness_positive_states_need_evidence():
    with pytest.raises(ValueError, match="原文证据"):
        PaperBoundness.model_validate(
            {"status": "bound", "evidence_refs": [], "issue_refs": [], "gap_refs": []}
        )
    # no_overall_conclusion 允许没有正面结论引句
    PaperBoundness.model_validate(
        {
            "status": "no_overall_conclusion",
            "evidence_refs": [],
            "issue_refs": [],
            "gap_refs": [],
        }
    )


def test_expansion_covers_all_eighteen():
    source = synthetic_source()
    context = StepContext(
        source=source,
        paper_ref=EV_VALUE,
        created_at=source.created_at,
        requested_quantities=quantity_paths(),
    )
    declaration = CoverageDeclaration.model_validate(
        {
            "scope": "all_requested",
            "common": {
                "state": "not_checked",
                "checked_file_ids": [],
                "gap_ids": [],
                "issue_ids": [],
            },
            "exceptions": [],
        }
    )
    rows = expand_coverage(
        declaration, context=context, item_id="contribution-A", extra_issue_refs={}
    )
    assert [row.quantity for row in rows] == list(quantity_paths())
    assert all(row.state == "not_checked" for row in rows)
