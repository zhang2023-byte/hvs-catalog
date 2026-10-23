"""Assemble scientific results and publish immutable, transactional result generations."""

import json
import re
from pathlib import Path

from ..records.common import ArtifactRef, sha256_of
from ..records.evidence_audit import uses_quote_warnings
from ..records.extraction_run import ExtractionRun, StageSummary
from ..records.literature_contribution import LiteratureContribution
from ..runtime import get_execution
from ..workspace.archives import publish_json, read_json
from ..workspace.store import CommitRequest
from .evidence_audit import make_audit, retained_mismatches, verify_result_audit
from .inputs import read_input
from .roster import saved_roster
from .stages import quantity_step

PREFIX = "extraction_result:"


def saved_quantities(space, execution_id, roster):
    handle = get_execution(space, execution_id)
    run = space.store().read_record_current(handle.run_ref.record_id)
    steps = {s.step_id: s for s in run.data.steps}
    result = {}
    for index in roster.data.source_indices:
        step = steps.get(quantity_step(roster, index))
        if step and step.result_refs:
            result[index] = space.store().read_record(step.result_refs[-1])
    return result


def result_status(space, execution_id):
    handle = get_execution(space, execution_id)
    roster = saved_roster(space, execution_id)
    if roster is None:
        return None
    if (
        handle.input_snapshot.options["extraction_scope"] == "roster"
        or not roster.data.stars
    ):
        return roster.data.status
    quantities = saved_quantities(space, execution_id, roster)
    if len(quantities) != len(roster.data.stars):
        return None
    return (
        "complete"
        if roster.data.status == "complete"
        and all(q.data.status == "complete" for q in quantities.values())
        else "partial"
    )


def current_result(space, execution_id):
    raw = space.store().get_meta(PREFIX + execution_id)
    if not raw:
        return None
    ref = ArtifactRef.model_validate_json(raw)
    value = ExtractionRun.model_validate_json(space.read_file(ref))
    handle = get_execution(space, execution_id)
    expected = ArtifactRef.model_validate_json(
        handle.input_snapshot.options["extraction_input"]
    )
    if value.execution_id != execution_id or value.input_ref != expected:
        raise ValueError(
            "The result generation belongs to a different fixed execution."
        )
    trace = read_json(space, value.trace_ref)
    from ..records.common import RecordRef
    from ..workspace import WorkspaceIntegrityError
    from ..workspace.store import verify_artifact_refs

    problems = verify_artifact_refs(
        [
            {
                "record_id": execution_id,
                "revision": 1,
                "manifest": value.model_dump(mode="json"),
            }
        ],
        space.root,
    )
    if problems:
        raise WorkspaceIntegrityError("; ".join(problems))
    for snapshot in trace["snapshots"]:
        record = space.store().read_record(RecordRef.model_validate(snapshot["ref"]))
        if (
            record is None
            or sha256_of(record.model_dump(mode="json", by_alias=True))
            != snapshot["sha256"]
        ):
            raise WorkspaceIntegrityError("科学记录与发布结果不一致")
    document = read_json(space, value.input_ref)
    science = (
        LiteratureContribution.model_validate_json(space.read_file(value.result_ref))
        if value.result_ref
        else None
    )
    roster = (
        space.store().read_record(value.stages.roster.snapshot_ref)
        if value.stages.roster.snapshot_ref
        else None
    )
    quantities = {
        stage.source_index: space.store().read_record(stage.snapshot_ref)
        for stage in value.stages.quantities
        if stage.snapshot_ref
    }
    verify_result_audit(space, value, document, science, roster, quantities)
    return ref, value


def publish_result(space, execution_id, *, closed=False, hook=None):
    with space.lock():
        handle = get_execution(space, execution_id)
        run = space.store().read_record_current(handle.run_ref.record_id)
        document, _, _ = read_input(space, handle.input_snapshot)
        roster = saved_roster(space, execution_id)
        quantities = saved_quantities(space, execution_id, roster) if roster else {}
        status = result_status(space, execution_id)
        scope = handle.input_snapshot.options["extraction_scope"]
        validations = []
        for ref in run.data.artifacts:
            if "/outputs/validation-" in ref.path:
                value = read_json(space, ref)
                validations.append({**value, "validation_ref": ref.model_dump()})
        calls = [
            {"step": s.step_id, "attempt": n, **a.model_dump(mode="json")}
            for s in run.data.steps
            for n, a in enumerate(s.attempts, 1)
        ]
        for call in calls:
            match = (
                re.search(
                    r"-(\d{2})(?:-retry-[a-f0-9]+)?-attempt-(\d{3})\.json$",
                    Path(call["request"]["path"]).name,
                )
                if call["request"]
                else None
            )
            call["round"] = int(match[1]) if match else None
            call["network_attempt"] = int(match[2]) if match else None
            call["completion_unknown"] = call["state"] == "pending" or bool(
                call.get("response")
                and read_json(space, ArtifactRef.model_validate(call["response"])).get(
                    "uncertain"
                )
            )
        trace = {
            "format": "hvs.extraction-trace",
            "version": 1,
            "execution_id": execution_id,
            "calls": calls,
            "validations": validations,
            "snapshots": [
                {
                    "ref": r.ref().model_dump(),
                    "sha256": sha256_of(r.model_dump(mode="json", by_alias=True)),
                }
                for r in [roster, *quantities.values()]
                if r
            ],
        }
        trace_ref = publish_json(
            space,
            space.run_path(execution_id, f"traces/{sha256_of(trace)}.json"),
            trace,
        )
        steps = {s.step_id: s for s in run.data.steps}

        def summary(record, step_id, index=None):
            step = steps.get(step_id)
            count = len([v for v in validations if v["step"] == step_id])
            if record and record.execution_id != execution_id:
                original = get_execution(space, record.execution_id)
                original_run = space.store().read_record_current(
                    original.run_ref.record_id
                )
                count = len(
                    [
                        r
                        for r in original_run.data.artifacts
                        if "/outputs/validation-roster-" in r.path
                    ]
                )
            return StageSummary(
                state="finished" if record else (step.state if step else "created"),
                status=record.data.status if record else None,
                snapshot_ref=record.ref() if record else None,
                source_execution=record.execution_id if record else None,
                rounds_completed=count,
                source_round=record.data.source_round if record else None,
                fallback=record.data.fallback if record else None,
                notes_available=record.data.notes is not None if record else None,
                source_index=index,
            ).model_dump(mode="json")

        science = None
        if scope == "full" and status in {"complete", "partial"}:
            notes = [roster.data.notes] if roster.data.notes else []
            stars = []
            for index, star in zip(
                roster.data.source_indices, roster.data.stars, strict=True
            ):
                numerical = quantities[index]
                stars.append(
                    {
                        **star.model_dump(mode="json"),
                        "quantities": [
                            q.model_dump(mode="json") for q in numerical.data.quantities
                        ],
                    }
                )
                if numerical.data.notes:
                    notes.append(
                        star.identifiers[0].value + ": " + numerical.data.notes
                    )
            science = LiteratureContribution.model_validate(
                {"metadata": document["metadata"], "stars": stars, "notes": notes}
            )
        usage = [
            a.usage.tokens if a.usage else None
            for s in run.data.steps
            for a in s.attempts
        ]
        finished = (
            max(
                [
                    r.created_at
                    for r in [roster, *quantities.values()]
                    if r and r.execution_id == execution_id
                ],
                default=run.created_at,
            )
            if run.data.state in {"finished", "cancelled"}
            else None
        )
        payload = {
            "format": "hvs.extraction-run",
            "version": 1,
            "execution_id": execution_id,
            "scope": scope,
            "status": {
                "execution": run.data.state,
                "result": status,
                "resumable": not closed,
            },
            "timestamps": {
                "started_at": space.store().get_execution(execution_id).created_at,
                "finished_at": finished.isoformat() if finished else None,
            },
            "input_ref": json.loads(handle.input_snapshot.options["extraction_input"]),
            "stages": {
                "roster": summary(roster, "roster"),
                "quantities": [
                    summary(quantities.get(i), quantity_step(roster, i), i)
                    for i in roster.data.source_indices
                ]
                if roster and scope == "full"
                else [],
            },
            "usage": {
                "attempts": len(usage),
                "known_tokens": sum(v for v in usage if v is not None),
                "total_tokens": sum(usage)
                if all(v is not None for v in usage)
                else None,
            },
            "errors": [],
            "trace_ref": trace_ref.model_dump(),
            "result_ref": None,
        }
        for error in run.data.errors:
            step = next(
                (s for s in run.data.steps if error.error_id in s.error_refs), None
            )
            payload["errors"].append(
                {
                    "stage": step.step_id if step else None,
                    "source_index": next(
                        (
                            i
                            for i in quantities
                            if step and quantity_step(roster, i) == step.step_id
                        ),
                        None,
                    ),
                    "kind": error.kind,
                    "message": error.message,
                }
            )
        for record in [roster, *quantities.values()]:
            if record and record.data.issues:
                payload["errors"].append(
                    {
                        "stage": "roster" if record is roster else "quantities",
                        "source_index": getattr(record.data, "source_index", None),
                        "kind": "submission_invalid",
                        "message": "The stage did not pass all checks.",
                        "snapshot_ref": record.ref().model_dump(),
                    }
                )
        generation = sha256_of(
            {
                "run": payload,
                "science": science.model_dump(mode="json") if science else None,
            }
        )
        folder = space.run_path(execution_id, f"results/{generation}")
        refs = [trace_ref]
        warning_mode = uses_quote_warnings(
            handle.input_snapshot.options.get("evidence_policy")
        )
        if science:
            science_folder = space.run_path(
                execution_id,
                "results/science/" + sha256_of(science.model_dump(mode="json")),
            )
            if warning_mode:
                science_folder = folder
            ref = space.publish_file(
                science_folder + "/literature_contribution.json",
                (
                    json.dumps(
                        science.model_dump(mode="json"), ensure_ascii=False, indent=2
                    )
                    + "\n"
                ).encode(),
            )
            payload["result_ref"] = ref.model_dump()
            refs.append(ref)
            if warning_mode:
                from ..llm.prompts import PromptInput

                locations = PromptInput.model_validate(
                    read_json(
                        space, ArtifactRef.model_validate(document["material_ref"])
                    )
                ).citation_locations
                mismatches = retained_mismatches(
                    space, science, roster, quantities, locations
                )
                audit = make_audit(science, ref, mismatches)
                audit_ref = space.publish_file(
                    folder + "/evidence_audit.json",
                    (audit.model_dump_json(indent=2) + "\n").encode(),
                )
                payload["evidence_audit_ref"] = audit_ref.model_dump()
                refs.append(audit_ref)
        value = ExtractionRun.model_validate(payload)
        verify_result_audit(space, value, document, science, roster, quantities)
        ref = space.publish_file(
            folder + "/run.json", (value.model_dump_json(indent=2) + "\n").encode()
        )
        refs.append(ref)
        if hook:
            hook("before_result_commit")
        marker = space.store().get_meta(PREFIX + execution_id)
        if marker != ref.model_dump_json():
            new_run = run.model_copy(
                update={
                    "revision": run.revision + 1,
                    "data": run.data.model_copy(
                        update={
                            "artifacts": [
                                *run.data.artifacts,
                                *[r for r in refs if r not in run.data.artifacts],
                            ]
                        }
                    ),
                }
            )
            space.store().commit(
                CommitRequest(
                    operation_key="publish-extraction:"
                    + execution_id
                    + ":"
                    + generation,
                    kind="extraction_result",
                    new_records=[new_run],
                    expected_revisions={run.record_id: run.revision},
                    execution_id=execution_id,
                    meta_updates={
                        PREFIX + execution_id: ref.model_dump_json(),
                        **(
                            {"retained_result:" + execution_id: ref.model_dump_json()}
                            if closed
                            else {}
                        ),
                    },
                )
            )
        if hook:
            hook("after_result_commit")
        return ref, value
