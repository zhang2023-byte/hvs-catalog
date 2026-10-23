"""Program-only scoring of every paper in a fixed plan."""

from collections import Counter, defaultdict

from ...records.common import sha256_of
from ...records.rules import current_rules_ref
from .identifiers import identifier_keys, qualified_identifiers
from .layers import maximum_matching
from .models import GoldReference, Prediction, ScoringPlan
from .numeric import scheme_reference, value_differences
from .results import (
    L0Metrics,
    Metrics,
    Pairing,
    PaperResult,
    RosterDetails,
    ScoringReport,
    l1_metrics,
    l2_metrics,
    ratio,
)


def _identifier_keys(identifier):
    return identifier_keys(
        qualified_identifiers(
            identifier.value,
            [(e.location, getattr(e, "quote", "")) for e in identifier.evidence],
        )
    )


def _star_keys(star):
    return set().union(*(_identifier_keys(i) for i in star.identifiers))


def _values(stars, indices, quantity):
    return [
        (f"/stars/{s}/quantities/{q}/values/{v}", value)
        for s in indices
        for q, group in enumerate(stars[s].quantities)
        if group.quantity == quantity
        for v, value in enumerate(group.values)
    ]


def _pair_values(gold, prediction, quantity):
    differences = {
        (g, p): value_differences(gv, pv, quantity)
        for g, (_, gv) in enumerate(gold)
        for p, (_, pv) in enumerate(prediction)
    }
    matched = maximum_matching(
        {key for key, fields in differences.items() if not fields}, len(gold)
    )
    rows = [
        Pairing(
            layer="value",
            status="matched",
            quantity=quantity,
            gold_path=gold[g][0],
            prediction_path=prediction[p][0],
        )
        for g, p in matched
    ]
    left = [g for g in range(len(gold)) if g not in {x for x, _ in matched}]
    right = [p for p in range(len(prediction)) if p not in {y for _, y in matched}]
    for g, p in zip(left, right):
        rows.append(
            Pairing(
                layer="value",
                status="mismatch",
                quantity=quantity,
                gold_path=gold[g][0],
                prediction_path=prediction[p][0],
                fields=differences[g, p],
            )
        )
    rows.extend(
        Pairing(
            layer="value", status="missing", quantity=quantity, gold_path=gold[g][0]
        )
        for g in left[len(right) :]
    )
    rows.extend(
        Pairing(
            layer="value",
            status="extra",
            quantity=quantity,
            prediction_path=prediction[p][0],
        )
        for p in right[len(left) :]
    )
    return len(matched), rows


def _roster_details(gold, pred, g, p, details, rows):
    details.matched_stars += 1
    correct = gold.contribution_type.value == pred.contribution_type.value
    details.contribution_correct += int(correct)
    rows.append(
        Pairing(
            layer="contribution_type",
            status="matched" if correct else "mismatch",
            gold_path=f"/stars/{g}/contribution_type",
            prediction_path=f"/stars/{p}/contribution_type",
        )
    )
    matched = maximum_matching(
        {
            (i, j)
            for i, a in enumerate(gold.identifiers)
            for j, b in enumerate(pred.identifiers)
            if _identifier_keys(a) & _identifier_keys(b)
        },
        len(gold.identifiers),
    )
    for i, j in matched:
        rows.append(
            Pairing(
                layer="identifier",
                status="matched",
                gold_path=f"/stars/{g}/identifiers/{i}",
                prediction_path=f"/stars/{p}/identifiers/{j}",
                basis=sorted(
                    _identifier_keys(gold.identifiers[i])
                    & _identifier_keys(pred.identifiers[j])
                ),
            )
        )
    for i in range(len(gold.identifiers)):
        if i not in {x for x, _ in matched}:
            details.missing_identifiers += 1
            rows.append(
                Pairing(
                    layer="identifier",
                    status="missing",
                    gold_path=f"/stars/{g}/identifiers/{i}",
                )
            )
    for j in range(len(pred.identifiers)):
        if j not in {y for _, y in matched}:
            details.extra_identifiers += 1
            rows.append(
                Pairing(
                    layer="identifier",
                    status="extra",
                    prediction_path=f"/stars/{p}/identifiers/{j}",
                )
            )


def _l0(planned, delivered, valid, counts):
    return L0Metrics(
        planned=planned,
        delivered=delivered,
        format_valid=valid,
        completion_counts=dict(sorted(counts.items())),
        delivered_over_planned=ratio(delivered, planned),
        valid_over_delivered=ratio(valid, delivered),
        valid_over_planned=ratio(valid, planned),
    )


def _paper(plan, reference, prediction):
    gold = reference.stars
    pred = prediction.content.stars if prediction else []
    rows, pools, extra = [], defaultdict(list), []
    details = RosterDetails()
    for p, star in enumerate(pred):
        candidates = [
            g for g, target in enumerate(gold) if _star_keys(star) & _star_keys(target)
        ]
        if len(candidates) != 1:
            extra.append(p)
            rows.append(
                Pairing(
                    layer="star",
                    status="ambiguous_identity" if candidates else "extra",
                    prediction_path=f"/stars/{p}",
                    basis=[f"/stars/{g}" for g in candidates],
                )
            )
            continue
        g = candidates[0]
        duplicate = bool(pools[g])
        pools[g].append(p)
        rows.append(
            Pairing(
                layer="star",
                status="duplicate" if duplicate else "matched",
                gold_path=f"/stars/{g}",
                prediction_path=f"/stars/{p}",
                basis=sorted(_star_keys(star) & _star_keys(gold[g])),
            )
        )
        if not duplicate:
            _roster_details(gold[g], star, g, p, details, rows)
    for g in range(len(gold)):
        if g not in pools:
            rows.append(
                Pairing(layer="star", status="missing", gold_path=f"/stars/{g}")
            )
    details.contribution_accuracy = ratio(
        details.contribution_correct, details.matched_stars
    )
    quantities = {}
    for quantity in plan.quantities:
        correct = 0
        for g in range(len(gold)):
            count, pairs = _pair_values(
                _values(gold, [g], quantity),
                _values(pred, pools[g], quantity),
                quantity,
            )
            correct += count
            rows.extend(pairs)
        _, pairs = _pair_values([], _values(pred, extra, quantity), quantity)
        rows.extend(pairs)
        quantities[quantity] = l2_metrics(
            correct,
            len(_values(pred, range(len(pred)), quantity)),
            len(_values(gold, range(len(gold)), quantity)),
        )
    # Accessing pools for missing stars above must not alter identity counts.
    matched_stars = details.matched_stars
    completion = prediction.completion if prediction else "unfinished"
    return PaperResult(
        arxiv_id=reference.metadata.arxiv_id,
        version=reference.metadata.version,
        execution_state=prediction.execution_state if prediction else "not_started",
        correct_empty=not gold and not pred and completion == "complete",
        l0=_l0(
            1,
            int(prediction.delivered) if prediction else 0,
            int(prediction.format_valid) if prediction else 0,
            {completion: 1},
        ),
        l1=l1_metrics(
            matched_stars, len(pred) - matched_stars, len(gold) - matched_stars
        ),
        l2=l2_metrics(
            sum(q.correct for q in quantities.values()),
            sum(q.predicted for q in quantities.values()),
            sum(q.gold for q in quantities.values()),
        )
        if plan.scope == "full"
        else None,
        roster_details=details,
        quantities=quantities,
        pairings=rows,
        errors=prediction.errors
        if prediction
        else ["No prediction was provided for this planned paper."],
        evidence_checks=prediction.evidence_checks if prediction else {},
    )


def score_batch(
    plan: ScoringPlan, gold: list[GoldReference], predictions: list[Prediction]
) -> ScoringReport:
    """Revalidate copies before hashing and scoring. Never mutate either input."""
    plan = ScoringPlan.model_validate(plan.model_dump(mode="json"))
    gold = [GoldReference.model_validate(g.model_dump(mode="json")) for g in gold]
    predictions = [
        Prediction.model_validate(p.model_dump(mode="json")) for p in predictions
    ]
    if (
        plan.scheme.model_dump() != scheme_reference()
        or plan.rules.model_dump() != current_rules_ref()
    ):
        raise ValueError(
            "The scoring scheme or science rules do not match this program."
        )
    gold_ids = [g.metadata.arxiv_id for g in gold]
    pred_ids = [p.content.metadata.arxiv_id for p in predictions]
    if len(set(gold_ids)) != len(gold_ids) or set(gold_ids) != set(plan.papers):
        raise ValueError("The plan requires exactly one reference per paper.")
    if len(set(pred_ids)) != len(pred_ids) or set(pred_ids) - set(plan.papers):
        raise ValueError("Predictions contain duplicate or unplanned papers.")
    gold_map = dict(zip(gold_ids, gold, strict=True))
    pred_map = dict(zip(pred_ids, predictions, strict=True))
    for g in gold:
        if g.metadata.rules != plan.rules:
            raise ValueError("Reference science rules differ from the plan.")
    for p in predictions:
        g = gold_map[p.content.metadata.arxiv_id]
        if (
            p.content.metadata.version != g.metadata.version
            or p.rules != g.metadata.rules
            or sorted((f.role, f.sha256) for f in p.materials.files)
            != [("pdf", g.metadata.material.sha256)]
        ):
            raise ValueError(
                "Prediction and reference paper version, rules or materials differ."
            )
        if p.scope != plan.scope:
            raise ValueError("Prediction scope differs from the fixed scoring plan.")
    papers = [_paper(plan, gold_map[key], pred_map.get(key)) for key in plan.papers]
    counts = Counter()
    for p in papers:
        counts.update(p.l0.completion_counts)
    details = RosterDetails(
        **{
            name: sum(getattr(p.roster_details, name) for p in papers)
            for name in (
                "matched_stars",
                "contribution_correct",
                "missing_identifiers",
                "extra_identifiers",
            )
        }
    )
    details.contribution_accuracy = ratio(
        details.contribution_correct, details.matched_stars
    )
    micro = Metrics(
        l0=_l0(
            len(papers),
            sum(p.l0.delivered for p in papers),
            sum(p.l0.format_valid for p in papers),
            counts,
        ),
        l1=l1_metrics(
            *(sum(getattr(p.l1, name) for p in papers) for name in ("tp", "fp", "fn"))
        ),
        l2=l2_metrics(
            *(
                sum(getattr(p.l2, name) for p in papers)
                for name in ("correct", "predicted", "gold")
            )
        )
        if plan.scope == "full"
        else None,
        roster_details=details,
    )
    return ScoringReport(
        input_sha256=sha256_of(
            {
                "plan": plan.model_dump(mode="json"),
                "gold": [g.model_dump(mode="json") for g in gold],
                "predictions": [p.model_dump(mode="json") for p in predictions],
            }
        ),
        scheme=plan.scheme.model_dump(),
        papers=papers,
        micro=micro,
        notes=[
            "Only exact or approved formatting matches count. Every other comparison is a mismatch.",
            "L2 measures numerical structure, not semantic correctness of conditions, evidence or notes.",
            "Explicit approved numerical qualifiers in condition retain their numerical effect; other wording is not scored.",
            "Identifier omissions and contribution accuracy cover matched stars only. L1 separately counts missing and extra stars.",
            "An ambiguous identity is unmatched. Duplicate stars count as L1 extras; their values share one identity pool and cannot consume a reference value twice.",
            "JSON paths locate unchanged reference answers and retained predictions in the frozen scoring inputs.",
        ],
    )
