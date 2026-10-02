"""Apply frozen scope limitations symmetrically and retain default/reference scores.

Only identifiers, quantities, and reviewed conditions select a scope. Never use
observed values, correctness, array order, or internal labels to remove a prediction.
Ambiguous condition/identity membership stays in counts and makes that score
provisional. Known limitations locate potential causes; they do not prove causality.
"""

from __future__ import annotations

from ...records.common import sha256_of
from .errors import ScoringInputError
from .layers import normalize_identifier
from .models import gold_input_digest, predictions_input_digest
from .printed import compare_condition
from .results import LimitationEffect


def _object_matches(scope, obj) -> bool:
    return bool(
        {normalize_identifier(name) for name in scope.object_identifiers}
        & {normalize_identifier(name) for name in obj.identifiers}
    )


def _membership(scope, obj, path=None, value=None) -> str:
    if not _object_matches(scope, obj):
        return "different"
    if scope.quantity_path is None:
        return "equivalent"
    if path != scope.quantity_path:
        return "different"
    comparisons = []
    selectors = dict(scope.attribution)
    if scope.conditions:
        selectors["condition"] = scope.conditions
    for field_name, accepted in selectors.items():
        actual = getattr(value, field_name, None)
        if actual is None:
            comparisons.append("undecidable")
        elif actual in accepted:
            comparisons.append("equivalent")
        elif field_name in {"source", "coordinate_format", "representation"}:
            comparisons.append("different")
        else:
            states = [compare_condition(item, actual) for item in accepted]
            comparisons.append(
                "equivalent"
                if "equivalent" in states
                else "undecidable"
                if "undecidable" in states
                else "different"
            )
    if "different" in comparisons:
        return "different"
    return "undecidable" if "undecidable" in comparisons else "equivalent"


def _refs(papers, limitation) -> tuple[list[str], list[str]]:
    refs, unknown = [], []
    for paper in papers:
        if paper.paper_key != limitation.paper_key:
            continue
        for obj in paper.objects:
            if not _object_matches(limitation.scope, obj):
                continue
            if limitation.scope.quantity_path is None:
                refs.append(obj.label)
            for group in obj.quantities:
                for entry in group.values:
                    state = _membership(
                        limitation.scope, obj, group.quantity_path, entry.value
                    )
                    ref = f"{obj.label}|{group.quantity_path}|{entry.record_id}"
                    if state == "equivalent":
                        refs.append(ref)
                    elif state == "undecidable":
                        unknown.append(ref)
    return sorted(set(refs)), sorted(set(unknown))


def _prediction_scopes(issues, gold):
    """Use every established alias of a uniquely identified Gold object."""
    result = []
    for item in issues:
        matches = [
            obj
            for paper in gold
            if paper.paper_key == item.paper_key
            for obj in paper.objects
            if _object_matches(item.scope, obj)
        ]
        if len(matches) == 1:
            identifiers = sorted(
                set(item.scope.object_identifiers + matches[0].identifiers)
            )
            item = item.model_copy(
                update={
                    "scope": item.scope.model_copy(
                        update={"object_identifiers": identifiers}
                    )
                }
            )
        result.append(item)
    return result


def _filter(papers, exclusions, *, identity_gold=None):
    results, unknown = [], []
    for paper in papers:
        objects = []
        issues = [item for item in exclusions if item.paper_key == paper.paper_key]
        for obj in paper.objects:
            if identity_gold is not None and any(
                _object_matches(item.scope, obj) for item in issues
            ):
                matches = [
                    gold_obj
                    for gold_paper in identity_gold
                    if gold_paper.paper_key == paper.paper_key
                    for gold_obj in gold_paper.objects
                    if {normalize_identifier(name) for name in gold_obj.identifiers}
                    & {normalize_identifier(name) for name in obj.identifiers}
                ]
                if len(matches) > 1:
                    objects.append(obj)
                    unknown.append((paper.paper_key, obj.label, "l1"))
                    continue
            if any(
                item.scope.quantity_path is None and _object_matches(item.scope, obj)
                for item in issues
            ):
                continue
            groups = []
            for group in obj.quantities:
                values = []
                for entry in group.values:
                    states = [
                        _membership(item.scope, obj, group.quantity_path, entry.value)
                        for item in issues
                    ]
                    if "equivalent" in states:
                        continue
                    if "undecidable" in states:
                        unknown.append(
                            (
                                paper.paper_key,
                                f"{obj.label}|{group.quantity_path}|{entry.record_id}",
                                "l2",
                            )
                        )
                    values.append(entry)
                if values:
                    groups.append(group.model_copy(update={"values": values}))
            objects.append(obj.model_copy(update={"quantities": groups}))
        results.append(paper.model_copy(update={"objects": objects}))
    return results, unknown


def _derived_plan(plan, gold, mode):
    payload = plan.model_dump(mode="json")
    payload.pop("limitations")
    payload["gold_ref"]["fingerprint"] = gold_input_digest(gold)
    # Frozen auxiliary requests must bind both the original declared limitations and
    # this score variant, even when filtering happens to remove no actual entries.
    payload["scoring_scheme_ref"]["fingerprint"] = sha256_of(
        {
            "scheme": payload["scoring_scheme_ref"],
            "limitations": plan.model_dump(mode="json")["limitations"],
            "mode": mode,
        }
    )
    return type(plan).model_validate(payload)


def _mark_unknown(report, unknown):
    if not unknown:
        return
    report.batch_provisional = True
    report.provisional_reasons.extend(
        f"scope_membership_unknown:{paper_key}:{ref}"
        for paper_key, ref, _ in sorted(set(unknown))
    )
    affected = {paper_key for paper_key, _, _ in unknown}
    identity_affected = {paper_key for paper_key, _, layer in unknown if layer == "l1"}
    for paper in report.papers:
        if paper.paper_key not in affected:
            continue
        paper.l2_provisional = True
        if paper.paper_key in identity_affected and paper.l1 is not None:
            paper.l1.precision = paper.l1.recall = paper.l1.f1 = None
        for metric in [paper.l2, *[item.metrics for item in paper.quantity_breakdown]]:
            if metric is not None:
                metric.precision = metric.recall = metric.f1 = None
    if report.micro.l2 is not None:
        report.micro.l2.precision = report.micro.l2.recall = report.micro.l2.f1 = None
    if identity_affected and report.micro.l1 is not None:
        report.micro.l1.precision = report.micro.l1.recall = report.micro.l1.f1 = None
    report.notes.append("问题范围的条件对应无法确定；相关计数保留，参考率暂定")


def score_with_limitations(plan, gold, predictions, *, auxiliary, agent_adapter):
    """Score the default scope and a second scope excluding PDF/source mismatches."""
    from .score import score_batch

    for item in plan.limitations:
        if item.category == "unsupported_material" and item.exclude_from_default:
            refs, unknown = _refs(gold, item)
            if refs or unknown:
                raise ScoringInputError(
                    "不支持解析的资料已有PDF-backed Gold时仍须默认评分；"
                    "应声明PDF/源码表示限制，不能以不支持解析为由排除Gold"
                )
        if item.exclude_from_reference:
            refs, unknown = _refs(gold, item)
            if not refs or unknown:
                raise ScoringInputError("参考排除范围未明确对应固定Gold条目")
            objects = [
                obj
                for paper in gold
                if paper.paper_key == item.paper_key
                for obj in paper.objects
                if _object_matches(item.scope, obj)
            ]
            if len(objects) != 1:
                raise ScoringInputError("参考排除范围必须唯一对应一个Gold对象")

    exclusions = [item for item in plan.limitations if item.exclude_from_default]
    effective_gold, gold_unknown = _filter(gold, exclusions)
    effective_predictions, prediction_unknown = _filter(
        predictions, _prediction_scopes(exclusions, gold), identity_gold=gold
    )
    report = score_batch(
        _derived_plan(plan, effective_gold, "default"),
        effective_gold,
        effective_predictions,
        auxiliary=auxiliary,
        agent_adapter=agent_adapter,
    )
    _mark_unknown(report, gold_unknown + prediction_unknown)
    report.digests.update(
        {
            "input_plan": sha256_of(plan.model_dump(mode="json")),
            "input_gold": gold_input_digest(gold),
            "input_predictions": predictions_input_digest(predictions),
            "limitations": sha256_of(
                [item.model_dump(mode="json") for item in plan.limitations]
            ),
        }
    )
    error_refs = {}
    for paper in report.papers:
        refs = error_refs.setdefault(paper.paper_key, set())
        for row in paper.pairings:
            if row.status not in {"matched", "out_of_scope_value"}:
                refs.update(ref for ref in (row.gold_ref, row.prediction_ref) if ref)
        for diagnostic in paper.diagnostics:
            if diagnostic.code not in {"out_of_scope_object", "out_of_scope_value"}:
                refs.update(diagnostic.refs)
    for item, prediction_scope in zip(
        plan.limitations, _prediction_scopes(plan.limitations, gold), strict=True
    ):
        gold_refs, _ = _refs(gold, item)
        prediction_refs, _ = _refs(predictions, prediction_scope)
        report.limitation_effects.append(
            LimitationEffect(
                limitation_id=item.limitation_id,
                paper_key=item.paper_key,
                category=item.category,
                basis=item.basis,
                evidence=item.evidence,
                gold_refs=gold_refs,
                prediction_refs=prediction_refs,
                observed_error_refs=sorted(
                    error_refs.get(item.paper_key, set())
                    & set(gold_refs + prediction_refs)
                ),
                excluded_from_default=item.exclude_from_default,
                excluded_from_reference=item.exclude_from_reference,
            )
        )
    reference_exclusions = exclusions + [
        item for item in plan.limitations if item.exclude_from_reference
    ]
    if any(item.exclude_from_reference for item in plan.limitations):
        reference_gold, gold_unknown = _filter(gold, reference_exclusions)
        reference_predictions, prediction_unknown = _filter(
            predictions,
            _prediction_scopes(reference_exclusions, gold),
            identity_gold=gold,
        )
        reference = score_batch(
            _derived_plan(plan, reference_gold, "reference"),
            reference_gold,
            reference_predictions,
            auxiliary=auxiliary,
            agent_adapter=agent_adapter,
        )
        _mark_unknown(reference, gold_unknown + prediction_unknown)
        default_papers = {paper.paper_key: paper for paper in effective_gold}
        for paper in reference.papers:
            if paper.correct_empty and default_papers[paper.paper_key].objects:
                paper.correct_empty = False
                reference.correct_empty_papers.remove(paper.paper_key)
        reference.notes.append(
            "按事先固定范围同步排除PDF/源码不对齐的Gold与预测，重算全部指标"
        )
        report.reference_score = reference
    report.notes.append(
        "已知材料/作者问题是错误的潜在解释；分类不自动证明原因或调整得分"
    )
    return report
