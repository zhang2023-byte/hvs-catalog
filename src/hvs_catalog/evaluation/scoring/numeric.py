"""Compare current value records using exact or approved formatting matches."""

import re

from ...records.common import sha256_of
from ...records.literature_contribution import LiteratureValue
from .printed import (
    _number_form,
    compare_number,
    compare_unit,
    is_supported_number_syntax,
    limit_operand,
    normalize_printed,
    percent_operand,
    printed_limit_inclusive,
    split_error_number,
    split_unit_roles,
)

NUMERIC_SCHEME = {
    "version": "10",
    "contract": "LiteratureValue; current eleven quantity identifiers",
    "matching": "exact strings or approved limited formatting; all other pairs differ",
    "objects": "unique paper-local identity from identifiers and explicit aliases; ambiguous predictions do not match; no coordinate, value or external lookup",
    "pairing": "maximum complete one-to-one matches per identity and quantity; remaining records paired by original order for diagnostics only",
    "content": "value, error, lower_error, upper_error, lower_bound, upper_bound, lower_inclusive, upper_inclusive, unit",
    "formatting": "retain precision, trailing zeros, approximation, unit scale, error form and bound direction/inclusion; approved Unicode/LaTeX notation, single-digit integer spacing, unit roles and inline error units",
    "qualifiers": "retain existing approved literal numeric declarations in condition; no semantic condition comparison",
    "denominators": "all planned reference and retained prediction values, including missed and extra stars; duplicates consume each reference at most once",
    "supplements": "missing/extra identifiers and contribution type on matched stars",
    "excluded": "semantic evaluation of condition, evidence and notes; no model, conversion, tolerance, unknown verdict or provisional score",
}


def scheme_reference() -> dict:
    return {"version": NUMERIC_SCHEME["version"], "sha256": sha256_of(NUMERIC_SCHEME)}


_ATOM = (
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"(?!\w|\.\d)(?!\s*(?:[×*^:+/]|-\s*\d|x\s*\d|\\times))"
)
_INEQUALITY = re.compile(r"(?P<operator><=|>=|[<>≤≥])\s*(?P<number>" + _ATOM + ")")
_APPROXIMATE = re.compile(
    r"(?:\babout\s+|\bapproximately\s+|[~∼≈≃]\s*)(?P<number>" + _ATOM + ")",
    re.IGNORECASE,
)
NUMBER_FIELDS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "lower_bound",
    "upper_bound",
)


def _content(value: LiteratureValue, quantity: str) -> dict:
    """Interpret only approved explicit formatting, without changing the record."""
    result = {
        name: getattr(value, name)
        for name in (*NUMBER_FIELDS, "unit", "lower_inclusive", "upper_inclusive")
    }
    unit = result["unit"]
    if unit is not None and not unit.strip():
        unit = None
    if (
        quantity in {"bound_probability", "unbound_probability"}
        and unit
        and unit.strip()
        in {
            "dimensionless",
            "dimensionless (probability)",
            "dimensionless (fraction)",
            "(dimensionless)",
            "probability",
            "probability (fraction)",
            "probability (dimensionless)",
            "fraction",
            "1",
            "—",
            "-",
        }
    ):
        unit = None
    coordinate_units = {
        "ra": {"hms", "h:m:s", "h m s", "hh:mm:ss"},
        "dec": {
            "dms",
            "d:m:s",
            "d m s",
            "dd:mm:ss",
            "deg:m:s",
            "deg:arcmin:arcsec",
            "deg arcmin arcsec",
        },
    }.get(quantity, set())
    normalized = normalize_printed(value.value) if value.value is not None else None
    if (
        unit
        and unit.strip() in coordinate_units
        and normalized
        and ":" in normalized
        and is_supported_number_syntax(normalized.replace(" ", ""))
        and not any((value.error, value.lower_error, value.upper_error))
    ):
        unit = None
    result["unit"] = unit
    condition = value.condition or ""
    if value.value is not None:
        mentions = any(
            compare_number(m["number"], value.value) == "equivalent"
            for m in _APPROXIMATE.finditer(condition)
        )
        if (
            (re.match(r"^Approximate\s", condition) or mentions)
            and normalized is not None
            and not normalized.startswith(("about ", "approximately ", "∼", "≈", "≃"))
        ):
            result["value"] = "about " + value.value
    for direction in ("lower", "upper"):
        field, flag = direction + "_bound", direction + "_inclusive"
        raw = result[field]
        if raw is None:
            continue
        kind = direction + "_limit"
        if result[flag] is None:
            hints = set()
            printed = printed_limit_inclusive(raw, kind)
            if printed is not None:
                hints.add(printed)
            declaration = re.match(
                r"^(Strict|Inclusive) (upper|lower) (?:bound|limit)\b", condition
            )
            if declaration and declaration[2] == direction:
                hints.add(declaration[1] == "Inclusive")
            for mention in _INEQUALITY.finditer(condition):
                if (
                    compare_number(mention["number"], limit_operand(raw, kind))
                    != "equivalent"
                ):
                    continue
                if (
                    mention["operator"].startswith("<") or mention["operator"] == "≤"
                ) == (direction == "upper"):
                    hints.add(mention["operator"] in {"<=", ">=", "≤", "≥"})
            if len(hints) == 1:
                result[flag] = hints.pop()
        result[field] = limit_operand(raw, kind, inclusive=result[flag])
    return result


def _same(left, right, comparator) -> bool:
    if left is None or right is None:
        return left is right
    return comparator(left, right) == "equivalent"


def value_differences(
    gold: LiteratureValue, prediction: LiteratureValue, quantity: str
) -> list[str]:
    """Return differing numerical fields. An empty list is one complete match."""
    left, right = _content(gold, quantity), _content(prediction, quantity)
    differences = []
    gold_unit, gold_error_unit = split_unit_roles(left["unit"])
    pred_unit, pred_error_unit = split_unit_roles(right["unit"])
    if not _same(gold_unit, pred_unit, compare_unit):
        differences.append("unit")
    if not any(
        left[n] is not None or right[n] is not None
        for n in ("error", "lower_error", "upper_error")
    ) and not _same(gold_error_unit, pred_error_unit, compare_unit):
        differences.append("unit")
    for name in NUMBER_FIELDS:
        a, b = left[name], right[name]
        if a is None or b is None:
            if a is not b:
                differences.append(name)
            continue
        a_unit, b_unit = gold_unit, pred_unit
        if "error" in name:
            a, a_unit = split_error_number(
                a, gold_error_unit, symmetric=name == "error"
            )
            b, b_unit = split_error_number(
                b, pred_error_unit, symmetric=name == "error"
            )
            sign = {"lower_error": "-", "upper_error": "+"}.get(name)
            if sign:
                a = a.removeprefix(sign).strip() if _number_form(a) is not None else a
                b = b.removeprefix(sign).strip() if _number_form(b) is not None else b
            if not _same(a_unit, b_unit, compare_unit):
                differences.append(name + ".unit")
        a, b = percent_operand(a, a_unit), percent_operand(b, b_unit)
        if compare_number(a, b) != "equivalent":
            differences.append(name)
    for name in ("lower_inclusive", "upper_inclusive"):
        if left[name] != right[name]:
            differences.append(name)
    return sorted(set(differences))
