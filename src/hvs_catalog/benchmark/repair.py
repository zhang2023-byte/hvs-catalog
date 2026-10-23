"""Compose validated repair fragments without replacing any original result."""

import copy
from pathlib import Path

from ..evaluation.adapt import _evidence_count
from ..evaluation.bundle import ScoringBundle, program_reference, save_scoring_bundle
from ..evaluation.saved import export_scoring_report
from ..evaluation.scoring import Prediction, score_batch
from ..pipeline.evidence_audit import make_audit
from ..records import quantity_validation, roster_validation
from ..records.common import ArtifactRef, sha256_of
from ..records.evidence_audit import quote_mismatches, uses_quote_warnings
from ..records.literature_contribution import LiteratureContribution
from ..records.roster_validation import evidence_issues
from ..workspace import open_workspace
from ..workspace.archives import read_json
from .repair_runtime import repair_status, run_repair
from .repair_source import (
    file_hash,
    prepare_repair,
    read_file,
    repair_folder,
    verify_sources,
    write_json,
)

__all__ = ["prepare_repair", "repair_status", "run_repair", "score_repair"]


def validate_fragments(folder, facts, jobs):
    """Replay archived submissions before including any derived content."""
    from ..llm.config import ModelConfig
    from .repair_source import job_locations, request_from_prepared

    space = open_workspace(Path(folder) / ".calls", read_only=True)
    try:
        for job in jobs:
            validator = (
                roster_validation if job["kind"] == "roster" else quantity_validation
            )
            selected = None
            for saved in job["validations"]:
                prepared = read_json(
                    space, ArtifactRef.model_validate(saved["request"])
                )
                request_from_prepared(
                    prepared, ModelConfig.model_validate(job["model"])
                )
                env = read_json(space, ArtifactRef.model_validate(saved["response"]))
                if env.get("request_sha256") != sha256_of(prepared):
                    raise ValueError("Repair response is bound to another request.")
                if prepared.get("evidence_policy") != job["prepared"].get(
                    "evidence_policy"
                ):
                    raise ValueError(
                        "Repair evidence policy differs from its frozen request."
                    )
                warning_mode = uses_quote_warnings(prepared.get("evidence_policy"))
                check = validator.check_response(
                    env, job_locations(facts, job), quote_warnings=warning_mode
                )
                values, indices, rejected, notes = (
                    validator.select_partial(check, job_locations(facts, job))
                    if check.readable
                    else ([], [], {}, check.notes)
                )
                expected = {
                    **saved,
                    "passed": check.passed,
                    "readable": check.readable,
                    "values": [v.model_dump(mode="json") for v in values],
                    "indices": indices,
                    "rejected": {
                        k: [i.model_dump() for i in v] for k, v in rejected.items()
                    },
                    "issues": [i.model_dump() for i in check.issues],
                    "notes": notes,
                }
                if warning_mode:
                    expected["warnings"] = [w.model_dump() for w in check.warnings]
                if saved != expected:
                    raise ValueError("Repair fragment differs from its archived reply.")
                if check.readable:
                    selected = saved
            if job["fragment"] and (
                selected is None
                or {k: v for k, v in job["fragment"].items() if k != "status"}
                != selected
            ):
                raise ValueError(
                    "Repair stage differs from its latest readable submission."
                )
    finally:
        space.close()


def merge_quantities(original, new):
    """Freeze every existing group; add only previously absent quantity groups."""
    result = copy.deepcopy(original)
    by_id = {g["quantity"]: g for g in result}
    conflicts = []
    for group in new:
        quantity = group["quantity"]
        if quantity not in by_id:
            result.append(copy.deepcopy(group))
            by_id[quantity] = result[-1]
        elif by_id[quantity] != group:
            conflicts.append(quantity)
    return result, conflicts


def repaired_prediction(baseline, context, jobs):
    """Derive science only from frozen baseline content and checked stage fragments."""
    baseline = Prediction.model_validate(baseline)
    value = baseline.model_dump(mode="json")
    content = value["content"]
    source_roster = context["roster"]
    roster_data = source_roster["data"] if source_roster else None
    quantities = context["quantities"]
    statuses = {int(i): record["data"]["status"] for i, record in quantities.items()}
    roster_status = roster_data["status"] if roster_data else None
    indices = roster_data["source_indices"] if roster_data else []
    overlay = []
    conflicts = []
    roster_jobs = [j for j in jobs if j["kind"] == "roster"]
    if roster_jobs:
        if len(roster_jobs) != 1 or source_roster is not None or content["stars"]:
            raise ValueError("Repair must not replace a retained roster.")
        job = roster_jobs[0]
        fragment = job["fragment"]
        if fragment:
            roster_status = fragment["status"]
            indices = fragment["indices"]
            content["stars"] = [
                {**copy.deepcopy(star), "quantities": []} for star in fragment["values"]
            ]
            if fragment["notes"]:
                content["notes"].append(fragment["notes"])
    for job in jobs:
        fragment = job["fragment"]
        overlay.append(
            {
                k: job[k]
                for k in [
                    "id",
                    "step",
                    "source_index",
                    "kind",
                    "source",
                    "calls",
                    "fragment",
                    "stop_reason",
                ]
                if k in job
            }
        )
        if job["kind"] == "roster" or not fragment:
            continue
        index = job["source_index"]
        if index not in indices:
            raise ValueError(
                "Numerical repair target is absent from the retained roster."
            )
        star = content["stars"][indices.index(index)]
        before = copy.deepcopy(star["quantities"])
        star["quantities"], changed = merge_quantities(before, fragment["values"])
        conflicts.extend(
            {"step": job["step"], "quantity": q, "action": "retained_baseline"}
            for q in changed
        )
        statuses[index] = "partial" if changed else fragment["status"]
        if fragment["notes"]:
            content["notes"].append(
                star["identifiers"][0]["value"] + ": " + fragment["notes"]
            )
    # Check every pre-existing star and value, including partial and false-positive content.
    old = baseline.content.model_dump(mode="json")
    if source_roster:
        for before, after in zip(old["stars"], content["stars"], strict=True):
            if {k: v for k, v in before.items() if k != "quantities"} != {
                k: v for k, v in after.items() if k != "quantities"
            }:
                raise ValueError("Retained star changed during network repair.")
            for group in before["quantities"]:
                if group not in after["quantities"]:
                    raise ValueError("Retained numerical group changed during repair.")
    science = LiteratureContribution.model_validate(content)
    warning_mode = uses_quote_warnings(context.get("evidence_policy"))
    checks = evidence_issues(
        science.model_dump(mode="json"),
        context["material"]["citation_locations"],
        quote_warnings=warning_mode,
    )
    if checks:
        raise ValueError(
            "Derived repair evidence is not supported by the original material."
        )
    resolved = roster_status in {"complete", "partial"} and all(
        i in statuses for i in indices
    )
    completion = (
        "complete"
        if resolved
        and roster_status == "complete"
        and all(statuses[i] == "complete" for i in indices)
        else "partial"
        if resolved
        else "failed"
        if roster_status == "failed"
        else "unfinished"
    )
    value.update(
        content=science.model_dump(mode="json"),
        delivered=baseline.delivered or resolved,
        format_valid=baseline.format_valid or resolved,
        completion=completion,
        execution_state="finished",
        evidence_checks={
            **baseline.evidence_checks,
            "invalid": 0,
            "checked": _evidence_count(science.model_dump(mode="json")),
            **(
                {
                    "quote_mismatches": len(
                        quote_mismatches(
                            science.model_dump(mode="json"),
                            context["material"]["citation_locations"],
                        )
                    )
                }
                if warning_mode
                else {}
            ),
        },
    )
    value["provenance"] = {
        "format": "hvs.network-repair-prediction",
        "version": 1,
        "baseline": baseline.provenance,
        "network_repair": {
            "stages": overlay,
            "conflicts": conflicts,
            "calls_workspace": ".calls",
            "original_quantities_preserved": True,
        },
    }
    return Prediction.model_validate(value)


def export_repair_prediction(root, output, pred, context):
    """Publish a derived prediction and its evidence audit without replacing sources."""
    root, output = Path(root).resolve(), Path(output).resolve()
    output.relative_to(root)
    if output.exists():
        raise ValueError("Derived output already exists; verify and reuse it.")
    write_json(
        output / "retained-prediction.json",
        pred.model_dump(mode="json"),
    )
    write_json(output / "overlay.json", pred.provenance["network_repair"])
    if pred.delivered:
        write_json(
            output / "literature_contribution.json",
            pred.content.model_dump(mode="json"),
        )
        if uses_quote_warnings(context.get("evidence_policy")):
            result_path = output / "literature_contribution.json"
            result_ref = ArtifactRef(
                path=str(result_path.relative_to(root)),
                sha256=file_hash(result_path),
            )
            audit = make_audit(
                pred.content,
                result_ref,
                quote_mismatches(
                    pred.content.model_dump(mode="json"),
                    context["material"]["citation_locations"],
                ),
            )
            write_json(
                output / "evidence_audit.json",
                audit.model_dump(mode="json"),
            )


def score_repair(root, name, *, label="network-repair-01"):
    """Use the unchanged scorer and the original full-scope frozen references."""
    root = Path(root).resolve()
    folder = repair_folder(root, name, label)
    manifest = read_file(folder / "manifest.json")
    verify_sources(root, manifest)
    state = read_file(folder / "state.json")
    if state["finished"] is None:
        raise ValueError("End the prepared repair before scoring.")
    if (folder / "scoring/scoring-report.json").exists():
        from ..evaluation.bundle import score_scoring_bundle

        return score_scoring_bundle(folder / "scoring/scoring-inputs.json")
    original = ScoringBundle.model_validate(
        read_file(root / "runs" / name / "scoring/scoring-inputs.json")
    )
    original_report = read_file(root / "runs" / name / "scoring/scoring-report.json")
    reproduced = score_batch(original.plan, original.gold, original.predictions)
    if reproduced.model_dump(mode="json") != original_report:
        raise ValueError("Current scorer did not reproduce the exact original report.")
    facts = read_file(folder / "source-facts.json")
    if sha256_of(facts) != manifest["source_facts_sha256"]:
        raise ValueError("Frozen repair input facts changed.")
    validate_fragments(folder, facts, state["jobs"])
    predictions = []
    for baseline in original.predictions:
        paper = baseline.content.metadata.arxiv_id
        jobs = [j for j in state["jobs"] if j["paper"] == paper]
        pred = repaired_prediction(baseline, facts[paper], jobs) if jobs else baseline
        predictions.append(pred)
        if jobs:
            export_repair_prediction(root, folder / paper, pred, facts[paper])
    plan = original.plan.model_copy(
        update={
            "program": program_reference(),
            "provenance": {
                **original.plan.provenance,
                "network_repair": {
                    "label": label,
                    "original_bundle_sha256": original.sha256,
                },
            },
        }
    )
    report = score_batch(plan, original.gold, predictions)
    export_scoring_report(report, folder / "scoring")
    save_scoring_bundle(
        folder / "scoring/scoring-inputs.json",
        plan=plan,
        gold=original.gold,
        predictions=predictions,
    )
    write_json(
        folder / "comparison.json",
        {
            "round": name,
            "baseline": original_report["micro"],
            "repaired": report.model_dump(mode="json")["micro"],
            "l1_f1_change_pp": 100 * (report.micro.l1.f1 - reproduced.micro.l1.f1),
            "l2_f1_change_pp": 100 * (report.micro.l2.f1 - reproduced.micro.l2.f1),
        },
    )
    write_json(
        folder / "trace.json",
        {
            "format": "hvs.network-repair-trace",
            "calls_workspace": ".calls",
            "version": 1,
            "stages": [
                {
                    k: j[k]
                    for k in [
                        "id",
                        "paper",
                        "step",
                        "kind",
                        "source",
                        "calls",
                        "validations",
                        "fragment",
                    ]
                }
                for j in state["jobs"]
            ],
        },
    )
    verify_sources(root, manifest)
    return report
