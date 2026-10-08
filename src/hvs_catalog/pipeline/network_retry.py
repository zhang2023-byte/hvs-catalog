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
_OBJECT_PREFIX = "object-retry-"
_FORMAT_PREFIX = "format-corrections-"


def _intents(space, run):
    for ref in run.data.artifacts:
        path = Path(ref.path)
        if path.parent.name == "recovery" and path.name.startswith(
            (_PREFIX, _OBJECT_PREFIX, _FORMAT_PREFIX)
        ):
            yield json.loads(space.read_file(ref))


def _selected_steps(space, execution_id, requested):
    from .roster import saved_roster

    roster = saved_roster(space, execution_id)
    if roster is None:
        raise ValueError("object selection requires a saved roster")
    selected = set()
    for identifier in requested:
        matches = [
            entry
            for entry in roster.data.entries
            if identifier in {entry.item_id, entry.contribution_id}
            or identifier in {item.value for item in entry.identifiers}
        ]
        if len(matches) != 1:
            raise ValueError("object selection is absent or ambiguous: " + identifier)
        selected.add("quantities-" + matches[0].item_id)
    return selected


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


def retry_network_failures(space, execution_id, *, request_key, object_ids=None):
    """Reopen only proven-unsent failures; this action itself makes no model calls.

    Resume then reuses successful steps/corrections and sends a new bounded logical
    call for the failed request and its dependent corrections. Uncertain uploads,
    provider completions and model-format/scientific failures are never authorized.
    Every action is an immutable artifact and a transactional run revision.
    """
    if not request_key:
        raise ValueError("network retry requires a nonempty request_key")
    requested = sorted(set(object_ids)) if object_ids is not None else None
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
                    or intent["requested_objects"] != requested
                ):
                    raise ValueError("network retry key is bound to another selection")
                return {**intent, "reused": True}
        require_resumable(space, execution_id)
        scope = execution.input_snapshot.options.get("extraction_scope")
        if scope not in {"roster", "full"} or run.data.state != "failed":
            raise ValueError("network retry requires a failed prepared extraction")
        if execution.input_snapshot.expert_adjudication:
            raise ValueError("network retry requires automatic extraction")
        selected_steps = None
        if requested is not None:
            selected_steps = _selected_steps(space, execution_id, requested)
        targets = []
        for step in run.data.steps:
            if step.state == "finished" or not step.attempts:
                continue
            if step.step_id != "roster" and not step.step_id.startswith("quantities-"):
                continue
            if selected_steps is not None and step.step_id not in selected_steps:
                continue
            attempt = step.attempts[-1]
            if attempt.response is None or attempt.request is None:
                continue
            envelope = json.loads(space.read_file(attempt.response))
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
        if not targets or (
            selected_steps is not None
            and {target["step_id"] for target in targets} != selected_steps
        ):
            raise ValueError(
                "selection contains no proven-unsent network failure; format and uncertain sent calls require a different action"
            )
        intent = {
            "kind": "network",
            "execution_id": execution_id,
            "request_key": request_key,
            "requested_objects": requested,
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


def retry_failed_objects(space, execution_id, *, request_key, object_ids):
    """Explicitly re-extract selected format-failed objects using frozen input.

    This is a fresh bounded generation, not a network retry or an automatic
    response to low scores. A completed provider response and a terminal local
    format-validation failure are required. Successful objects are ineligible.
    """
    requested = sorted(set(object_ids))
    if not request_key or not requested:
        raise ValueError("object retry requires a request key and selected objects")
    token = sha256_of({"request_key": request_key, "kind": "object-reextract"})[:20]
    with space.lock():
        execution = get_execution(space, execution_id)
        run = space.store().read_record(execution.run_ref)
        for intent in _intents(space, run):
            if intent["request_key"] == request_key:
                if (
                    intent.get("kind") != "object-reextract"
                    or intent["requested_objects"] != requested
                ):
                    raise ValueError("object retry key is bound to another selection")
                return {**intent, "reused": True}
        require_resumable(space, execution_id)
        if (
            execution.input_snapshot.options.get("extraction_scope") != "full"
            or execution.input_snapshot.expert_adjudication
            or run.data.state != "failed"
        ):
            raise ValueError("object retry requires a failed automatic PDF extraction")
        selected = _selected_steps(space, execution_id, requested)
        targets = []
        for step in run.data.steps:
            if (
                step.step_id not in selected
                or step.state == "finished"
                or not step.attempts
            ):
                continue
            attempt = step.attempts[-1]
            if attempt.request is None or attempt.response is None:
                continue
            envelope = json.loads(space.read_file(attempt.response))
            exchange = envelope.get("exchange") or {}
            body = exchange.get("body") or {}
            completed = bool(envelope.get("result")) or (
                exchange.get("status") == 200
                and any(
                    choice.get("finish_reason")
                    in {"stop", "tool_calls", "function_call", "length"}
                    for choice in body.get("choices", [])
                )
            )
            if (
                envelope.get("uncertain")
                or envelope.get("error") not in (None, "invalid_tool_arguments")
                or not completed
                or not any(
                    step.step_id.removeprefix("quantities-") + ": " + marker
                    in error.message
                    for error in run.data.errors
                    if error.kind == "flow_failure"
                    for marker in (
                        "Quantity format correction exhausted",
                        "No safe local format repair",
                    )
                )
            ):
                continue
            prepared = read_json(space, attempt.request)
            targets.append(
                {
                    "step_id": step.step_id,
                    "first_round": 0,
                    "request_ref": attempt.request.model_dump(mode="json"),
                    "response_ref": attempt.response.model_dump(mode="json"),
                    "prepared_sha256": sha256_of(prepared),
                }
            )
        if {t["step_id"] for t in targets} != selected:
            raise ValueError(
                "selection must contain only completed format-failed objects"
            )
        intent = {
            "kind": "object-reextract",
            "execution_id": execution_id,
            "request_key": request_key,
            "requested_objects": requested,
            "input_digest": execution.input_digest,
            "previous_run_ref": run.ref().model_dump(mode="json"),
            "token": token,
            "targets": targets,
        }
        ref = space.publish_run_file(
            execution_id,
            f"recovery/{_OBJECT_PREFIX}{token}.json",
            canonical_json(intent).encode(),
        )
        append_run_revision(
            space,
            execution,
            state="running",
            artifacts=[ref],
            operation_key=f"object-retry:{execution_id}:{token}",
        )
        return {**intent, "reused": False}


def continue_format_corrections(space, execution_id, *, request_key, maximum=2):
    """Append missing corrections to a completed response without a new generation.

    The original configuration and call keys remain immutable. This explicit
    allowance applies to the remaining corrections and unprocessed descendants;
    it never resets the number already consumed or changes successful steps.
    """
    from .configuration import stored_configuration

    if not request_key or type(maximum) is not int or not 1 <= maximum <= 2:
        raise ValueError("format continuation requires a key and maximum 1 or 2")
    token = sha256_of({"request_key": request_key, "kind": "format-corrections"})[:20]
    with space.lock():
        execution = get_execution(space, execution_id)
        run = space.store().read_record(execution.run_ref)
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
                raise ValueError(
                    "format continuation allowance already fixed; budget cannot reset"
                )
        require_resumable(space, execution_id)
        if (
            run.data.state != "failed"
            or execution.input_snapshot.expert_adjudication
            or execution.input_snapshot.options.get("extraction_scope")
            not in {"roster", "full"}
            or stored_configuration(execution.input_snapshot).max_format_corrections
            >= maximum
        ):
            raise ValueError(
                "format continuation requires a failed automatic extraction with unused allowance"
            )
        targets = []
        for step in run.data.steps:
            if (
                step.state == "finished"
                or not step.attempts
                or not (
                    step.step_id == "roster" or step.step_id.startswith("quantities-")
                )
            ):
                continue
            attempt = step.attempts[-1]
            if attempt.request is None or attempt.response is None:
                raise ValueError(
                    "format continuation requires archived requests and responses"
                )
            response = json.loads(space.read_file(attempt.response))
            body = (response.get("exchange") or {}).get("body") or {}
            if (
                response.get("uncertain")
                or response.get("error")
                not in {
                    None,
                    "invalid_tool_arguments",
                    "missing_or_multiple_tool_calls",
                }
                or not body.get("choices")
            ):
                raise ValueError(
                    "format continuation cannot retry network or unknown failures"
                )
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


def object_retry_token(ctx, step_id):
    run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
    token = None
    for intent in _intents(ctx.space, run):
        if intent["input_digest"] != ctx.execution.input_digest:
            raise ValueError("retry intent differs from frozen extraction input")
        if intent.get("kind") == "object-reextract" and any(
            t["step_id"] == step_id for t in intent["targets"]
        ):
            token = intent["token"]
    return token


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
