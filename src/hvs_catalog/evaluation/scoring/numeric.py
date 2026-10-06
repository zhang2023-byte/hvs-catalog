"""Approved program-only numerical comparison, independent of attribution wording.

Maximum complete matches determine C. Residual one-to-one pairings explain component
differences without changing C/P/G. Unsupported expressions cannot become correct
through a model judgment. Historical synthetic attribution protocols remain readable
separately; new scoring plans explicitly select this scheme.
"""

from ...records.common import sha256_of
from .agent import AuxiliaryScoringConfig
from .layers import QuantityPairing, ValueSide, _max_matching, compare_content
from .models import InputReference
from .results import PairingDetail

NUMERIC_SCHEME = {
    "version": "1",
    "objects": "paper-local identifiers and explicit aliases; NFC, minus, whitespace",
    "pairing": "maximum complete numeric matching within matched object and quantity",
    "residual_pairing": "maximum undecidable pairs, then definite differences, sorted refs",
    "content": "value, errors/components, unit, limits, intervals/endpoints, coverage/quantiles",
    "formatting": "approved restricted printed comparison; precision and trailing zeros retained",
    "attribution": "condition/source/frame/epoch/evidence retained, not compared",
    "denominators": "all in-scope predictions and Gold, including extra/missing objects",
    "unknown": "provisional when possible matches exceed definite matches; final rates null",
    "auxiliary": "disabled; no models, unit conversion, derived values, or tolerance",
}


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
        (g, p): compare_content(gold.measured, pred.measured)
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
