"""Independent synthetic formatting cases preserving numerical/scientific distinctions."""

import copy

import pytest
from test_numeric_scoring import bundle

from hvs_catalog.evaluation.scoring import compare_number, compare_unit, score_batch
from hvs_catalog.evaluation.scoring.builders import build_measured_value
from hvs_catalog.evaluation.scoring.layers import compare_content
from hvs_catalog.evaluation.scoring.numeric import numeric_content


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("07:08:09.10", "07 : 08 : 09.10", "equivalent"),
        ("−18:12:34.50", "-18:12:34.50", "equivalent"),
        ("07:08:09.10", "07:08:09.1", "different"),
        ("07:68:09.10", "07:68:09.10", "undecidable"),
        ("07:08:69.10", "07:08:69.10", "undecidable"),
        ("07:08:09.10", "7.13586", "different"),
        ("about 42", "∼42", "equivalent"),
        ("about 42", "≃42", "equivalent"),
        ("42", "≃42", "different"),
        ("about 42.0", "≃42", "different"),
        ("about 42", "≃43", "different"),
        ("about forty", "≃forty", "undecidable"),
        ("about 42", "~42", "equivalent"),
        ("42", "~42", "different"),
        ("42", "about 42", "different"),
        ("42", "∼42", "different"),
        ("about 42", "about 42.0", "different"),
        ("about forty", "forty", "undecidable"),
        ("± 0.7", "0.7", "undecidable"),
        (r"\mystery{42}", "42", "undecidable"),
        ("2*10^{3}", r"2*10\^{3}", "undecidable"),
        ("28", "2 8", "equivalent"),
        ("−28", "-2 8", "equivalent"),
        ("+28", "+2 8", "equivalent"),
        ("280", "2 8 0", "equivalent"),
        ("about 28", "∼2 8", "equivalent"),
        ("28", "about 2 8", "different"),
        ("28.0", "2 8", "different"),
        ("280", "2 8", "different"),
        ("2.8", "2 8", "different"),
        ("-28", "2 8", "different"),
        ("028", "2 8", "different"),
        ("28", "2, 8", "undecidable"),
        ("28", "(2 8)", "undecidable"),
        ("283", "28 3", "undecidable"),
        ("2834", "28 34", "undecidable"),
        ("2834", "2 834", "undecidable"),
        ("28.00", "2 8.0 0", "undecidable"),
        ("28e+3", "2 8e+3", "undecidable"),
        ("29", "2 8 + 1", "undecidable"),
    ],
)
def test_number_format_boundaries(left, right, expected):
    assert compare_number(left, right) == expected


@pytest.mark.parametrize(
    "label",
    [
        "probability",
        "probability (fraction)",
        "probability (dimensionless)",
        "dimensionless (fraction)",
        "(dimensionless)",
    ],
)
def test_probability_label_spellings_are_context_limited(label):
    left = build_measured_value(value_id="g", value="0.37", condition="Synthetic")
    right = left.model_copy(update={"value_id": "p", "unit": label})
    path = "bound_assessment.unbound_probability"
    assert (
        compare_content(
            numeric_content(left, quantity_path=path),
            numeric_content(right, quantity_path=path),
        )[0]
        == "equivalent"
    )
    assert (
        compare_content(
            numeric_content(left, quantity_path="observed_phase_space.distance"),
            numeric_content(right, quantity_path="observed_phase_space.distance"),
        )[0]
        == "different"
    )
    right = right.model_copy(update={"value": "37", "unit": "percent"})
    assert (
        compare_content(
            numeric_content(left, quantity_path=path),
            numeric_content(right, quantity_path=path),
        )[0]
        == "different"
    )


@pytest.mark.parametrize(
    ("path", "labels"),
    [
        ("observed_phase_space.ra", ["h:m:s", "h m s", "hh:mm:ss"]),
        (
            "observed_phase_space.dec",
            ["d:m:s", "deg:m:s", "deg:arcmin:arcsec", "deg arcmin arcsec"],
        ),
    ],
)
def test_coordinate_label_spellings_keep_errors_units_and_precision(path, labels):
    left = build_measured_value(
        value_id="g", value="12:34:56.70", condition="Synthetic"
    )
    for label in labels:
        right = left.model_copy(update={"value_id": "p", "unit": label})
        compare = lambda a, b: compare_content(
            numeric_content(a, quantity_path=path),
            numeric_content(b, quantity_path=path),
        )[0]
        assert compare(left, right) == "equivalent"
        assert (
            compare(left, right.model_copy(update={"value": "12:34:56.7"}))
            == "different"
        )
        assert (
            compare(
                left.model_copy(update={"error": "0.1"}),
                right.model_copy(update={"error": "0.1"}),
            )
            == "different"
        )
    wrong = "d:m:s" if path.endswith("ra") else "h:m:s"
    assert compare(left, left.model_copy(update={"unit": wrong})) == "different"
    assert compare(left, left.model_copy(update={"unit": "deg"})) == "different"


def test_percent_word_is_same_scale_in_values_and_statistical_reports():
    assert compare_unit("percent", "%") == "equivalent"
    assert compare_unit("per cent", "%") == "equivalent"
    left = build_measured_value(
        value_id="g", value="37.0", unit="%", condition="Synthetic"
    )
    right = left.model_copy(
        update={"value_id": "p", "value": "37.0%", "unit": "percent"}
    )
    assert compare_content(left, right)[0] == "equivalent"
    assert (
        compare_content(left, right.model_copy(update={"value": "37%"}))[0]
        == "different"
    )
    assert (
        compare_content(
            left, right.model_copy(update={"value": "0.370", "unit": None})
        )[0]
        == "different"
    )


@pytest.mark.parametrize(
    ("scalar", "expression", "expected"),
    [
        ("7.50", "7.50 - b", "different"),
        ("7.50", "7.50−b", "different"),
        ("7.50", "7.50 + offset_2", "different"),
        ("7.50", "7.50e-3 - b", "different"),
        ("7.50", "-7.50 - b", "different"),
        ("7.50", "7.50 - b - c", "undecidable"),
        ("7.50", "7.50 -", "undecidable"),
        ("7.50", "7.50 - 0", "undecidable"),
        ("7.50", r"7.50 - \\mystery{b}", "undecidable"),
        ("7.50 - b", "7.50 - b", "undecidable"),
        ("7.50 - b", "7.50 - c", "undecidable"),
    ],
)
def test_symbolic_offset_is_distinct_from_scalar_without_evaluation(
    scalar, expression, expected
):
    assert compare_number(scalar, expression) == expected
    assert compare_number(expression, scalar) == expected


def test_symbolic_offset_prediction_has_final_mismatch_and_preserves_inputs():
    plan, gold, predictions = bundle(
        [{"value": "7.50", "error": "0.20", "unit": "mas"}],
        [{"value": "7.50 - b", "error": "0.20", "unit": "mas"}],
    )
    before = copy.deepcopy([item.model_dump(mode="json") for item in predictions])
    report = score_batch(plan, gold, predictions)
    assert not report.batch_provisional
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (
        0,
        1,
        1,
    )
    assert report.micro.l2.f1 == 0
    assert [item.model_dump(mode="json") for item in predictions] == before


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("deg", "°", "equivalent"),
        ("km/s", "km s⁻¹", "equivalent"),
        ("mas/yr", "mas yr^{-1}", "equivalent"),
        ("km s^-1", "s^{-1} km", "equivalent"),
        ("km/s", "m/s", "different"),
        ("deg", "rad", "different"),
        ("mas/yr", "arcsec/yr", "different"),
        ("km s^{-1}", "km s^{-2}", "different"),
        ("deg; error mas", "deg", "different"),
        ("deg; error mas", "deg ± mas", "equivalent"),
        ("deg; error mas", "deg ± arcsec", "different"),
        ("deg; error mas", "mas ± deg", "different"),
        ("deg; error mas", "deg ± deg", "different"),
        ("deg", "deg ± mas", "different"),
        ("deg; error mas", "deg ± mas ± arcsec", "undecidable"),
        ("km/s", r"km s\^{-1}", "equivalent"),
        ("km/s", r"m s\^{-1}", "different"),
        ("km/s", r"km s\^{-2}", "different"),
        ("km/s", r"km s\^{-}", "undecidable"),
        ("km/s", r"km s\^{-1", "undecidable"),
        ("km//s", "km/s", "undecidable"),
        (r"\mysteryunit", "km/s", "undecidable"),
    ],
)
def test_unit_notation_without_scale_conversion(left, right, expected):
    assert compare_unit(left, right) == expected


@pytest.mark.parametrize(
    ("gold", "prediction", "expected"),
    [
        (
            {"value": "42", "unit": "%"},
            {"value": "42%", "unit": "%"},
            "equivalent",
        ),
        (
            {"value": "42", "unit": "%"},
            {"value": "0.42", "unit": None},
            "different",
        ),
        (
            {"value": "42", "unit": "%"},
            {"value": "about 42%", "unit": "%"},
            "different",
        ),
        ({"error": "0.7"}, {"error": "± 0.7"}, "equivalent"),
        (
            {"value": "28", "error": "57"},
            {"value": "2 8", "error": "5 7"},
            "equivalent",
        ),
        ({"value": "28", "error": "57"}, {"value": "2 8", "error": "5 8"}, "different"),
        ({"value": "28", "error": "57"}, {"value": "2 8", "error": "5.7"}, "different"),
        (
            {"lower_error": "0.2", "upper_error": "0.4"},
            {"lower_error": "−0.2", "upper_error": "+0.4"},
            "equivalent",
        ),
        ({"lower_error": "0.2"}, {"lower_error": "+0.2"}, "different"),
        ({"upper_error": "0.4"}, {"upper_error": "-0.4"}, "different"),
        ({"lower_error": "0.20"}, {"lower_error": "-0.2"}, "different"),
        ({"value": "-3.5"}, {"value": "3.5"}, "different"),
        ({"error": "0.70"}, {"error": "± 0.7"}, "different"),
        (
            {"unit": "deg; error mas", "error": "0.025"},
            {"unit": "°", "error": "0.025 mas"},
            "equivalent",
        ),
        (
            {"unit": "deg; error mas", "error": "0.025"},
            {"unit": "deg", "error": "0.025"},
            "different",
        ),
        (
            {"unit": "deg; error mas", "error": "0.025"},
            {"unit": "deg", "error": "0.025 arcsec"},
            "different",
        ),
        (
            {"unit": "deg; error mas", "error": "0.025"},
            {"unit": "deg ± mas", "error": "± 0.025"},
            "equivalent",
        ),
        (
            {"unit": "deg; error mas", "error": "0.025"},
            {"unit": "deg ± arcsec", "error": "0.025"},
            "different",
        ),
        (
            {"unit": "km/s", "lower_error": "0.2", "upper_error": "0.4"},
            {"unit": r"km s\^{-1}", "lower_error": "-0.2", "upper_error": "+0.4"},
            "equivalent",
        ),
        (
            {"lower_error": "0.2", "upper_error": "0.4"},
            {"error": "± 0.3"},
            "different",
        ),
        (
            {"value": "3.5", "limit_kind": "upper_limit"},
            {"value": "< 3.5", "limit_kind": "upper_limit"},
            "equivalent",
        ),
        (
            {"value": "3.5"},
            {"value": "< 3.5", "limit_kind": "upper_limit"},
            "different",
        ),
        (
            {"value": "3.5", "limit_kind": "upper_limit", "upper_inclusive": False},
            {"value": "≤ 3.5", "limit_kind": "upper_limit", "upper_inclusive": True},
            "different",
        ),
        (
            {"value": "< 3.5", "limit_kind": "upper_limit"},
            {"value": "≤ 3.5", "limit_kind": "upper_limit"},
            "different",
        ),
        (
            {"value": "> 3.5", "limit_kind": "lower_limit"},
            {"value": "≥ 3.5", "limit_kind": "lower_limit"},
            "different",
        ),
    ],
)
def test_structured_measurements_keep_units_errors_and_limits(
    gold, prediction, expected
):
    common = {"value": "3.5", "unit": "kpc", "condition": "Synthetic report"}
    left = build_measured_value(value_id="synthetic-g", **(common | gold))
    right = build_measured_value(value_id="synthetic-p", **(common | prediction))
    before = copy.deepcopy((left.model_dump(), right.model_dump()))
    assert compare_content(left, right)[0] == expected
    assert (left.model_dump(), right.model_dump()) == before


@pytest.mark.parametrize(
    ("gold", "prediction", "expected"),
    [
        ({"condition": "Approximate distance"}, {"value": "about 3.5"}, "equivalent"),
        ({"condition": "Approximate distance"}, {"value": "≃3.5"}, "equivalent"),
        (
            {"condition": "Approximate distance", "value": "≃3.5"},
            {"value": "about 3.5"},
            "equivalent",
        ),
        ({"condition": "Approximate distance"}, {"value": "3.5"}, "different"),
        (
            {"condition": "Distance from an approximate model"},
            {"value": "about 3.5"},
            "different",
        ),
        (
            {"condition": "Approximate distance", "value": "about 3.5"},
            {"value": "∼3.5"},
            "equivalent",
        ),
        ({"condition": "Approximate distance"}, {"value": "about 3.50"}, "different"),
        (
            {"limit_kind": "upper_limit", "condition": "Strict upper bound Q < 3.5"},
            {"limit_kind": "upper_limit", "upper_inclusive": False},
            "equivalent",
        ),
        (
            {
                "limit_kind": "lower_limit",
                "condition": "Inclusive lower limit Q >= 3.5",
            },
            {"limit_kind": "lower_limit", "lower_inclusive": True},
            "equivalent",
        ),
        (
            {"limit_kind": "upper_limit", "condition": "Strict upper bound Q < 3.5"},
            {"limit_kind": "upper_limit", "upper_inclusive": True},
            "different",
        ),
        (
            {"limit_kind": "upper_limit", "condition": "Selection uses Q < 3.5"},
            {"limit_kind": "upper_limit", "upper_inclusive": False},
            "equivalent",
        ),
        (
            {"limit_kind": "upper_limit", "condition": "Selection uses Q < 7.0"},
            {"limit_kind": "upper_limit", "upper_inclusive": False},
            "different",
        ),
        (
            {"limit_kind": "upper_limit", "condition": "Strict lower bound Q > 3.5"},
            {"limit_kind": "upper_limit", "upper_inclusive": False},
            "different",
        ),
        ({"unit": " "}, {"unit": None}, "equivalent"),
        ({"unit": ""}, {"unit": "kpc"}, "different"),
        (
            {"condition": "The velocity is about 3.5 km/s"},
            {"value": "∼3.5"},
            "equivalent",
        ),
        (
            {"condition": "The distance is about 2.0 kpc"},
            {"value": "∼3.5"},
            "different",
        ),
        ({"condition": "The velocity is about 3.5."}, {"value": "∼3.5"}, "equivalent"),
        ({"condition": "The velocity is ≃3.5."}, {"value": "∼3.5"}, "equivalent"),
        ({"condition": "The velocity is ≃3.50."}, {"value": "∼3.5"}, "different"),
        (
            {"condition": "The result is about 3.5 × 10^2"},
            {"value": "∼3.5"},
            "different",
        ),
        ({"condition": "The result is about 3.5/2"}, {"value": "∼3.5"}, "different"),
        (
            {"condition": "The velocity is about 3.50 km/s"},
            {"value": "3.5"},
            "equivalent",
        ),
    ],
)
def test_explicit_numeric_declarations_without_condition_semantics(
    gold, prediction, expected
):
    common = {"value": "3.5", "unit": "kpc", "condition": "Synthetic report"}
    left = build_measured_value(value_id="g", **(common | gold))
    right = build_measured_value(value_id="p", **(common | prediction))
    before = copy.deepcopy((left.model_dump(), right.model_dump()))
    assert compare_content(numeric_content(left), numeric_content(right))[0] == expected
    assert (left.model_dump(), right.model_dump()) == before


@pytest.mark.parametrize(
    ("path", "left", "right", "expected"),
    [
        (
            "bound_assessment.unbound_probability",
            {"value": "0.42", "unit": None},
            {"value": "0.42", "unit": "fraction"},
            "equivalent",
        ),
        (
            "bound_assessment.bound_probability",
            {"value": "0.42", "unit": None},
            {"value": "0.42", "unit": "dimensionless (probability)"},
            "equivalent",
        ),
        (
            "bound_assessment.unbound_probability",
            {"value": "0.42", "unit": None},
            {"value": "42", "unit": "%"},
            "different",
        ),
        (
            "observed_phase_space.distance",
            {"value": "0.42", "unit": None},
            {"value": "0.42", "unit": "fraction"},
            "different",
        ),
        (
            "observed_phase_space.ra",
            {"value": "07:08:09.10", "unit": "hms"},
            {"value": "07:08:09.10", "unit": None},
            "equivalent",
        ),
        (
            "observed_phase_space.dec",
            {"value": "-18:12:34.50", "unit": "dms"},
            {"value": "-18:12:34.50", "unit": None},
            "equivalent",
        ),
        (
            "observed_phase_space.ra",
            {"value": "07:08:09.10", "unit": "dms"},
            {"value": "07:08:09.10", "unit": None},
            "different",
        ),
        (
            "observed_phase_space.ra",
            {"value": "07:08:09.10", "unit": "hms", "error": "0.02"},
            {"value": "07:08:09.10", "unit": None, "error": "0.02"},
            "different",
        ),
    ],
)
def test_unit_format_labels_require_the_corresponding_quantity(
    path, left, right, expected
):
    common = {"condition": "Synthetic report"}
    gold = build_measured_value(value_id="g", **(common | left))
    prediction = build_measured_value(value_id="p", **(common | right))
    assert (
        compare_content(
            numeric_content(gold, quantity_path=path),
            numeric_content(prediction, quantity_path=path),
        )[0]
        == expected
    )


def test_error_components_and_numerical_matching_remain_one_to_one():
    gold = {"value": "3.5", "unit": "deg; error mas", "error": "0.025"}
    pred = {"value": "3.5", "unit": "°", "error": "± 0.025 mas"}
    plan, golds, predictions = bundle([gold], [pred, pred])
    report = score_batch(plan, golds, predictions)
    assert not report.batch_provisional
    assert (
        report.micro.l2.correct,
        report.micro.l2.predicted,
        report.micro.l2.gold,
    ) == (
        1,
        2,
        1,
    )
    assert report.micro.l2.f1 == pytest.approx(2 / 3)
    left = build_measured_value(
        value_id="g",
        value="3.5",
        unit="deg; error mas",
        condition="Synthetic",
        error_components=[{"name": "stat", "error": "0.025"}],
    )
    right = build_measured_value(
        value_id="p",
        value="3.5",
        unit="°",
        condition="Synthetic",
        error_components=[{"name": "stat", "error": "±0.025 mas"}],
    )
    assert compare_content(left, right)[0] == "equivalent"
