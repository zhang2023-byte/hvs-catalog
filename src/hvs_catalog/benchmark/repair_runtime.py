"""Durable, bounded repair scheduling over the common model-call runtime."""

import copy
import random
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field

from ..llm.concurrent import ConcurrentCalls
from ..llm.config import ModelConfig, RuntimeConfig, load_credential
from ..llm.managed import FORMAT_ERRORS, CallService, ModelCallFlow, get_call
from ..llm.transport import TokenDanceAdapter
from ..pipeline.roster import correction_messages
from ..records import quantity_validation, roster_validation
from ..records.common import Config, sha256_of
from ..records.evidence_audit import uses_quote_warnings
from ..records.response import response_message
from ..records.runs import ModelSpec
from ..runtime import build_input_snapshot, create_execution, resume_execution
from ..workspace import initialize_workspace
from ..workspace.archives import publish_json
from .repair_source import (
    descendant_jobs,
    job_locations,
    read_file,
    repair_folder,
    request_from_prepared,
    retryable_network,
    verify_sources,
    write_json,
)


class RepairPolicy(Config):
    initial_probe: bool = False
    preview_concurrency: int = Field(default=5, ge=1, le=100)
    other_concurrency: int = Field(default=10, ge=1, le=100)
    preview_spacing: float = Field(default=2, ge=0)
    other_spacing: float = Field(default=1, ge=0)
    preview_attempts: int = Field(default=10, ge=1, le=10)
    other_attempts: int = Field(default=6, ge=1, le=10)
    backoff_base: float = Field(default=10, gt=0)
    backoff_cap: float = Field(default=300, gt=0)
    circuit_threshold: int = Field(default=5, ge=1)
    circuit_seconds: float = Field(default=300, gt=0)
    half_open_failures: int = Field(default=3, ge=1)
    version_seconds: float = Field(default=7200, gt=0)
    batch_seconds: float = Field(default=172800, gt=0)

    def delay(self, attempt, retry_after=None, *, rng=random.uniform):
        cap = min(self.backoff_cap, self.backoff_base * 2 ** min(attempt - 1, 20))
        return max(float(retry_after or 0), rng(cap / 2, cap))


class RepairCallFlow(ModelCallFlow):
    mode = "evaluation"


class FrozenAdapter:
    """Keep historical preparation and response-policy metadata byte-for-byte."""

    name = "tokendance"

    def __init__(self, prepared, delegate):
        self.prepared, self.delegate = prepared, delegate

    def validate_runtime(self, runtime):
        self.delegate.validate_runtime(runtime)

    def prepare(self, request):
        request_from_prepared(self.prepared, request.model)
        return copy.deepcopy(self.prepared)

    def send(self, payload, runtime, credential):
        return self.delegate.send(payload, runtime, credential)

    def parse(self, exchange, request):
        return self.delegate.parse(exchange, request)


def _start_call(space, job, runtime, credential, delegate):
    model = ModelConfig.model_validate(job["model"])
    request = request_from_prepared(job["prepared"], model)
    fixed = {"request": request.model_dump(mode="json"), "prepared": job["prepared"]}
    if credential.get_secret_value() in json_text(fixed):
        raise ValueError("Credential must not occur in model inputs.")
    with space.lock():
        ref = publish_json(space, f"runs/inputs/llm/{sha256_of(fixed)}.json", fixed)
    origin = {
        "stage": job["id"],
        "paper": job["paper"],
        "source": job["source"],
        "submission_round": job["submission_round"],
        "new_attempt": job["network_attempt"],
    }
    snapshot = build_input_snapshot(
        source_refs=[],
        method=None,
        expert_adjudication=False,
        models=[
            ModelSpec(
                step_id="model_call",
                model=model.model,
                provider=model.provider,
                parameters=model.parameters or {},
            )
        ],
        options={
            "llm_input": ref.model_dump_json(),
            "llm_runtime": runtime.model_dump_json(),
            "network_repair_origin": json_text(origin),
        },
    )
    handle, _ = create_execution(
        space,
        request_key=sha256_of(origin),
        input_snapshot=snapshot,
        flow=RepairCallFlow(),
    )
    return handle.execution_id


def json_text(value):
    from ..records.common import canonical_json

    return canonical_json(value)


def _finish(job, *, reason=None):
    job["state"] = "finished"
    job["call_execution"] = None
    job["stop_reason"] = reason
    selected = next((v for v in reversed(job["validations"]) if v["readable"]), None)
    if selected is not None:
        job["fragment"] = {
            **selected,
            "status": "complete"
            if selected["passed"] and not reason
            else "partial"
            if selected["values"]
            else "failed",
        }


def _gateway_code(envelope):
    body = (envelope.get("exchange") or {}).get("body") or {}
    if not isinstance(body, dict):
        return None
    error = body.get("error") or body
    return error.get("code") if isinstance(error, dict) else None


def _route_result(route, envelope, policy, now):
    status = (envelope.get("exchange") or {}).get("status")
    code = _gateway_code(envelope)
    if status == 404:
        route["stopped"] = "model_route_unavailable"
        return
    if (
        status in {401, 403}
        or code == "insufficient_quota"
        or envelope.get("error") in {"authentication", "budget_insufficient"}
    ):
        route["stopped"] = code or envelope.get("error") or str(status)
        return
    failed = envelope.get("error") is not None and (status != 200)
    if status == 200:
        route["initial_probe_passed"] = True
    transport = (envelope.get("exchange") or {}).get("transport") or {}
    burst = status in {503, 429} or (
        envelope.get("error") in {"connection_failed", "completion_unknown"}
        and transport.get("phase") == "connect"
    )
    if route.get("half_open"):
        route["half_open"] = False
        if failed:
            route["probe_failures"] = route.get("probe_failures", 0) + 1
            if route["probe_failures"] >= policy.half_open_failures:
                route["stopped"] = "circuit_exhausted"
            route["open_until"] = now + policy.circuit_seconds
        else:
            route.update(open_until=0, consecutive=0, probe_failures=0)
    elif burst:
        route["consecutive"] = route.get("consecutive", 0) + 1
        if route["consecutive"] >= policy.circuit_threshold:
            route["open_until"] = now + policy.circuit_seconds
    else:
        route["consecutive"] = 0


def _consume(root, folder, space, facts, state, job, runtime, policy, limit, now):
    call = get_call(space, job["call_execution"])
    env = call["response"]
    if env is None:
        _finish(job, reason="unattributed_completion_no_response")
        return
    attempt = call["attempts"][-1]
    row = {
        "execution_id": call["execution_id"],
        "submission_round": job["submission_round"],
        "new_attempt": job["network_attempt"],
        **attempt,
        "error": env.get("error"),
        "http_status": (env.get("exchange") or {}).get("status"),
    }
    job["calls"].append(row)
    job["call_execution"] = None
    _route_result(state["route"], env, policy, now)
    if retryable_network(env, runtime):
        if (
            job["network_attempt"] >= limit
            or now - job["version_started"] >= policy.version_seconds
        ):
            _finish(job, reason=env.get("error") or "network_exhausted")
        else:
            job["state"] = "ready"
            job["next_due"] = now + policy.delay(
                job["network_attempt"], env.get("retry_after")
            )
        if job["state"] == "finished":
            _spawn_children(facts, state, job)
        return
    if env.get("error") and env["error"] not in FORMAT_ERRORS:
        _finish(job, reason=env["error"])
        _spawn_children(facts, state, job)
        return
    validator = roster_validation if job["kind"] == "roster" else quantity_validation
    warning_mode = uses_quote_warnings(job["prepared"].get("evidence_policy"))
    check = validator.check_response(
        env, job_locations(facts, job), quote_warnings=warning_mode
    )
    values, indices, rejected, notes = (
        validator.select_partial(check, job_locations(facts, job))
        if check.readable
        else ([], [], {}, check.notes)
    )
    validation = {
        "response": attempt["response"],
        "request": attempt["request"],
        "submission_round": job["submission_round"],
        "passed": check.passed,
        "readable": check.readable,
        "values": [v.model_dump(mode="json") for v in values],
        "indices": indices,
        "rejected": {k: [i.model_dump() for i in v] for k, v in rejected.items()},
        "issues": [i.model_dump() for i in check.issues],
        "notes": notes,
    }
    if warning_mode:
        validation["warnings"] = [w.model_dump() for w in check.warnings]
    job["validations"].append(validation)
    if check.passed or job["submission_round"] >= job["format_limit"]:
        _finish(job)
    else:
        model = ModelConfig.model_validate(job["model"])
        request = request_from_prepared(job["prepared"], model)
        request.messages.extend(
            correction_messages(
                response_message(env),
                check.issues,
                effective_prior=response_message(env, effective=True),
                tool="submit_" + job["kind"],
            )
        )
        updated = TokenDanceAdapter().prepare(request)
        # Historical policy stays fixed during this stage's remaining corrections.
        if "evidence_policy" in job["prepared"]:
            updated["evidence_policy"] = job["prepared"]["evidence_policy"]
        if "response_policy" in job["prepared"]:
            updated["response_policy"] = job["prepared"]["response_policy"]
        job.update(
            prepared=updated,
            submission_round=job["submission_round"] + 1,
            network_attempt=0,
            version_started=None,
            next_due=0,
            state="ready",
        )
    _spawn_children(facts, state, job)


def _spawn_children(facts, state, job):
    if job["state"] == "finished" and job["kind"] == "roster" and job["fragment"]:
        children = descendant_jobs(facts[job["paper"]], job, job["fragment"])
        existing = {j["id"] for j in state["jobs"]}
        state["jobs"].extend(j for j in children if j["id"] not in existing)


def run_repair(
    root,
    name,
    *,
    label="network-repair-01",
    credential=None,
    env_file=None,
    adapter=None,
    clock=None,
):
    """Advance only prepared repair targets; restarting never resets attempt counts."""
    from .repair_source import prepare_repair

    folder = repair_folder(root, name, label)
    if not (folder / "manifest.json").exists():
        prepare_repair(root, name, label=label)
    return run_prepared_repair(
        root,
        folder,
        credential=credential,
        env_file=env_file,
        adapter=adapter,
        clock=clock,
    )


def run_prepared_repair(
    root, folder, *, credential=None, env_file=None, adapter=None, clock=None
):
    """Run a prepared repair in its own call workspace, independent of source layout."""
    from ..pipeline.inputs import code_fingerprint

    folder = Path(folder).resolve()
    manifest = read_file(folder / "manifest.json")
    if manifest["repair_code_sha256"] != code_fingerprint():
        raise ValueError("Repair code differs from its frozen preparation.")
    verify_sources(root, manifest)
    facts = read_file(folder / "source-facts.json")
    if sha256_of(facts) != manifest["source_facts_sha256"]:
        raise ValueError("Frozen repair inputs changed.")
    policy = RepairPolicy.model_validate(manifest["policy"])
    state = read_file(folder / "state.json")
    if state["finished"] is not None:
        return repair_status(folder)
    preview = manifest["configuration"]["default_model"]["provider"] == "bocha"
    concurrency = policy.preview_concurrency if preview else policy.other_concurrency
    spacing = policy.preview_spacing if preview else policy.other_spacing
    limit = policy.preview_attempts if preview else policy.other_attempts
    runtime = RuntimeConfig.model_validate(
        {
            **manifest["configuration"]["runtime"],
            "max_attempts": 1,
            "connect_timeout": 20.0,
            "upload_timeout": 180.0,
            "idle_timeout": 300.0,
            "total_timeout": 1800.0,
            "upload_concurrency": concurrency,
        }
    )
    credential = credential or load_credential(runtime, env_file=env_file)
    delegate = adapter or TokenDanceAdapter()
    clock = clock or time
    space = initialize_workspace(folder / ".calls")
    dispatcher = ConcurrentCalls(concurrency)
    active = {}

    def save():
        write_json(folder / "state.json", state)

    try:
        # One repair coordinator owns this entire run, including its network waits.
        with space.lock():
            state["started"] = state["started"] or clock.time()
            for job in state["jobs"]:
                if job["call_execution"]:
                    _consume(
                        root,
                        folder,
                        space,
                        facts,
                        state,
                        job,
                        runtime,
                        policy,
                        limit,
                        clock.time(),
                    )
            save()
            while any(j["state"] != "finished" for j in state["jobs"]) or active:
                now = clock.time()
                if now - state["started"] >= policy.batch_seconds:
                    state["route"]["stopped"] = "batch_time_limit"
                route = state["route"]
                if route.get("stopped"):
                    for job in state["jobs"]:
                        if job["state"] == "ready":
                            _finish(job, reason="route_stopped:" + route["stopped"])
                opened = bool(route.get("open_until"))
                ready = [
                    j
                    for j in state["jobs"]
                    if j["state"] == "ready" and j["next_due"] <= now
                ]
                if (
                    ready
                    and not route.get("stopped")
                    and len(active)
                    < (
                        1
                        if policy.initial_probe
                        and not route.get("initial_probe_passed")
                        else concurrency
                    )
                    and now >= state["next_start"]
                    and now >= route.get("open_until", 0)
                    and (not opened or not active)
                ):
                    job = ready[0]
                    if (
                        job["version_started"] is not None
                        and now - job["version_started"] >= policy.version_seconds
                    ):
                        _finish(job, reason="submission_time_limit")
                        save()
                        continue
                    if opened:
                        route["half_open"] = True
                    job["network_attempt"] += 1
                    job["version_started"] = job["version_started"] or now
                    job["call_execution"] = _start_call(
                        space, job, runtime, credential, delegate
                    )
                    job["state"] = "running"
                    state["next_start"] = now + spacing
                    save()
                    service = CallService(
                        runtime,
                        credential=credential,
                        adapter=FrozenAdapter(job["prepared"], delegate),
                    )
                    service.dispatcher = dispatcher
                    active[job["call_execution"]] = (job, service)
                    resume_execution(
                        space, job["call_execution"], RepairCallFlow(), service=service
                    )
                    save()
                completed = dispatcher.collect(wait_for_result=bool(active))
                for key in completed:
                    execution_id = key[1]
                    job, service = active.pop(execution_id)
                    resume_execution(
                        space, execution_id, RepairCallFlow(), service=service
                    )
                    _consume(
                        root,
                        folder,
                        space,
                        facts,
                        state,
                        job,
                        runtime,
                        policy,
                        limit,
                        clock.time(),
                    )
                    save()
                if not active and not completed:
                    clock.sleep(0.1)
            state["finished"] = clock.time()
            save()
    finally:
        dispatcher.close()
        space.close()
    verify_sources(root, manifest)
    status = repair_status(folder)
    write_json(folder / "run.json", status)
    return status


def repair_status(folder):
    state = read_file(folder / "state.json")
    jobs = state["jobs"]
    return {
        "format": "hvs.network-repair-status",
        "version": 1,
        "started_at": datetime.fromtimestamp(state["started"], UTC).isoformat()
        if state["started"]
        else None,
        "finished_at": datetime.fromtimestamp(state["finished"], UTC).isoformat()
        if state["finished"]
        else None,
        "targets": len(jobs),
        "attempts": sum(len(j["calls"]) for j in jobs),
        "remaining": sum(j["state"] != "finished" for j in jobs),
        "complete": sum(
            bool(j["fragment"] and j["fragment"]["status"] == "complete") for j in jobs
        ),
        "partial": sum(
            bool(j["fragment"] and j["fragment"]["status"] == "partial") for j in jobs
        ),
        "route": state["route"],
    }
