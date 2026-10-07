"""Layered matching engine: L1 one-to-one objects and L2 attribution/value pairs.

L1 uses restricted normalization of paper-local identifiers/explicit aliases. Duplicate
definite predictions consume one TP, with remaining FP. Shared-alias identity ambiguity
remains pending separately from definite FP/FN, without value-based disambiguation or
default errors.

L2 builds the full relation graph per object/quantity. First compare quantity_path,
condition, source/source_note, frame, epoch, coordinate_format, representation, purpose,
and author_usage. different forbids edges; equivalent permits domain-specific content
comparisons; unknown attribution, including insufficient evidence/technical failure,
stays possible. Equal values never establish cross-condition correctness. Content keys
certify safe equivalence only; different keys still use three-state domain checks.
Unsupported expressions are unknown, not proven different. Maximum one-to-one matching
of definite edges gives C; possible edges bound uncertainty, and attribution-unknown
edges that may change correspondence remain unresolved. Reordering/renaming internal IDs
cannot change C/P/G. Null units are not wildcards; named components match by name
without removing missing components.

Only undecidable formatting/condition relations use injected resolvers, with both sides
providing material-bound fragments declared for the field. Established program/Agent
differences cannot be overturned and become definite mismatch diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hvs_catalog.records.paper import MeasuredValue

from .agent import ScoringAgentResolver
from .identifiers import identifier_keys
from .models import EvidenceFragment, GoldObjectEntry, PredictionObjectEntry
from .printed import (
    Verdict,
    compare_condition,
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
from .results import PairingDetail

ATTRIBUTION_FIELDS = (
    "source",
    "source_note",
    "frame",
    "epoch",
    "coordinate_format",
    "representation",
    "purpose",
    "author_usage",
)
NUMBER_CONTENT_FIELDS = (
    "value",
    "error",
    "lower_error",
    "upper_error",
    "range_lower",
    "range_upper",
)
UNIT_CONTENT_FIELDS = ("unit",)
REPORT_CONTENT_FIELDS = ("coverage", "lower_quantile", "upper_quantile")
COMPONENT_FIELDS = ("error", "lower_error", "upper_error")


def _attribution_summary(value: MeasuredValue) -> str:
    parts = [f"condition={value.condition}", f"source={value.source}"]
    for name in ATTRIBUTION_FIELDS:
        if name == "source":
            continue
        raw = getattr(value, name)
        if raw is not None:
            parts.append(f"{name}={raw}")
    return "; ".join(parts)


def compare_attribution(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[Verdict, list[str]]:
    """Compare attribution: exact non-condition fields and restricted condition-phrase
    comparison.
    """
    mismatches = [
        name
        for name in ATTRIBUTION_FIELDS
        if getattr(gold, name) != getattr(prediction, name)
    ]
    if mismatches:
        return "different", mismatches
    verdict = compare_condition(gold.condition, prediction.condition)
    return verdict, (["condition"] if verdict == "different" else [])


def content_key(value: MeasuredValue) -> tuple | None:
    """Content key for complete scientific fields with restricted normalized, valid syntax;
    otherwise None.

    Equal keys imply safe equivalence. Unknown macros or unsupported/malformed numeric
    syntax prevent keys; never certify malformed expressions through shortcuts.
    Different keys do not prove difference; compare_content judges domains.
    """

    def _normalized_number(raw: str) -> str | None:
        normalized = normalize_printed(raw)
        if normalized is None or not is_supported_number_syntax(normalized):
            return None
        return normalized

    parts: list = []
    for name in (*NUMBER_CONTENT_FIELDS, *UNIT_CONTENT_FIELDS):
        raw = getattr(value, name)
        if raw is None:
            parts.append(None)
            continue
        if name in UNIT_CONTENT_FIELDS:
            normalized = normalize_printed(raw)
        else:
            normalized = _normalized_number(raw)
        if normalized is None:
            return None
        parts.append(normalized)
    parts.extend(
        [
            value.limit_kind,
            value.interval_kind,
            value.lower_inclusive,
            value.upper_inclusive,
        ]
    )
    for report_field in REPORT_CONTENT_FIELDS:
        report = getattr(value, report_field)
        if report is None:
            parts.append(None)
            continue
        value_norm = _normalized_number(report.value)
        unit_norm = normalize_printed(report.unit) if report.unit is not None else None
        if value_norm is None or (report.unit is not None and unit_norm is None):
            return None
        parts.append((value_norm, unit_norm))
    components = []
    for component in sorted(value.error_components, key=lambda item: item.name):
        entry: list = [
            component.name,
            component.error is not None,
            component.lower_error is not None,
            component.upper_error is not None,
        ]
        for name in COMPONENT_FIELDS:
            raw = getattr(component, name)
            if raw is None:
                entry.append(None)
                continue
            normalized = _normalized_number(raw)
            if normalized is None:
                return None
            entry.append(normalized)
        components.append(tuple(entry))
    parts.append(tuple(components))
    return tuple(parts)


def _compare_number_field(
    field_name: str, left: str, right: str, diff: list[str], undecided: list[str]
) -> None:
    verdict = compare_number(left, right)
    if verdict == "different":
        diff.append(field_name)
    elif verdict == "undecidable":
        undecided.append(field_name)


def _compare_unit_field(
    field_name: str, left: str, right: str, diff: list[str], undecided: list[str]
) -> None:
    verdict = compare_unit(left, right)
    if verdict == "different":
        diff.append(field_name)
    elif verdict == "undecidable":
        undecided.append(field_name)


def _compare_error_field(
    field_name,
    left,
    right,
    left_unit,
    right_unit,
    diff,
    undecided,
    *,
    compare_units=False,
):
    inherited_units = left_unit, right_unit
    symmetric = field_name.split(".")[-1] == "error"
    left, left_unit = split_error_number(left, left_unit, symmetric=symmetric)
    right, right_unit = split_error_number(right, right_unit, symmetric=symmetric)
    direction = {"lower_error": "-", "upper_error": "+"}.get(field_name.split(".")[-1])
    if direction:
        # The field already states the direction. Strip only its redundant sign,
        # never the opposite sign, a central-value sign, or an exponent sign.
        def magnitude(raw):
            normalized = normalize_printed(raw)
            if normalized and normalized.startswith(direction):
                operand = normalized[1:].strip()
                if operand and not operand.startswith(("+", "-")):
                    return operand
            return raw

        left, right = magnitude(left), magnitude(right)
    _compare_number_field(field_name, left, right, diff, undecided)
    if not compare_units and inherited_units == (left_unit, right_unit):
        return
    if (left_unit is None) != (right_unit is None):
        diff.append(field_name)
    elif left_unit is not None:
        _compare_unit_field(field_name, left_unit, right_unit, diff, undecided)


def _compare_components(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[list[str], list[str]]:
    gold_names = {component.name for component in gold.error_components}
    pred_names = {component.name for component in prediction.error_components}
    if gold_names != pred_names:
        missing = sorted(gold_names - pred_names)
        extra = sorted(pred_names - gold_names)
        return [f"error_components(缺 {missing}，多 {extra})"], []
    diff: list[str] = []
    undecided: list[str] = []
    gold_base, gold_unit = split_unit_roles(gold.unit)
    pred_base, pred_unit = split_unit_roles(prediction.unit)
    pred_by_name = {c.name: c for c in prediction.error_components}
    for component in gold.error_components:
        other = pred_by_name[component.name]
        for name in COMPONENT_FIELDS:
            left = getattr(component, name)
            right = getattr(other, name)
            if (left is None) != (right is None):
                diff.append(f"error_components.{component.name}.{name}")
                continue
            if left is None:
                continue
            _compare_error_field(
                f"error_components.{component.name}.{name}",
                left,
                right,
                gold_unit,
                pred_unit,
                diff,
                undecided,
                compare_units=gold_unit != gold_base or pred_unit != pred_base,
            )
    return diff, undecided


def _compare_reports(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[list[str], list[str]]:
    diff: list[str] = []
    undecided: list[str] = []
    for report_field in REPORT_CONTENT_FIELDS:
        left = getattr(gold, report_field)
        right = getattr(prediction, report_field)
        if (left is None) != (right is None):
            diff.append(report_field)
            continue
        if left is None:
            continue
        _compare_number_field(
            f"{report_field}.value", left.value, right.value, diff, undecided
        )
        if (left.unit is None) != (right.unit is None):
            diff.append(f"{report_field}.unit")
        elif left.unit is not None:
            _compare_unit_field(
                f"{report_field}.unit", left.unit, right.unit, diff, undecided
            )
    return diff, undecided


def compare_content(
    gold: MeasuredValue, prediction: MeasuredValue
) -> tuple[Verdict, list[str], list[str]]:
    """Compare content by domain; return verdict, definite differing fields, and
    undecidable fields.
    """
    diff: list[str] = []
    undecided: list[str] = []
    gold_base, gold_error_unit = split_unit_roles(gold.unit)
    pred_base, pred_error_unit = split_unit_roles(prediction.unit)
    for name in NUMBER_CONTENT_FIELDS:
        left = getattr(gold, name)
        right = getattr(prediction, name)
        if (left is None) != (right is None):
            diff.append(name)
            continue
        if left is None:
            continue
        if name in COMPONENT_FIELDS:
            _compare_error_field(
                name,
                left,
                right,
                gold_error_unit,
                pred_error_unit,
                diff,
                undecided,
                compare_units=gold_error_unit != gold_base
                or pred_error_unit != pred_base,
            )
        else:
            if name == "value":
                gold_endpoint = printed_limit_inclusive(left, gold.limit_kind)
                pred_endpoint = printed_limit_inclusive(right, prediction.limit_kind)
                if (
                    gold.limit_kind == prediction.limit_kind
                    and gold_endpoint is not None
                    and pred_endpoint is not None
                    and gold_endpoint != pred_endpoint
                ):
                    diff.append(
                        "upper_inclusive"
                        if gold.limit_kind == "upper_limit"
                        else "lower_inclusive"
                    )
                left = limit_operand(
                    left,
                    gold.limit_kind,
                    inclusive=gold.upper_inclusive
                    if gold.limit_kind == "upper_limit"
                    else gold.lower_inclusive,
                )
                left = percent_operand(left, gold_base)
                right = limit_operand(
                    right,
                    prediction.limit_kind,
                    inclusive=prediction.upper_inclusive
                    if prediction.limit_kind == "upper_limit"
                    else prediction.lower_inclusive,
                )
                right = percent_operand(right, pred_base)
            _compare_number_field(name, left, right, diff, undecided)
    for name in UNIT_CONTENT_FIELDS:
        left = gold_base
        right = pred_base
        if (left is None) != (right is None):
            diff.append(name)
            continue
        if left is None:
            continue
        _compare_unit_field(name, left, right, diff, undecided)
    for name in ("limit_kind", "interval_kind", "lower_inclusive", "upper_inclusive"):
        if getattr(gold, name) != getattr(prediction, name):
            diff.append(name)
    report_diff, report_undecided = _compare_reports(gold, prediction)
    diff.extend(report_diff)
    undecided.extend(report_undecided)
    component_diff, component_undecided = _compare_components(gold, prediction)
    diff.extend(component_diff)
    undecided.extend(component_undecided)
    diff, undecided = list(dict.fromkeys(diff)), list(dict.fromkeys(undecided))
    if diff:
        return "different", diff, undecided
    if undecided:
        return "undecidable", [], undecided
    return "equivalent", [], []


@dataclass
class ValueSide:
    owner: str
    quantity_path: str
    record_id: str
    measured: MeasuredValue
    fragments: list[EvidenceFragment] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.owner}|{self.quantity_path}|{self.record_id}"


@dataclass
class QuantityPairing:
    pairings: list[PairingDetail] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    matched: int = 0
    predicted: int = 0
    gold: int = 0
    reported_mismatch: set[str] = field(default_factory=set)
    reported_unresolved: set[str] = field(default_factory=set)
    matched_gold_refs: set[str] = field(default_factory=set)


def _max_matching(
    edges: set[tuple[int, int]], gold_count: int, pred_count: int
) -> list[tuple[int, int]]:
    """Deterministic maximum one-to-one matching using Kuhn augmenting paths; cardinality
    is input-order independent.
    """

    adjacency = [[] for _ in range(gold_count)]
    for gold_index, pred_index in edges:
        adjacency[gold_index].append(pred_index)
    for neighbors in adjacency:
        neighbors.sort()
    match_of_pred = [-1] * pred_count

    def try_augment(gold_index: int, seen: set[int]) -> bool:
        for pred_index in adjacency[gold_index]:
            if pred_index in seen:
                continue
            seen.add(pred_index)
            if match_of_pred[pred_index] == -1 or try_augment(
                match_of_pred[pred_index], seen
            ):
                match_of_pred[pred_index] = gold_index
                return True
        return False

    for gold_index in range(gold_count):
        try_augment(gold_index, set())
    return [
        (gold_index, pred_index)
        for pred_index, gold_index in enumerate(match_of_pred)
        if gold_index != -1
    ]


def _field_pair(
    gold_side: ValueSide, pred_side: ValueSide, field_name: str
) -> tuple[str | None, str | None]:
    """Extract actual printed strings for unresolved field paths.

    Ordinary fields read directly. coverage/lower_quantile/upper_quantile .value reads
    the reported value and .unit reads its unit, never substituting values for units.
    error_components.<name>.<subfield> selects the named component and actual subfield
    on each side.
    """
    parts = field_name.split(".")
    base = parts[0]
    if base in {"coverage", "lower_quantile", "upper_quantile"}:
        sub = parts[-1] if len(parts) > 1 else "value"
        left = _report_fragment(gold_side.measured, field_name, sub)
        right = _report_fragment(pred_side.measured, field_name, sub)
        return (left or None, right or None)
    if base == "error_components" and len(parts) == 3:
        component_name, sub = parts[1], parts[2]

        def _component_text(value: MeasuredValue) -> str | None:
            for component in value.error_components:
                if component.name == component_name:
                    raw = getattr(component, sub)
                    return None if raw is None else str(raw)
            return None

        return (
            _component_text(gold_side.measured),
            _component_text(pred_side.measured),
        )
    left = getattr(gold_side.measured, base)
    right = getattr(pred_side.measured, base)
    return (
        None if left is None else str(left),
        None if right is None else str(right),
    )


def _resolve_candidate(
    gold_side: ValueSide,
    pred_side: ValueSide,
    undecided_fields: list[str],
    resolver: ScoringAgentResolver | None,
    reasons: list[str],
) -> tuple[Verdict, list[str]]:
    """Merge unresolved field verdicts; any necessary component explicitly different takes
    precedence.

    Collect definite differences/unknowns/technical failures without returning at the
    first insufficient evidence or failure. A definite difference establishes
    whole-value mismatch and stops unnecessary calls. Retain failures in frozen
    storage/auxiliary summaries, injecting unresolved reasons only without definite
    differences.
    """
    if resolver is None:
        return "undecidable", []
    equivalent = True
    determined_diff: list[str] = []
    technical_failures: list[str] = []
    for field_name in undecided_fields:
        left, right = _field_pair(gold_side, pred_side, field_name)
        if left is None or right is None:
            equivalent = False
            continue
        resolution = resolver.resolve_printed(
            field_name=field_name,
            left=left,
            right=right,
            left_fragments=gold_side.fragments,
            right_fragments=pred_side.fragments,
            left_attribution=_attribution_summary(gold_side.measured),
            right_attribution=_attribution_summary(pred_side.measured),
        )
        if resolution.status == "insufficient_evidence":
            equivalent = False
            continue
        if resolution.verdict == "different":
            determined_diff.append(field_name)
            break  # The whole value is definitely mismatched; stop unnecessary calls.
        if resolution.verdict is None:
            technical_failures.append(resolution.entry_key)
            equivalent = False
            continue
        if resolution.verdict == "undecidable":
            equivalent = False
    if determined_diff:
        return "different", determined_diff
    if technical_failures:
        reasons.extend(f"agent_technical_failure:{key}" for key in technical_failures)
    return ("equivalent", []) if equivalent else ("undecidable", [])


def _report_fragment(value: MeasuredValue, field_name: str, part: str) -> str:
    report_field = field_name.split(".")[0]
    report = getattr(value, report_field)
    if report is None:
        return ""
    if part == "unit":
        return report.unit or ""
    return report.value


def pair_quantity_values(
    gold_sides: list[ValueSide],
    pred_sides: list[ValueSide],
    *,
    resolver: ScoringAgentResolver | None,
) -> QuantityPairing:
    """Build the full graph per object/quantity and find deterministic maximum one-to-one
    matching.

    Compare attribution for every Gold/prediction pair first: different forbids edges,
    equivalent permits content checks, and unknowns remain possible edges, never
    resolved by numeric equality. Compare contents only on equivalent-attribution edges;
    equal safe keys certify equivalence, while differing keys require domain-specific
    three-state checks. Unsupported numbers/units stay unknown. Definite-edge maximum
    matching gives C; including possible edges bounds C uncertainty. Attribution-unknown
    edges present in any maximum matching keep correspondence unresolved too.
    """
    result = QuantityPairing(predicted=len(pred_sides), gold=len(gold_sides))
    if not gold_sides and not pred_sides:
        return result
    golds = sorted(gold_sides, key=lambda side: side.ref)
    preds = sorted(pred_sides, key=lambda side: side.ref)
    gold_count, pred_count = len(golds), len(preds)
    # Decide whether technical failures become unresolved reasons only after matching analysis.
    # Irrelevant failures remain in frozen storage/summaries without making the batch provisional.
    deferred_failures: list[str] = []

    attr_equivalent: set[tuple[int, int]] = set()
    attr_forbidden: dict[tuple[int, int], list[str]] = {}
    attr_unknown: set[tuple[int, int]] = set()
    for gold_index, gold_side in enumerate(golds):
        for pred_index, pred_side in enumerate(preds):
            verdict, fields = compare_attribution(
                gold_side.measured, pred_side.measured
            )
            if verdict == "equivalent":
                attr_equivalent.add((gold_index, pred_index))
            elif verdict == "different":
                attr_forbidden[(gold_index, pred_index)] = fields
            elif resolver is not None:
                resolution = resolver.resolve_condition(
                    left=gold_side.measured.condition,
                    right=pred_side.measured.condition,
                    left_fragments=gold_side.fragments,
                    right_fragments=pred_side.fragments,
                )
                if resolution.status == "insufficient_evidence":
                    attr_unknown.add((gold_index, pred_index))
                elif resolution.verdict == "equivalent":
                    attr_equivalent.add((gold_index, pred_index))
                elif resolution.verdict == "different":
                    attr_forbidden[(gold_index, pred_index)] = ["condition"]
                else:
                    attr_unknown.add((gold_index, pred_index))
                    deferred_failures.append(
                        f"agent_technical_failure:{resolution.entry_key}"
                    )
            else:
                attr_unknown.add((gold_index, pred_index))

    determined: set[tuple[int, int]] = set()
    content_mismatch: dict[tuple[int, int], list[str]] = {}
    content_unknown: dict[tuple[int, int], list[str]] = {}
    for gold_index, pred_index in sorted(attr_equivalent):
        gold_side, pred_side = golds[gold_index], preds[pred_index]
        gold_key = content_key(gold_side.measured)
        pred_key = content_key(pred_side.measured)
        if gold_key is not None and pred_key is not None and gold_key == pred_key:
            # Equal keys establish safe equivalence only, never difference.
            determined.add((gold_index, pred_index))
            continue
        verdict, diff, undecided = compare_content(
            gold_side.measured, pred_side.measured
        )
        if verdict == "equivalent":
            determined.add((gold_index, pred_index))
            continue
        if verdict == "different":
            content_mismatch[(gold_index, pred_index)] = diff
            continue
        scratch_reasons: list[str] = []
        resolved_verdict, resolved_diff = _resolve_candidate(
            gold_side,
            pred_side,
            undecided,
            resolver,
            scratch_reasons,
        )
        deferred_failures.extend(scratch_reasons)
        if resolved_verdict == "equivalent":
            determined.add((gold_index, pred_index))
        elif resolved_verdict == "different":
            content_mismatch[(gold_index, pred_index)] = resolved_diff
        else:
            # Insufficient evidence, technical failures, and refusals stay unknown despite an injected adapter.
            content_unknown[(gold_index, pred_index)] = undecided

    matched_pairs = _max_matching(determined, gold_count, pred_count)
    matched_gold_indices = {gold_index for gold_index, _ in matched_pairs}
    matched_pred_indices = {pred_index for _, pred_index in matched_pairs}
    for gold_index, pred_index in matched_pairs:
        result.matched += 1
        result.matched_gold_refs.add(golds[gold_index].ref)
        result.pairings.append(
            PairingDetail(
                layer="l2",
                status="matched",
                gold_ref=golds[gold_index].ref,
                prediction_ref=preds[pred_index].ref,
            )
        )

    possible_edges = set(content_unknown) | set(attr_unknown)
    upper_pairs = _max_matching(determined | possible_edges, gold_count, pred_count)
    score_uncertain = len(upper_pairs) > len(matched_pairs)
    correspondence_uncertain = False
    if not score_uncertain:
        # Attribution-unknown edges in any maximum matching may change which Gold matches
        # which prediction, even with unchanged C; retain unresolved correspondence.
        for gold_index, pred_index in sorted(attr_unknown):
            reduced = {
                edge
                for edge in (determined | possible_edges)
                if edge[0] != gold_index and edge[1] != pred_index
            }
            reduced_size = len(_max_matching(reduced, gold_count, pred_count))
            if reduced_size >= len(upper_pairs) - 1:
                correspondence_uncertain = True
                break
    unresolved = score_uncertain or correspondence_uncertain
    if unresolved:
        # Inject technical failures as unresolved reasons only when they affect scores/correspondence.
        result.reasons.extend(deferred_failures)

    if unresolved:
        for pred_index in sorted({index for _, index in attr_unknown}):
            candidate_refs = sorted(
                golds[gold_index].ref
                for gold_index, index in attr_unknown
                if index == pred_index
            )
            result.reported_unresolved.update(candidate_refs)
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="unresolved_attribution",
                    gold_ref=candidate_refs[0],
                    prediction_ref=preds[pred_index].ref,
                    fields=["condition"],
                )
            )
            result.reasons.append(
                "unresolved_attribution:"
                + preds[pred_index].ref
                + " vs "
                + ",".join(candidate_refs)
            )
        for gold_index, pred_index in sorted(content_unknown):
            gold_side, pred_side = golds[gold_index], preds[pred_index]
            result.reported_unresolved.add(gold_side.ref)
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="unresolved_value",
                    gold_ref=gold_side.ref,
                    prediction_ref=pred_side.ref,
                    fields=sorted(set(content_unknown[(gold_index, pred_index)])),
                )
            )
            result.reasons.append(f"unresolved_value:{gold_side.ref}|{pred_side.ref}")
    else:
        for pred_index, pred_side in enumerate(preds):
            if pred_index in matched_pred_indices:
                continue
            unknown_content_keys = sorted(
                key for key in content_unknown if key[1] == pred_index
            )
            if unknown_content_keys:
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="unknown_candidate_extra",
                        gold_ref=None,
                        prediction_ref=pred_side.ref,
                        fields=sorted(
                            {
                                field
                                for key in unknown_content_keys
                                for field in content_unknown[key]
                            }
                        ),
                    )
                )
            unknown_attr_keys = sorted(
                key for key in attr_unknown if key[1] == pred_index
            )
            if unknown_attr_keys:
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="unknown_relation",
                        gold_ref=golds[unknown_attr_keys[0][0]].ref,
                        prediction_ref=pred_side.ref,
                        fields=["condition"],
                    )
                )
            if unknown_content_keys or unknown_attr_keys:
                continue
            # One-to-one constraint: Gold already paired elsewhere cannot diagnose another mismatch;
            # when all candidates are consumed, diagnose this prediction as extra_value.
            mismatch_keys = sorted(
                key
                for key in content_mismatch
                if key[1] == pred_index and key[0] not in matched_gold_indices
            )
            if mismatch_keys:
                gold_index, _ = mismatch_keys[0]
                gold_side = golds[gold_index]
                result.reported_mismatch.add(gold_side.ref)
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="value_mismatch",
                        gold_ref=gold_side.ref,
                        prediction_ref=pred_side.ref,
                        fields=sorted(set(content_mismatch[mismatch_keys[0]])),
                    )
                )
                continue
            forbidden_keys = sorted(
                key for key in attr_forbidden if key[1] == pred_index
            )
            has_equivalent_edge = any(key[1] == pred_index for key in attr_equivalent)
            if forbidden_keys and not has_equivalent_edge:
                gold_index, _ = forbidden_keys[0]
                result.pairings.append(
                    PairingDetail(
                        layer="l2",
                        status="attribution_mismatch",
                        gold_ref=golds[gold_index].ref,
                        prediction_ref=pred_side.ref,
                        fields=attr_forbidden[forbidden_keys[0]],
                    )
                )
                continue
            result.pairings.append(
                PairingDetail(
                    layer="l2",
                    status="extra_value",
                    gold_ref=None,
                    prediction_ref=pred_side.ref,
                )
            )

    for gold_index, gold_side in enumerate(golds):
        if gold_index in matched_gold_indices:
            continue
        if gold_side.ref in result.reported_mismatch:
            continue
        if gold_side.ref in result.reported_unresolved:
            continue
        result.pairings.append(
            PairingDetail(
                layer="l2",
                status="value_missing",
                gold_ref=gold_side.ref,
                prediction_ref=None,
                fields=[],
            )
        )
    return result


@dataclass
class L1Match:
    pairings: list[PairingDetail] = field(default_factory=list)
    matched_pairs: list[tuple[GoldObjectEntry, PredictionObjectEntry]] = field(
        default_factory=list
    )
    duplicate_pairs: list[tuple[GoldObjectEntry, PredictionObjectEntry]] = field(
        default_factory=list
    )
    unmatched_gold: list[GoldObjectEntry] = field(default_factory=list)
    extra_pred: list[PredictionObjectEntry] = field(default_factory=list)
    ambiguous_pred: list[PredictionObjectEntry] = field(default_factory=list)
    pending_gold: list[GoldObjectEntry] = field(default_factory=list)
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def pending(self) -> int:
        return len(self.ambiguous_pred) + len(self.pending_gold)


def match_objects(
    gold_objects: list[GoldObjectEntry],
    pred_objects: list[PredictionObjectEntry],
) -> L1Match:
    """L1 one-to-one matching after restricted identifier/alias normalization.

    Definite duplicates consume one TP and remaining FP; duplicate_pairs supplies
    confirmed-identity record pools for L2 matching. Real alias ambiguity across
    multiple Gold objects remains pending without FP/FN, value-based disambiguation, or
    duplicate-pool membership.
    """
    match = L1Match()
    taken: set[str] = set()
    ambiguous_candidates: set[str] = set()
    for pred in pred_objects:
        pred_ids = identifier_keys(pred.identifiers)
        candidates = [
            gold
            for gold in gold_objects
            if pred_ids & identifier_keys(gold.identifiers)
        ]
        if not candidates:
            match.fp += 1
            match.extra_pred.append(pred)
            match.pairings.append(
                PairingDetail(layer="l1", status="extra", prediction_ref=pred.label)
            )
            continue
        if len(candidates) > 1:
            match.ambiguous_pred.append(pred)
            ambiguous_candidates.update(gold.label for gold in candidates)
            match.pairings.append(
                PairingDetail(layer="l1", status="ambiguous", prediction_ref=pred.label)
            )
            continue
        gold = candidates[0]
        if gold.label in taken:
            # Duplicate predictions uniquely refer to the same confirmed Gold identity: L1 counts FP;
            # score.py merges their records into one identity pool for L2 one-to-one value matching.
            match.fp += 1
            match.duplicate_pairs.append((gold, pred))
            match.pairings.append(
                PairingDetail(
                    layer="l1",
                    status="duplicate",
                    gold_ref=gold.label,
                    prediction_ref=pred.label,
                )
            )
            continue
        taken.add(gold.label)
        match.tp += 1
        match.matched_pairs.append((gold, pred))
        match.pairings.append(
            PairingDetail(
                layer="l1",
                status="matched",
                gold_ref=gold.label,
                prediction_ref=pred.label,
            )
        )
    for gold in gold_objects:
        if gold.label in taken:
            continue
        if gold.label in ambiguous_candidates:
            match.pending_gold.append(gold)
            match.pairings.append(
                PairingDetail(layer="l1", status="pending", gold_ref=gold.label)
            )
        else:
            match.fn += 1
            match.unmatched_gold.append(gold)
            match.pairings.append(
                PairingDetail(layer="l1", status="omitted", gold_ref=gold.label)
            )
    return match
