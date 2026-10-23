"""Execute bounded submission rounds and commit one immutable stage snapshot."""

from pathlib import Path

from ..llm.managed import CallFailure, execute_model_call
from ..records.common import ArtifactRef, build_record, canonical_json, sha256_of
from ..records.quantities import QuantityData, QuantityRecord
from ..records.roster import RosterData, RosterIssue, RosterRecord
from ..records.roster_validation import response_message
from ..runtime.execution import append_run_revision
from .network_retry import format_correction_limit, retry_call_key


def quantity_step(roster, source_index):
    return f"quantities-{sha256_of(roster.ref().model_dump())[:12]}-{source_index}"


def run_stage(ctx, request, source, inputs, *, roster=None, source_index=None):
    from ..records import quantity_validation, roster_validation
    from .roster import correction_messages

    numerical = roster is not None
    validator = quantity_validation if numerical else roster_validation
    step = quantity_step(roster, source_index) if numerical else "roster"
    binding = {
        "request": request.model_dump(mode="json"),
        "target": {
            "roster_ref": roster.ref().model_dump(),
            "source_index": source_index,
        }
        if numerical
        else None,
    }
    initial = sha256_of(binding)
    limit = format_correction_limit(ctx)
    digest = sha256_of(
        {
            "input": initial,
            "limit": limit,
            "keys": [retry_call_key(ctx, step, n) for n in range(limit + 1)],
        }
    )
    for cache in (initial, digest):
        saved = ctx.reuse_step(step, cache)
        if saved is not None:
            return ctx.store.read_record(saved.result_refs[0])
    rounds = []
    failure = None
    for number in range(limit + 1):
        key = retry_call_key(ctx, step, number)
        try:
            envelope = execute_model_call(
                ctx,
                ctx.service,
                request,
                ctx.service.adapter.prepare(request),
                step_id=step,
                call_key=key,
            )
        except CallFailure as exc:
            failure = str(exc)
            if failure.startswith("completion_unknown"):
                raise
            break
        check = validator.check_response(envelope, inputs.citation_locations)
        run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
        current = next(s for s in run.data.steps if s.step_id == step)
        response = next(
            a.response
            for a in reversed(current.attempts)
            if a.request
            and Path(a.request.path).name.startswith(key + "-")
            and a.response
        )
        validation = {
            "step": step,
            "round": number,
            "response_ref": response.model_dump(),
            "readable": check.readable,
            "passed": check.passed,
            "issues": [i.model_dump() for i in check.issues],
        }
        ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            f"outputs/validation-{key}.json",
            canonical_json(validation).encode(),
        )
        append_run_revision(ctx.space, ctx.execution, artifacts=[ref])
        rounds.append((number, response, check))
        if check.passed:
            break
        if number < limit:
            request.messages.extend(
                correction_messages(
                    response_message(envelope),
                    check.issues,
                    tool="submit_quantities" if numerical else "submit_roster",
                )
            )
    selected = next((row for row in reversed(rounds) if row[2].readable), None)
    if selected:
        number, response, check = selected
        values, indices, rejected, notes = validator.select_partial(
            check, inputs.citation_locations
        )
        issues = list(check.issues)
        status = (
            "complete"
            if check.passed and failure is None
            else ("partial" if values else "failed")
        )
        fallback = number != rounds[-1][0]
    else:
        number, response, check = rounds[-1] if rounds else (None, None, None)
        values, indices, rejected, notes = [], [], {}, check.notes if check else None
        issues = list(check.issues) if check else []
        status, fallback = "failed", False
    if failure:
        issues.append(RosterIssue(path="/response", message=failure))
    common = {
        "source_ref": source.ref(),
        "material_ref": ArtifactRef.model_validate_json(
            ctx.execution.input_snapshot.options["prepared_prompt"]
        ),
        "response_ref": response,
        "source_round": number,
        "fallback": fallback,
        "status": status,
        "notes": notes,
        "rejected": rejected,
        "issues": issues,
    }
    if numerical:
        data = QuantityData(
            **common,
            roster_ref=roster.ref(),
            source_index=source_index,
            quantities=values,
            source_paths=indices,
        )
        model = QuantityRecord
    else:
        data = RosterData(**common, stars=values, source_indices=indices)
        model = RosterRecord
    record = build_record(
        model,
        record_id=f"{step}-{ctx.execution.execution_id}-{digest[:20]}",
        created_at=ctx.now(),
        execution_id=ctx.execution.execution_id,
        data=data,
    )
    ctx.service.hook("before_stage_commit")
    ctx.commit_step(
        step_id=step,
        input_digest=initial if status == "complete" else digest,
        records=[record],
        result_refs=[record.ref()],
    )
    ctx.service.hook("after_stage_commit")
    return record
