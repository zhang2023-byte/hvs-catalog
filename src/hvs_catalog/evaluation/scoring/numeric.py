"""Approved program-only numerical comparison, independent of attribution wording.

Maximum complete matches determine C. Residual one-to-one pairings explain component
differences without changing C/P/G. Unsupported expressions cannot become correct
through a model judgment. Historical synthetic attribution protocols remain readable
separately; new scoring plans explicitly select this scheme.
"""

import re

from ...records.common import sha256_of
from ...records.paper import MeasuredValue
from .agent import AuxiliaryScoringConfig
from .layers import QuantityPairing, ValueSide, _max_matching, compare_content
from .models import InputReference
from .printed import (
    compare_number,
    is_supported_number_syntax,
    limit_operand,
    normalize_printed,
    printed_limit_inclusive,
)
from .results import PairingDetail

NUMERIC_SCHEME = {
    "version": "8",
    "symbolic_offsets": "a complete scalar plus/minus one unresolved ASCII identifier is structurally different from a supported scalar; no formula equivalence, substitution or arithmetic; other unsupported syntax remains undecidable",
    "integer_digit_spacing": "uniform single-digit spacing in one complete scalar integer; preserve signs, all digits and approximation; no grouping, list, decimal or exponent-fragment concatenation",
    "objects": "paper-local identifiers and explicit aliases; NFC, Unicode minus/en dash, whitespace; retain explicit Gaia release/ID-column and HVS-column namespaces from the identifier's own saved Gold evidence; optional Gaia Source ID label and anchored a.k.a. declarations; never infer from row order, coordinates or values",
    "pairing": "maximum complete numeric matching within matched object and quantity",
    "residual_pairing": "maximum undecidable pairs, then definite differences, sorted refs",
    "content": "value, errors/components, unit, limits, intervals/endpoints, coverage/quantiles",
    "formatting": "approved restricted printed comparison; precision and trailing zeros retained",
    "coordinates": "identical colon sexagesimal syntax; no coordinate conversion",
    "units": "same-scale degree symbols and monomial slash/inverse notation, including a redundant escaped caret before a complete integer unit power; explicit value/error-unit roles separated by '; error' or plus/minus",
    "errors": "redundant symmetric plus/minus and direction-consistent asymmetric signs; explicitly printed inline units",
    "qualifiers": "approximation retained, including leading ASCII tilde and Unicode ∼/≈/≃, anchored Approximate declarations and literal approximate mentions of the same numeric operand; missing endpoint flags read only from matching literal inequalities or anchored Strict/Inclusive upper/lower bound declarations",
    "empty_units": "empty or whitespace unit is absent, never a wildcard for a physical unit",
    "format_units": "explicit dimensionless/probability/fraction label spellings and corresponding hms/dms or colon/space-separated sexagesimal labels read only within their quantity paths; no scale conversion; sexagesimal errors retain units",
    "percent": "percent/per cent and % are the same unit; redundant printed percent mark only when stored unit is percent; no probability conversion",
    "attribution": "condition/source/frame/epoch/evidence retained, not semantically compared; only explicit numerical qualifier declarations/expressions with the identical operand read",
    "denominators": "all in-scope predictions and Gold, including extra/missing objects",
    "unknown": "provisional when possible matches exceed definite matches; final rates null",
    "auxiliary": "disabled; no models, unit conversion, derived values, or tolerance",
}

_ATOM = (
    r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    r"(?!\w|\.\d)(?!\s*(?:[×*^:+/]|-\s*\d|x\s*\d|\\times))"
)
_INEQUALITY = re.compile(r"(?P<operator><=|>=|[<>≤≥])\s*(?P<number>" + _ATOM + ")")
_APPROXIMATE_MENTION = re.compile(
    r"(?:\babout\s+|\bapproximately\s+|[~∼≈≃]\s*)(?P<number>" + _ATOM + ")",
    re.IGNORECASE,
)


def numeric_content(
    value: MeasuredValue, *, quantity_path: str | None = None
) -> MeasuredValue:
    """Read explicit numerical declarations without rewriting stored answers.

    No free-text semantic inference: declarations or literal expressions must
    explicitly qualify the same operand, at unchanged printed precision.
    Unspecified endpoints stay unspecified; an explicit existing flag wins.
    """
    updates = {}
    if value.unit is not None and not value.unit.strip():
        updates["unit"] = None
    normalized = normalize_printed(value.value) if value.value is not None else None
    if (
        quantity_path
        in {
            "bound_assessment.bound_probability",
            "bound_assessment.unbound_probability",
        }
        and value.unit is not None
        and value.unit.strip()
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
        updates["unit"] = None
    coordinate_units = {
        "observed_phase_space.ra": {"hms", "h:m:s", "h m s", "hh:mm:ss"},
        "observed_phase_space.dec": {
            "dms",
            "d:m:s",
            "d m s",
            "dd:mm:ss",
            "deg:m:s",
            "deg:arcmin:arcsec",
            "deg arcmin arcsec",
        },
    }.get(quantity_path, set())
    if (
        value.unit is not None
        and value.unit.strip() in coordinate_units
        and normalized
        and ":" in normalized
        and is_supported_number_syntax(normalized.replace(" ", ""))
        and not any(
            (value.error, value.lower_error, value.upper_error, value.error_components)
        )
    ):
        updates["unit"] = None
    mentions = [
        m
        for m in _APPROXIMATE_MENTION.finditer(value.condition)
        if value.value is not None
        and compare_number(m["number"], value.value) == "equivalent"
    ]
    if (
        (re.match(r"^Approximate\s", value.condition) or mentions)
        and normalized is not None
        and not normalized.startswith(
            ("approx ", "about ", "approximately ", "∼", "≈", "≃")
        )
    ):
        updates["value"] = "about " + value.value
    direction = {"upper_limit": "upper", "lower_limit": "lower"}.get(value.limit_kind)
    if direction:
        field = direction + "_inclusive"
        if getattr(value, field) is None:
            hints = set()
            inclusive = printed_limit_inclusive(value.value, value.limit_kind)
            if inclusive is not None:
                hints.add(inclusive)
            declaration = re.match(
                r"^(Strict|Inclusive) (upper|lower) (?:bound|limit)\b", value.condition
            )
            if declaration and declaration[2] == direction:
                hints.add(declaration[1] == "Inclusive")
            operand = limit_operand(value.value, value.limit_kind)
            for mention in _INEQUALITY.finditer(value.condition):
                if compare_number(mention["number"], operand) != "equivalent":
                    continue
                if (
                    mention["operator"].startswith("<") or mention["operator"] == "≤"
                ) != (direction == "upper"):
                    continue
                hints.add(mention["operator"] in {"<=", ">=", "≤", "≥"})
            if len(hints) == 1:
                updates[field] = hints.pop()
    return value.model_copy(update=updates)


def numeric_scoring_config() -> AuxiliaryScoringConfig:
    """Bind the explicit absence of auxiliary model evaluation."""
    digest = sha256_of({"numeric_scoring": "no_auxiliary"})
    return AuxiliaryScoringConfig(
        mode="disabled",
        model_config_digest=digest,
        prompt_digest=digest,
        params_digest=digest,
        budget_digest=digest,
    )


def numeric_scheme_reference() -> InputReference:
    return InputReference(
        name="program-numeric-scoring",
        version=NUMERIC_SCHEME["version"],
        fingerprint=sha256_of(NUMERIC_SCHEME),
    )


def pair_numeric_values(
    gold_sides: list[ValueSide], pred_sides: list[ValueSide]
) -> QuantityPairing:
    """Consume every final Gold/prediction pair at most once; IDs only break ties."""
    golds = sorted(gold_sides, key=lambda item: item.ref)
    preds = sorted(pred_sides, key=lambda item: item.ref)
    result = QuantityPairing(predicted=len(preds), gold=len(golds))
    comparisons = {
        (g, p): compare_content(
            numeric_content(gold.measured, quantity_path=gold.quantity_path),
            numeric_content(pred.measured, quantity_path=pred.quantity_path),
        )
        for g, gold in enumerate(golds)
        for p, pred in enumerate(preds)
    }
    equivalent = {
        key for key, (verdict, _, _) in comparisons.items() if verdict == "equivalent"
    }
    unknown = {
        key for key, (verdict, _, _) in comparisons.items() if verdict == "undecidable"
    }
    matched = _max_matching(equivalent, len(golds), len(preds))
    result.matched = len(matched)
    if len(_max_matching(equivalent | unknown, len(golds), len(preds))) > len(matched):
        result.reasons.append("unknown_numeric_expression")
    used_gold: set[int] = set()
    used_pred: set[int] = set()

    def record(g: int, p: int, status: str, fields: list[str]) -> None:
        used_gold.add(g)
        used_pred.add(p)
        result.pairings.append(
            PairingDetail(
                layer="l2",
                status=status,
                gold_ref=golds[g].ref,
                prediction_ref=preds[p].ref,
                fields=sorted(fields),
            )
        )

    for g, p in matched:
        record(g, p, "matched", [])
        result.matched_gold_refs.add(golds[g].ref)
    for verdict, status in (
        ("undecidable", "unknown_relation"),
        ("different", "value_mismatch"),
    ):
        edges = {
            key
            for key, comparison in comparisons.items()
            if comparison[0] == verdict
            and key[0] not in used_gold
            and key[1] not in used_pred
        }
        for g, p in _max_matching(edges, len(golds), len(preds)):
            _, differences, undecided = comparisons[(g, p)]
            record(g, p, status, undecided if verdict == "undecidable" else differences)
    result.pairings.extend(
        PairingDetail(layer="l2", status="omitted_value", gold_ref=gold.ref)
        for g, gold in enumerate(golds)
        if g not in used_gold
    )
    result.pairings.extend(
        PairingDetail(layer="l2", status="extra_value", prediction_ref=pred.ref)
        for p, pred in enumerate(preds)
        if p not in used_pred
    )
    return result
