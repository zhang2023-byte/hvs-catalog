"""Explicit, durable recovery of unsent network calls at their existing request boundary."""

import json
import re
from pathlib import Path

from ..records.common import canonical_json, sha256_of
from ..runtime import get_execution
from ..runtime.execution import append_run_revision
from ..workspace.archives import read_json
from ..workspace.retention import require_resumable

_PREFIX = "network-retry-"
_FORMAT_PREFIX = "format-corrections-"


def _intents(space, run):
    for ref in run.data.artifacts:
        path = Path(ref.path)
        if path.parent.name == "recovery" and path.name.startswith(
            (_PREFIX, _FORMAT_PREFIX)
        ):
            yield json.loads(space.read_file(ref))


def _known_unsent(envelope):
    error = envelope.get("error")
    if error in {"connection_failed", "rate_limited"}:
        return not envelope.get("uncertain") and envelope.get("retryable") is True
    transport = (envelope.get("exchange") or {}).get("transport") or {}
    exceptions = {item.get("type") for item in transport.get("exceptions", [])}
    # Earlier phase-aware archives lack request_started. Their connect phase is
    # entered before HTTP request(), and upload is entered before any HTTP bytes.
    return (
        error == "completion_unknown"
        and transport.get("phase") == "connect"
        and transport.get("request_started") is not True
        and transport.get("response_bytes") == 0
        and bool(exceptions & {"ReadTimeout", "ConnectTimeout", "NewConnectionError"})
    )


def _active_steps(space, execution_id):
    from .roster import saved_roster
    from .stages import quantity_step

    roster = saved_roster(space, execution_id)
    return {
        "roster": None,
        **(
            {quantity_step(roster, i): i for i in roster.data.source_indices}
            if roster
            else {}
        ),
    }


def retry_network_failures(space, execution_id, *, request_key, source_indices=None):
    """Reopen only proven-unsent failures; this action itself makes no model calls.

    Resume then reuses successful steps/corrections and sends a new bounded logical
    call for the failed request and its dependent corrections. Uncertain uploads,
    provider completions and model-format/scientific failures are never authorized.
    Every action is an immutable artifact and a transactional run revision.
    """
    if not request_key:
        raise ValueError("network retry requires a nonempty request_key")
    requested = sorted(set(source_indices)) if source_indices is not None else None
    if requested == []:
        raise ValueError("object selection must not be empty")
    token = sha256_of({"request_key": request_key})[:20]
    with space.lock():
        execution = get_execution(space, execution_id)
        run = space.store().read_record(execution.run_ref)
        for intent in _intents(space, run):
            if intent["request_key"] == request_key:
                if (
                    intent.get("kind", "network") != "network"
                    or intent["requested_targets"] != requested
                ):
                    raise ValueError("network retry key is bound to another selection")
                return {**intent, "reused": True}
        require_resumable(space, execution_id)
        scope = execution.input_snapshot.options.get("extraction_scope")
        if scope not in {"roster", "full"} or run.data.state not in {
            "failed",
            "finished",
        }:
            raise ValueError("network retry requires a failed prepared extraction")
        if execution.input_snapshot.expert_adjudication:
            raise ValueError("network retry requires automatic extraction")
        targets = []
        active = _active_steps(space, execution_id)
        for step in run.data.steps:
            if step.step_id not in active:
                continue
            if not step.attempts:
                continue
            record = (
                space.store().read_record(step.result_refs[-1])
                if step.result_refs
                else None
            )
            if record and record.data.status == "complete":
                continue
            if step.step_id != "roster" and not step.step_id.startswith("quantities-"):
                continue
            if requested is not None and active[step.step_id] not in requested:
                continue
            attempt = step.attempts[-1]
            if attempt.response is None or attempt.request is None:
                continue
            envelope = read_json(space, attempt.response)
            if not _known_unsent(envelope):
                continue
            # Reading verifies the frozen original request before making any intent.
            prepared = read_json(space, attempt.request)
            match = re.fullmatch(
                re.escape(step.step_id)
                + r"-(\d{2})(?:-retry-[a-f0-9]+)?-attempt-\d{3}\.json",
                Path(attempt.request.path).name,
            )
            if match is None:
                raise ValueError("network retry lacks a fixed extraction call key")
            targets.append(
                {
                    "step_id": step.step_id,
                    "first_round": int(match[1]),
                    "request_ref": attempt.request.model_dump(mode="json"),
                    "response_ref": attempt.response.model_dump(mode="json"),
                    "prepared_sha256": sha256_of(prepared),
                }
            )
        if not targets:
            raise ValueError(
                "selection contains no proven-unsent network failure; format and uncertain sent calls require a different action"
            )
        intent = {
            "kind": "network",
            "execution_id": execution_id,
            "request_key": request_key,
            "requested_targets": requested,
            "input_digest": execution.input_digest,
            "previous_run_ref": run.ref().model_dump(mode="json"),
            "token": token,
            "targets": targets,
        }
        ref = space.publish_run_file(
            execution_id,
            f"recovery/{_PREFIX}{token}.json",
            canonical_json(intent).encode(),
        )
        append_run_revision(
            space,
            execution,
            state="running",
            artifacts=[ref],
            operation_key=f"network-retry:{execution_id}:{token}",
        )
        return {**intent, "reused": False}


def continue_format_corrections(space, execution_id, *, request_key, maximum=2):
    """Append missing corrections to a completed response without a new generation.

    The original configuration and call keys remain immutable. This explicit
    allowance applies to the remaining corrections and unprocessed descendants;
    it never resets the number already consumed or changes successful steps.
    """
    from ..llm.managed import FORMAT_ERRORS
    from .configuration import stored_configuration

    if not request_key or type(maximum) is not int or not 1 <= maximum <= 2:
        raise ValueError("format continuation requires a key and maximum 1 or 2")
    token = sha256_of({"request_key": request_key, "kind": "format-corrections"})[:20]
    with space.lock():
        execution = get_execution(space, execution_id)
        run = space.store().read_record(execution.run_ref)
        allowance = stored_configuration(
            execution.input_snapshot
        ).max_format_corrections
        for intent in _intents(space, run):
            if intent["request_key"] == request_key:
                if (
                    intent.get("kind") != "format-corrections"
                    or intent["maximum"] != maximum
                ):
                    raise ValueError(
                        "format continuation key is bound to another allowance"
                    )
                return {**intent, "reused": True}
            if intent.get("kind") == "format-corrections":
                allowance = max(allowance, intent["maximum"])
        require_resumable(space, execution_id)
        if (
            run.data.state not in {"failed", "finished"}
            or execution.input_snapshot.expert_adjudication
            or execution.input_snapshot.options.get("extraction_scope")
            not in {"roster", "full"}
            or allowance >= maximum
        ):
            raise ValueError(
                "format continuation requires a failed automatic extraction with unused allowance"
            )
        targets = []
        active = _active_steps(space, execution_id)
        for step in run.data.steps:
            if step.step_id not in active:
                continue
            if not step.attempts or (
                step.step_id != "roster" and not step.step_id.startswith("quantities-")
            ):
                continue
            record = (
                space.store().read_record(step.result_refs[-1])
                if step.result_refs
                else None
            )
            if record and record.data.status == "complete":
                continue
            attempt = step.attempts[-1]
            if attempt.request is None or attempt.response is None:
                raise ValueError(
                    "format continuation requires archived requests and responses"
                )
            response = read_json(space, attempt.response)
            body = (response.get("exchange") or {}).get("body") or {}
            if (
                response.get("uncertain")
                or response.get("error") not in {None, *FORMAT_ERRORS}
                or not body.get("choices")
            ):
                continue
            prepared = read_json(space, attempt.request)
            targets.append(
                {
                    "step_id": step.step_id,
                    "request_ref": attempt.request.model_dump(mode="json"),
                    "response_ref": attempt.response.model_dump(mode="json"),
                    "prepared_sha256": sha256_of(prepared),
                }
            )
        if not targets:
            raise ValueError("format continuation requires an unfinished submission")
        intent = {
            "kind": "format-corrections",
            "execution_id": execution_id,
            "request_key": request_key,
            "input_digest": execution.input_digest,
            "previous_run_ref": run.ref().model_dump(mode="json"),
            "token": token,
            "maximum": maximum,
            "targets": targets,
        }
        ref = space.publish_run_file(
            execution_id,
            f"recovery/{_FORMAT_PREFIX}{token}.json",
            canonical_json(intent).encode(),
        )
        append_run_revision(
            space,
            execution,
            state="running",
            artifacts=[ref],
            operation_key=f"format-corrections:{execution_id}:{token}",
        )
        return {**intent, "reused": False}


def format_correction_limit(ctx):
    from .configuration import stored_configuration

    limit = stored_configuration(ctx.execution.input_snapshot).max_format_corrections
    run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
    for intent in _intents(ctx.space, run):
        if intent["input_digest"] != ctx.execution.input_digest:
            raise ValueError("correction intent differs from frozen extraction input")
        if intent.get("kind") == "format-corrections":
            limit = max(limit, intent["maximum"])
    return limit


def retry_call_key(ctx, step_id, round_number):
    """Use the last applicable explicit intent; earlier successful calls retain keys."""
    base = f"{step_id}-{round_number:02d}"
    run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
    key = base
    for intent in _intents(ctx.space, run):
        if intent["input_digest"] != ctx.execution.input_digest:
            raise ValueError(
                "network retry intent differs from frozen extraction input"
            )
        if intent.get("kind") == "format-corrections":
            continue
        for target in intent["targets"]:
            if target["step_id"] == step_id and round_number >= target["first_round"]:
                key = base + "-retry-" + intent["token"]
    return key
