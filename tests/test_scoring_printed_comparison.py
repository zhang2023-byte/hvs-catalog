"""受限排版与条件短语比较的边界测试（A1–A5、B 系列判定来源、U1/U2）。

按域区分：数值用 ``compare_number``、单位用 ``compare_unit``、条件用
``compare_condition``。白名单未命中不再证明不同；数值域
外的英文文本、未支持单位语法与未支持条件差异均 undecidable；单位丢
失/尺度不同、数值符号/指数符号不同与 B7 的统计结构差异仍为确定判定。
完整分数影响由固定样例与修复回归测试覆盖。
"""

from __future__ import annotations

import pytest

from hvs_catalog.evaluation.scoring import (
    compare_condition,
    compare_number,
    compare_printed,
    compare_unit,
    normalize_printed,
)

M1 = "under Galactic potential M1"
M2 = "under Galactic potential M2"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("8.2", "8.2"),
        ("600", "600"),
        ("1.20 × 10³", "1.20\\times10^{3}"),  # A3
        ("12h34m56.7s", "12^{h}34^{m}56.7^{s}"),  # A4
        ("−8.2", "-8.2"),  # A2
    ],
)
def test_number_equivalent_forms(gold, prediction):
    assert compare_number(gold, prediction) == "equivalent"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("8.2", "8200"),  # B1 的数值侧：正确换算也不是排版等价
        ("600", "610"),  # B2：中心值不同
        ("8.2", "8.20"),  # C1 反向：尾零不能丢也不能补
        ("1.20\\times10^{3}", "1.20\\times10^{-3}"),  # 指数符号不同
        ("-8.2", "8.2"),  # 数值符号不同
        ("12h34m56.7s", "12h34m56.8s"),
    ],
)
def test_number_different_forms(gold, prediction):
    assert compare_number(gold, prediction) == "different"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("\\mystery{8.2}", "8.2"),  # U1：未知宏不能剥离
        ("8.2", "\\mystery{8.2}"),
        ("about eight", "eight"),  # 数值域外的英文差异不能确定语义
        ("< 600", "≤ 600"),  # 包含符差异属统计结构，由 inclusive 字段判定
    ],
)
def test_number_undecidable_forms(gold, prediction):
    assert compare_number(gold, prediction) == "undecidable"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("km s⁻¹", "\\mathrm{km\\,s^{-1}}"),  # A1
        ("s⁻¹ km", "s^{-1} km"),
    ],
)
def test_unit_equivalent_forms(gold, prediction):
    assert compare_unit(gold, prediction) == "equivalent"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("kpc", "pc"),  # 单位尺度不同（B1 单位侧）
        ("km s⁻¹", "km s⁻²"),  # 指数不同
    ],
)
def test_unit_different_forms(gold, prediction):
    assert compare_unit(gold, prediction) == "different"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("km s⁻¹", "km/s"),  # 未支持语法不能确定语义
        ("kpc", "\\mysteryunit"),
    ],
)
def test_unit_undecidable_forms(gold, prediction):
    assert compare_unit(gold, prediction) == "undecidable"


def test_unit_missing_is_structural_difference_in_content_compare():
    from hvs_catalog.evaluation.scoring.builders import build_measured_value
    from hvs_catalog.evaluation.scoring.layers import compare_content

    common = {
        "value_id": "x",
        "condition": "as reported",
        "value": "8.2",
        "error": "0.3",
    }
    with_unit = build_measured_value(unit="kpc", **common)
    no_unit = build_measured_value(**common)
    verdict, diff, _ = compare_content(with_unit, no_unit)
    assert verdict == "different" and "unit" in diff


def test_printed_alias_is_number_domain():
    assert compare_printed("600", "610") == "different"
    assert compare_printed("8.2", "8.2") == "equivalent"
    assert compare_printed("\\mystery{8.2}", "8.2") == "undecidable"


def test_normalize_keeps_reported_precision_and_exponent_sign():
    assert normalize_printed("1.20 × 10³") == "1.20*10^{3}"
    assert normalize_printed("1.20 × 10⁻³") == "1.20*10^{-3}"
    assert normalize_printed("8.20") != normalize_printed("8.2")
    assert normalize_printed("\\mystery{8.2}") is None


def test_condition_whitelist_and_model_distinction():
    assert compare_condition(M1, "assuming potential M1") == "equivalent"
    assert (
        compare_condition("assuming potential M2", "under Galactic potential M2")
        == "equivalent"
    )
    # A5：白名单外的句式不能因包含 M1 而同义，也不能据此证明不同
    assert compare_condition(M1, "not assuming potential M1") == "different"
    assert compare_condition(M1, "under Galactic potential M1 and M2") == "undecidable"
    # B8/U2：明确 M1 与 M2 不同；缺失限定无法判定
    assert compare_condition(M1, M2) == "different"
    assert (
        compare_condition(M1, "adopting the Galactic potential model") == "undecidable"
    )
    assert (
        compare_condition(
            "assuming potential M2", "assuming the Galactic potential model"
        )
        == "undecidable"
    )


def test_condition_unsupported_differences_are_undecidable():
    # 白名单未命中不能证明不同（原先被编码为 different）
    assert compare_condition(
        M1, "under Galactic potential M1 (same adopted model)"
    ) == ("undecidable")
    assert (
        compare_condition("as reported in paper", "as reported in the paper")
        == "undecidable"
    )
    assert compare_condition("as reported", "as reported elsewhere") == "undecidable"


def test_condition_exact_match_and_negation():
    assert (
        compare_condition("as reported in the paper", "as reported in the paper")
        == "equivalent"
    )
    assert (
        compare_condition("as reported in the paper", "as  reported   in the paper ")
        == "equivalent"
    )
    assert compare_condition("bound", "not bound") == "different"


def test_error_component_sides_and_missing_components_via_content_compare():
    from hvs_catalog.evaluation.scoring.builders import build_measured_value
    from hvs_catalog.evaluation.scoring.layers import compare_content

    common = {"condition": "as reported", "unit": "kpc", "value": "8.2"}
    gold = build_measured_value(
        value_id="g",
        error_components=[
            {"name": "stat", "error": "0.2"},
            {"name": "sys", "error": "0.1"},
        ],
        **common,
    )
    side_swapped = build_measured_value(
        value_id="p1",
        error_components=[
            {"name": "stat", "lower_error": "0.1", "upper_error": "0.2"},
            {"name": "sys", "error": "0.1"},
        ],
        **common,
    )
    missing = build_measured_value(
        value_id="p2",
        error_components=[{"name": "stat", "error": "0.2"}],
        **common,
    )
    verdict, diff, _ = compare_content(gold, side_swapped)
    assert verdict == "different"
    assert "error_components.stat.error" in diff
    verdict, diff, _ = compare_content(gold, missing)
    assert verdict == "different"
    assert any(field.startswith("error_components(") for field in diff)
