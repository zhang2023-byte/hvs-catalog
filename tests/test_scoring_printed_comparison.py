"""Restricted formatting/condition comparison boundaries (A1-A5, B verdict sources, U1/U2).

Use domain-specific compare_number/unit/condition. Missing the allowlist does not prove
difference: out-of-domain English, unsupported units, and unsupported condition changes
are undecidable. Missing/scaled units, numeric/exponent sign differences, and B7
statistical-structure differences remain definite. Frozen examples and regression tests
cover full score effects.
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
        (
            "8.2",
            "8200",
        ),  # B1 numeric side: correct conversion is still not formatting equivalence.
        ("600", "610"),  # B2: differing central values.
        ("8.2", "8.20"),  # Reverse C1: neither remove nor add trailing zeros.
        ("1.20\\times10^{3}", "1.20\\times10^{-3}"),  # Different exponent signs.
        ("-8.2", "8.2"),  # Different numeric signs.
        ("12h34m56.7s", "12h34m56.8s"),
    ],
)
def test_number_different_forms(gold, prediction):
    assert compare_number(gold, prediction) == "different"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("\\mystery{8.2}", "8.2"),  # U1: never strip unknown macros.
        ("8.2", "\\mystery{8.2}"),
        (
            "about eight",
            "eight",
        ),  # English differences outside the numeric domain cannot establish semantics.
        (
            "< 600",
            "≤ 600",
        ),  # Inclusion-symbol differences are statistical structure, judged through inclusive.
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
        ("kpc", "pc"),  # Different unit scales: B1 unit side.
        ("km s⁻¹", "km s⁻²"),  # Different exponents.
    ],
)
def test_unit_different_forms(gold, prediction):
    assert compare_unit(gold, prediction) == "different"


@pytest.mark.parametrize(
    ("gold", "prediction"),
    [
        ("km s⁻¹", "km/s"),  # Unsupported syntax cannot establish semantics.
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
    # A5: an unallowlisted phrase mentioning M1 proves neither equivalence nor difference.
    assert compare_condition(M1, "not assuming potential M1") == "different"
    assert compare_condition(M1, "under Galactic potential M1 and M2") == "undecidable"
    # B8/U2: explicit M1 and M2 differ; missing qualifiers remain undecidable.
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
    # An allowlist miss cannot prove difference, despite the previous different encoding.
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
