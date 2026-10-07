"""A durable single model-call flow using the existing workspace/runtime contract.

Request snapshots, attempts, raw responses and outcomes use normal RunRecord
artifacts. The adapter never writes business state or retries on its own.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from ..records.common import canonical_json, sha256_of
from ..records.runs import Attempt, ModelSpec, RunError, RunRecord, StepRecord, Usage
from ..runtime import (
    build_input_snapshot,
    create_execution,
    get_execution,
    resume_execution,
)
from ..runtime.deferred import WaitingForCall
from ..runtime.execution import DefaultFlowAdapter, append_run_revision
from ..workspace import Workspace, WorkspaceIntegrityError
from .config import CallRequest, RuntimeConfig, load_credential
from .quota import AccountQuota
from .transport import (
    ModelAdapter,
    OutputFailure,
    TokenDanceAdapter,
    TransportFailure,
    network_retry_allowed,
    token_usage,
)

STEP = "model_call"


class CallFailure(RuntimeError):
    """Already archived, safe diagnostic; no raw exception or credential values."""


class CallService:
    def __init__(
        self, runtime, *, credential=None, env_file=None, adapter=None, hooks=None
    ):
        self.runtime = runtime
        self._credential = credential
        self.env_file = env_file
        self.adapter = adapter or TokenDanceAdapter()
        self.hooks = hooks or {}
        self.dispatcher = None

    def credential(self):
        if self._credential is None:
            self._credential = load_credential(self.runtime, env_file=self.env_file)
        return self._credential

    def hook(self, name):
        if name in self.hooks:
            self.hooks[name]()


def _run(ctx):
    run = ctx.store.read_record_current(ctx.execution.run_ref.record_id)
    if not isinstance(run, RunRecord):
        raise WorkspaceIntegrityError("model call run record missing")
    return run


def _attempts(ctx, step_id=STEP):
    step = next((s for s in _run(ctx).data.steps if s.step_id == step_id), None)
    return list(step.attempts) if step else []


def _save(ctx, attempts, *, step_state="running", error_kind=None, step_id=STEP):
    error = None
    if error_kind:
        error = RunError(
            error_id="llm-error-" + uuid.uuid4().hex,
            kind=error_kind,
            message=error_kind,
        )
    counts = [a.usage.tokens if a.usage else None for a in attempts]
    total = sum(counts) if all(v is not None for v in counts) else None
    return append_run_revision(
        ctx.space,
        ctx.execution,
        step=StepRecord(
            step_id=step_id,
            state=step_state,
            input_refs=[],
            attempts=attempts,
            result_refs=[],
            error_refs=[error.error_id] if error else [],
        ),
        errors=[error] if error else [],
        usage=Usage(tokens=total),
    )


def _finalize(ctx, attempts, envelope, *, index=-1, step_id=STEP):
    attempt = attempts[index]
    error = envelope.get("error")
    uncertain = envelope.get("uncertain", False)
    attempt = attempt.model_copy(
        update={
            "state": "pending" if uncertain else ("failed" if error else "succeeded"),
            "finished_at": datetime.now(UTC) if not uncertain else None,
        }
    )
    attempts[index] = attempt
    _save(
        ctx,
        attempts,
        error_kind="completion_unknown" if uncertain else error,
        step_id=step_id,
    )
    return attempt


def _archive_raw(ctx, envelope, name):
    """The coordinator adopts a worker spool into the normal backup closure."""
    exchange = envelope.get("exchange") or {}
    raw = exchange.pop("_raw_stream", None)
    if raw is not None:
        try:
            raw.seek(0)
            ref = ctx.space.publish_run_stream(ctx.execution.execution_id, name, raw)
            exchange["raw_response_ref"] = ref.model_dump(mode="json")
            append_run_revision(ctx.space, ctx.execution, artifacts=[ref])
        finally:
            raw.close()


def _execute(ctx, service: CallService):
    request = CallRequest.model_validate_json(
        ctx.execution.input_snapshot.options["llm_request"]
    )
    prepared = json.loads(ctx.execution.input_snapshot.options["llm_prepared"])
    digest = sha256_of(prepared)
    if ctx.reuse_step(STEP, digest):
        return {"completed": True}
    envelope = execute_model_call(ctx, service, request, prepared)
    if envelope.get("error"):
        _save(ctx, _attempts(ctx), step_state="failed")
        raise CallFailure(envelope["error"])
    ctx.commit_step(step_id=STEP, input_digest=digest, records=[], result_refs=[])
    return {"completed": True}


FORMAT_ERRORS = frozenset(
    {"missing_or_multiple_tool_calls", "wrong_tool", "invalid_tool_arguments"}
)


def execute_model_call(ctx, service, request, prepared, *, step_id=STEP, call_key=None):
    """Execute one durable logical call inside the owning business flow.

    Each correction has a separate fixed call key. All physical attempts stay in
    the owning step, use account quota, and retain the existing unknown-completion
    boundary. The caller validates science and commits the business step.
    Known output-format failures are returned for bounded caller correction.
    """
    if service.dispatcher is not None:
        return _execute_concurrent(ctx, service, request, prepared, step_id, call_key)
    attempts = _attempts(ctx, step_id)
    prefix = f"{call_key}-" if call_key else ""
    indices = [
        i
        for i, a in enumerate(attempts)
        if a.request is not None
        and Path(a.request.path).name.startswith(prefix + "attempt-")
    ]
    for i in indices:
        if json.loads(ctx.space.read_file(attempts[i].request)) != prepared:
            raise CallFailure("archived call key is bound to different input")
    index = indices[-1] if indices else None
    # A response committed before a crash is enough to finish; an unconfirmed
    # outbound call never authorizes another send during normal recovery.
    if index is not None and attempts[index].state == "pending":
        if attempts[index].response is None:
            _save(
                ctx,
                attempts,
                step_state="failed",
                error_kind="completion_unknown",
                step_id=step_id,
            )
            raise CallFailure(
                "completion_unknown; create a new request only after reviewing the uncertain call"
            )
        envelope = json.loads(ctx.space.read_file(attempts[index].response))
        _finalize(ctx, attempts, envelope, index=index, step_id=step_id)
    if index is not None:
        envelope = (
            json.loads(ctx.space.read_file(attempts[index].response))
            if attempts[index].response
            else None
        )
        if (
            attempts[index].state == "succeeded"
            or envelope
            and envelope.get("error") in FORMAT_ERRORS
        ):
            return envelope
        # Resume may complete archival, but does not restart an exhausted or
        # explicitly failed logical call (including format correction).
        raise CallFailure(
            "previous model call failed; inspect it and create a new request"
        )
    digest = sha256_of(prepared)
    credential = service.credential()
    if credential.get_secret_value() in canonical_json(prepared):
        raise CallFailure("credential values must not occur in model inputs")
    quota = AccountQuota(service.runtime)
    for index in range(service.runtime.max_attempts):
        service.hook("before_send")
        try:
            lease = quota.acquire()
        except TimeoutError:
            _save(
                ctx,
                attempts,
                step_state="failed",
                error_kind="quota_wait_expired",
                step_id=step_id,
            )
            raise CallFailure(
                "quota_wait_expired; no physical request was sent"
            ) from None
        envelope = None
        try:
            number = index + 1
            request_ref = ctx.space.publish_run_file(
                ctx.execution.execution_id,
                f"requests/{prefix}attempt-{number:03d}.json",
                canonical_json(prepared).encode(),
            )
            attempts.append(
                Attempt(
                    request=request_ref,
                    response=None,
                    state="pending",
                    started_at=datetime.now(UTC),
                    finished_at=None,
                    usage=None,
                    service_request_id=None,
                )
            )
            _save(ctx, attempts, step_id=step_id)
            service.hook("after_pending")
            try:
                exchange = service.adapter.send(
                    prepared["payload"], service.runtime, credential
                )
                service.hook("after_response")
                try:
                    result = service.adapter.parse(exchange, request)
                    envelope = {
                        "exchange": exchange,
                        "result": result,
                        "error": None,
                        "uncertain": False,
                        "retryable": False,
                    }
                except (
                    OutputFailure,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as exc:
                    kind = (
                        str(exc)
                        if isinstance(exc, OutputFailure)
                        else "invalid_response"
                    )
                    envelope = {
                        "exchange": exchange,
                        "result": None,
                        "error": kind,
                        "uncertain": False,
                        "retryable": False,
                    }
            except TransportFailure as exc:
                envelope = {
                    "exchange": exc.exchange,
                    "result": None,
                    "error": exc.kind,
                    "uncertain": exc.uncertain,
                    "retryable": exc.retryable,
                    "retry_after": exc.retry_after,
                }
            envelope["request_sha256"] = digest
            _archive_raw(ctx, envelope, f"responses/{prefix}attempt-{number:03d}.raw")
            response_ref = ctx.space.publish_run_file(
                ctx.execution.execution_id,
                f"responses/{prefix}attempt-{number:03d}.json",
                canonical_json(envelope).encode(),
            )
            exchange = envelope["exchange"]
            usage = token_usage(
                exchange.get("body") if isinstance(exchange.get("body"), dict) else {}
            )
            result = envelope.get("result") or {}
            usage = result.get("usage", usage)
            attempts[-1] = attempts[-1].model_copy(
                update={
                    "response": response_ref,
                    "usage": Usage(tokens=usage["tokens"]),
                    "service_request_id": result.get("service_request_id")
                    or exchange.get("headers", {}).get("x-request-id"),
                }
            )
            _save(ctx, attempts, step_id=step_id)
            service.hook("after_archive")
            _finalize(ctx, attempts, envelope, step_id=step_id)
            if network_retry_allowed(envelope, service.runtime):
                delay = envelope.get("retry_after")
                quota.cooldown(delay if delay is not None else min(2**index, 30))
        finally:
            quota.release(lease)
        if not envelope["error"] or envelope["error"] in FORMAT_ERRORS:
            return envelope
        if (
            not network_retry_allowed(envelope, service.runtime)
            or number == service.runtime.max_attempts
        ):
            _save(ctx, attempts, step_state="failed", step_id=step_id)
            raise CallFailure(
                "completion_unknown" if envelope["uncertain"] else envelope["error"]
            )
        # AccountQuota performs the bounded wait for the next physical request.
    raise AssertionError("unreachable")


def _execute_concurrent(ctx, service, request, prepared, step_id, call_key):
    """Persist before dispatch; reentry consumes only this coordinator's calls."""
    dispatcher = service.dispatcher
    key = (str(ctx.space.root), ctx.execution.execution_id, step_id, call_key)
    prefix = f"{call_key}-" if call_key else ""
    attempts = _attempts(ctx, step_id)
    indices = [
        i
        for i, a in enumerate(attempts)
        if a.request is not None
        and Path(a.request.path).name.startswith(prefix + "attempt-")
    ]
    for i in indices:
        if json.loads(ctx.space.read_file(attempts[i].request)) != prepared:
            raise CallFailure("archived call key is bound to different input")
    if indices:
        index = indices[-1]
        attempt = attempts[index]
        if attempt.response is None:
            if key in dispatcher.jobs:
                raise WaitingForCall()
            _save(
                ctx,
                attempts,
                step_state="failed",
                error_kind="completion_unknown",
                step_id=step_id,
            )
            raise CallFailure(
                "completion_unknown; no live coordinator owns the pending call"
            )
        envelope = json.loads(ctx.space.read_file(attempt.response))
        if attempt.state == "pending":
            service.hook("after_archive")
            _finalize(ctx, attempts, envelope, index=index, step_id=step_id)
        if not envelope.get("error") or envelope.get("error") in FORMAT_ERRORS:
            return envelope
        if (
            not network_retry_allowed(envelope, service.runtime)
            or len(indices) >= service.runtime.max_attempts
            or key not in dispatcher.completed
        ):
            raise CallFailure(
                "completion_unknown" if envelope.get("uncertain") else envelope["error"]
            )
    if dispatcher.stop_reason is not None:
        raise CallFailure(dispatcher.stop_reason)
    credential = service.credential()
    if credential.get_secret_value() in canonical_json(prepared):
        raise CallFailure("credential values must not occur in model inputs")
    service.hook("before_send")
    request_ref = ctx.space.publish_run_file(
        ctx.execution.execution_id,
        f"requests/{prefix}attempt-{len(indices) + 1:03d}.json",
        canonical_json(prepared).encode(),
    )
    attempts.append(
        Attempt(
            request=request_ref,
            response=None,
            state="pending",
            started_at=datetime.now(UTC),
            finished_at=None,
            usage=None,
            service_request_id=None,
        )
    )
    _save(ctx, attempts, step_id=step_id)
    service.hook("after_pending")
    digest = sha256_of(prepared)

    def archive(envelope):
        with ctx.space.lock():
            envelope["request_sha256"] = digest
            _archive_raw(
                ctx, envelope, f"responses/{prefix}attempt-{len(indices) + 1:03d}.raw"
            )
            response_ref = ctx.space.publish_run_file(
                ctx.execution.execution_id,
                f"responses/{prefix}attempt-{len(indices) + 1:03d}.json",
                canonical_json(envelope).encode(),
            )
            current = _attempts(ctx, step_id)
            index = next(i for i, a in enumerate(current) if a.request == request_ref)
            exchange = envelope.get("exchange") or {}
            usage = token_usage(
                exchange.get("body") if isinstance(exchange.get("body"), dict) else {}
            )
            result = envelope.get("result") or {}
            usage = result.get("usage", usage)
            current[index] = current[index].model_copy(
                update={
                    "response": response_ref,
                    "usage": Usage(tokens=usage["tokens"]),
                    "service_request_id": result.get("service_request_id")
                    or exchange.get("headers", {}).get("x-request-id"),
                }
            )
            _save(ctx, current, step_id=step_id)

    dispatcher.submit(key, service, request, prepared, archive)
    raise WaitingForCall()


class ModelCallFlow(DefaultFlowAdapter):
    flow_id = "llm-model-call"
    flow_version = "1"
    mode = "synthetic"

    def initial_state(self, ctx):
        return {"completed": False}

    def build_graph(self, ctx):
        graph = StateGraph(dict)
        graph.add_node(STEP, lambda state: _execute(ctx, ctx.service))
        graph.add_edge(START, STEP)
        graph.add_edge(STEP, END)
        return graph.compile(checkpointer=ctx.checkpointer)


def start_call(
    space: Workspace,
    request: CallRequest,
    runtime: RuntimeConfig,
    *,
    request_key: str,
    credential: SecretStr,
    adapter: ModelAdapter | None = None,
):
    adapter = adapter or TokenDanceAdapter()
    request = CallRequest.model_validate(request.model_dump())
    runtime = RuntimeConfig.model_validate(runtime.model_dump())
    if request.model.adapter != adapter.name:
        raise ValueError(
            "model adapter must be supplied as a matching trusted program object"
        )
    adapter.validate_runtime(runtime)
    prepared = adapter.prepare(request)
    if credential.get_secret_value() in canonical_json(
        {
            "request": request.model_dump(mode="json"),
            "prepared": prepared,
            "runtime": runtime.model_dump(mode="json"),
        }
    ):
        raise ValueError("credential values must not occur in model inputs")
    snapshot = build_input_snapshot(
        source_refs=[],
        method=None,
        expert_adjudication=False,
        models=[
            ModelSpec(
                step_id=STEP,
                provider=request.model.provider,
                model=request.model.model,
                parameters=request.model.parameters or {},
            )
        ],
        options={
            "llm_request": request.model_dump_json(),
            "llm_prepared": canonical_json(prepared),
            "llm_runtime": runtime.model_dump_json(),
        },
    )
    handle, _ = create_execution(
        space, request_key=request_key, input_snapshot=snapshot, flow=ModelCallFlow()
    )
    return handle


def resume_call(
    space: Workspace,
    execution_id: str,
    *,
    credential: SecretStr | None = None,
    env_file: Path | None = None,
    adapter: ModelAdapter | None = None,
    hooks: dict | None = None,
):
    handle = get_execution(space, execution_id)
    if handle.flow_id != ModelCallFlow.flow_id:
        raise ValueError("execution is not a model call")
    runtime = RuntimeConfig.model_validate_json(
        handle.input_snapshot.options["llm_runtime"]
    )
    request = CallRequest.model_validate_json(
        handle.input_snapshot.options["llm_request"]
    )
    adapter = adapter or TokenDanceAdapter()
    if request.model.adapter != adapter.name:
        raise ValueError("resume requires the original trusted adapter")
    adapter.validate_runtime(runtime)
    resume_execution(
        space,
        execution_id,
        ModelCallFlow(),
        service=CallService(
            runtime,
            credential=credential,
            env_file=env_file,
            adapter=adapter,
            hooks=hooks,
        ),
    )
    return get_call(space, execution_id)


def get_call(space: Workspace, execution_id: str) -> dict:
    handle = get_execution(space, execution_id)
    if handle.flow_id != ModelCallFlow.flow_id:
        raise ValueError("execution is not a model call")
    run = space.store().read_record_current(handle.run_ref.record_id)
    attempts = [a for step in run.data.steps for a in step.attempts]
    response = None
    if attempts and attempts[-1].response:
        response = json.loads(space.read_file(attempts[-1].response))
    return {
        "execution_id": execution_id,
        "state": run.data.state,
        "attempt_count": len(attempts),
        "usage": run.data.usage.model_dump(mode="json"),
        "errors": [e.model_dump() for e in run.data.errors],
        "attempts": [a.model_dump(mode="json") for a in attempts],
        "response": response,
        "config": {
            "request": json.loads(handle.input_snapshot.options["llm_request"]),
            "runtime": json.loads(handle.input_snapshot.options["llm_runtime"]),
        },
    }
