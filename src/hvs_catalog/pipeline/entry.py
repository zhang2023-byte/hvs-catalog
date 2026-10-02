"""Shared extraction Python API for creation, advancement, and queries.

start_extraction creates/reuses runtime frozen-input/request-key executions.
resume_extraction advances new runs, paused recovery, or idempotent completed reentry.
get_extraction reads state/configuration/questions/model judgments/usage. Recovery uses
saved snapshots only, never new requests; configuration changes require new
keys/executions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..records.common import RecordRef
from ..records.runs import ModelSpec, RunRecord
from ..review import ReviewService
from ..review.service import QuestionView
from ..runtime import ExecutionHandle, StepRunInfo, get_execution
from ..runtime import (
    build_input_snapshot as _build_input_snapshot,
)
from ..runtime import (
    create_execution as _create_execution,
)
from ..runtime import (
    resume_execution as _resume_execution,
)
from ..workspace import Workspace, WorkspaceIntegrityError
from .params import ParameterPolicyError, effective_policy
from .registry import PipelineRegistry
from .request import ExtractionRequest

__all__ = [
    "ExtractionHandle",
    "ExtractionReport",
    "PipelineRequestError",
    "get_extraction",
    "list_methods",
    "pending_questions",
    "resume_extraction",
    "start_extraction",
    "supported_parameters",
]


class PipelineRequestError(ValueError):
    """Request violates entry contracts, including unknown methods/configuration/support
    scope.
    """


@dataclass(frozen=True)
class ExtractionHandle:
    """start_extraction result: execution identity and echoed frozen configuration."""

    execution_id: str
    request_key: str
    reused: bool
    method: str
    mode: str
    expert_adjudication: bool
    models: list[dict]
    input_digest: str
    flow_id: str
    flow_version: str

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "request_key": self.request_key,
            "reused": self.reused,
            "method": self.method,
            "mode": self.mode,
            "expert_adjudication": self.expert_adjudication,
            "models": list(self.models),
            "input_digest": self.input_digest,
            "flow_id": self.flow_id,
            "flow_version": self.flow_version,
        }


@dataclass
class ExtractionReport:
    """Advancement result: state, questions, steps, and original failures."""

    execution_id: str
    outcome: str  # paused | finished | failed
    mode: str  # fresh | resumed | already-finished
    state: str  # Run-manifest state: running, waiting_review, finished, failed, and others.
    config: dict
    steps: list[StepRunInfo] = field(default_factory=list)
    pending_question: dict | None = None
    open_questions: int = 0
    answered_questions: int = 0
    model_judgments: int = 0
    result_refs: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    service_summary: dict | None = None

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "outcome": self.outcome,
            "mode": self.mode,
            "state": self.state,
            "config": self.config,
            "steps": [{"step_id": s.step_id, "reused": s.reused} for s in self.steps],
            "pending_question": self.pending_question,
            "open_questions": self.open_questions,
            "answered_questions": self.answered_questions,
            "model_judgments": self.model_judgments,
            "result_refs": list(self.result_refs),
            "errors": list(self.errors),
            "service_summary": self.service_summary,
        }


def _validated_model_specs(registration, models, *, context: str) -> list[ModelSpec]:
    """Validate restricted parameters before execution and return snapshot content copies.

    ModelSpec copies make snapshots equal actual sent contents despite later caller
    mutation. effective_policy treats undeclared policies as empty consistently with
    flow validation.
    """
    declared = registration.spec.parameter_policy
    policy = effective_policy(declared)
    hint = (
        f"方法 {registration.spec.method_id!r} 未声明模型参数支持集"
        "（等同空策略，不接受任何参数）"
        if declared is None
        else f"方法 {registration.spec.method_id!r}"
    )
    validated: list[ModelSpec] = []
    for item in models:
        try:
            parameters = policy.validate(
                item.parameters,
                context=f"{context}：{hint}（模型 {item.model}）",
            )
        except ParameterPolicyError as exc:
            raise PipelineRequestError(
                f"{context}：模型参数未通过受限策略校验，请求被拒绝：{exc}"
            ) from exc
        validated.append(
            ModelSpec(
                step_id=item.step_id,
                provider=item.provider,
                model=item.model,
                parameters=parameters,
            )
        )
    return validated


def _check_stored_model_specs(registration, snapshot_models) -> None:
    """Recovery validates saved parameters through the same policy without rewriting
    snapshots.

    Reject unsupported nested legacy/bypass parameters rather than rewriting data to
    appear safe or sending unchecked content.
    """
    try:
        _validated_model_specs(
            registration, snapshot_models, context="执行固定的模型参数"
        )
    except PipelineRequestError as exc:
        raise PipelineRequestError(
            f"执行固定的模型参数未通过当前受限策略校验，恢复被拒绝：{exc}"
        ) from exc


def _default_registry(registry: PipelineRegistry | None) -> PipelineRegistry:
    if registry is not None:
        return registry
    from .methods import DEFAULT_REGISTRY

    return DEFAULT_REGISTRY


def _validate_models(method_spec, request: ExtractionRequest) -> None:
    for item in request.models:
        if item.step_id is None:
            continue
        if not method_spec.has_step(item.step_id):
            raise PipelineRequestError(
                f"方法 {method_spec.method_id!r} 没有步骤 {item.step_id!r}；"
                f"可用步骤：{', '.join(method_spec.step_ids())}。"
                "给不存在或未启用的步骤设置模型时，运行前报错"
            )


def start_extraction(
    space: Workspace,
    *,
    request: ExtractionRequest,
    request_key: str,
    registry: PipelineRegistry | None = None,
    configuration=None,
) -> ExtractionHandle:
    """Create/reuse extraction through strict requests.

    Freeze methods, modes, default/step models, and review switches into
    snapshots/digests. Changed same-key input rejects; new configuration requires new
    keys. Requests have no Gold/scoring/answer paths.
    """
    registration = _default_registry(registry).get(request.method)
    _validate_models(registration.spec, request)
    frozen = {}
    if configuration is None:
        validated_models = _validated_model_specs(
            registration, request.models, context="提取请求"
        )
    else:
        from .configuration import ExtractionConfig, freeze_configuration

        configuration = ExtractionConfig.model_validate(configuration.model_dump())
        if (
            configuration.request(request.source_refs).model_dump()
            != request.model_dump()
        ):
            raise PipelineRequestError("提取请求与完整运行配置不一致")
        validated_models = [
            ModelSpec.model_validate(item.model_dump()) for item in request.models
        ]
    problems = registration.check_request(space, request)
    if problems:
        raise PipelineRequestError("提取请求未通过运行前检查：" + "；".join(problems))
    if configuration is not None:
        with space.lock():
            frozen = freeze_configuration(space, configuration)
    snapshot = _build_input_snapshot(
        source_refs=list(request.source_refs),
        method=request.method,
        models=validated_models,
        expert_adjudication=request.expert_adjudication,
        existing_decision_refs=list(request.existing_decision_refs),
        **frozen,
    )
    flow = registration.build_flow(registration.spec, mode=request.mode)
    handle, reused = _create_execution(
        space,
        request_key=request_key,
        input_snapshot=snapshot,
        flow=flow,
    )
    return ExtractionHandle(
        execution_id=handle.execution_id,
        request_key=request_key,
        reused=reused,
        method=request.method,
        mode=request.mode,
        expert_adjudication=request.expert_adjudication,
        models=[item.model_dump(mode="json") for item in request.models],
        input_digest=handle.input_digest,
        flow_id=handle.flow_id,
        flow_version=handle.flow_version,
    )


def resume_extraction(
    space: Workspace,
    execution_id: str,
    *,
    service: Any,
    registry: PipelineRegistry | None = None,
) -> ExtractionReport:
    """Advance extraction with saved fixed configuration only.

    Share initial execution, paused recovery, and completed reentry; resolve frozen
    registered methods and accept no new request contents.
    """
    handle = get_execution(space, execution_id)
    registration = _default_registry(registry).get(handle.input_snapshot.method)
    from .configuration import validate_configuration_snapshot

    if validate_configuration_snapshot(handle.input_snapshot) is None:
        _check_stored_model_specs(registration, handle.input_snapshot.models)
    flow = registration.build_flow(registration.spec, mode=handle.mode)
    report = _resume_execution(space, execution_id, flow, service=service)
    return _build_report(space, handle, report)


def get_extraction(
    space: Workspace, execution_id: str, *, registry: PipelineRegistry | None = None
) -> dict:
    """Read-only execution state/configuration/step/question/model-judgment/usage queries."""
    handle = get_execution(space, execution_id)
    run = _current_run(space, handle)
    questions = ReviewService(space).list_questions(execution_id=execution_id)
    usages = [attempt.usage for step in run.data.steps for attempt in step.attempts]
    attempts_total = len(usages)
    known_tokens = [
        usage.tokens
        for usage in usages
        if usage is not None and usage.tokens is not None
    ]
    return {
        "execution_id": execution_id,
        "state": run.data.state,
        "config": _config_echo(handle),
        "steps": [
            {
                "step_id": step.step_id,
                "state": step.state,
                "attempts": len(step.attempts),
                "tokens": (
                    sum(a.usage.tokens for a in step.attempts)
                    if step.attempts
                    and all(
                        a.usage is not None and a.usage.tokens is not None
                        for a in step.attempts
                    )
                    else None
                ),
                "result_refs": [
                    ref.model_dump(mode="json") for ref in step.result_refs
                ],
            }
            for step in run.data.steps
        ],
        "questions": [
            {
                "question_ref": view.question_ref.model_dump(mode="json"),
                "status": view.status,
                "answered_outcome": view.answered_outcome,
                "needs_processing": view.needs_processing,
                "created_at": view.record.created_at.isoformat(),
                "decided_at": (
                    view.decisions[-1].data.decided_at.isoformat()
                    if view.decisions
                    else None
                ),
            }
            for view in questions
        ],
        "model_judgments": [
            {
                "step_id": judgment.step_id,
                "judgment": judgment.judgment,
                "created_at": judgment.created_at.isoformat(),
            }
            for judgment in run.data.model_judgments
        ],
        "result_refs": [ref.model_dump(mode="json") for ref in run.data.result_refs],
        "errors": [error.message for error in run.data.errors],
        "usage": {
            "attempts": attempts_total,
            # Unknown usage stays null, never fabricated zero.
            "tokens": sum(known_tokens)
            if len(known_tokens) == attempts_total
            else None,
        },
        "input_digest": handle.input_digest,
    }


def pending_questions(
    space: Workspace, execution_id: str | None = None
) -> list[QuestionView]:
    """List pending questions, equivalent to ReviewService.list_questions."""
    return ReviewService(space).list_questions(execution_id=execution_id)


def list_methods(registry: PipelineRegistry | None = None) -> list[dict]:
    """List registered methods, steps, and markers, currently synthetic-mock only."""
    return [
        {
            "method_id": registration.spec.method_id,
            "implementation": registration.spec.implementation,
            "real_extraction": registration.spec.real_extraction,
            "version": registration.spec.version,
            "steps": [
                {
                    "step_id": step.step_id,
                    "kind": step.kind,
                    "title": step.title,
                }
                for step in registration.spec.steps
            ],
        }
        for registration in _default_registry(registry).list_methods()
    ]


def supported_parameters(registry: PipelineRegistry | None = None) -> dict:
    """List declared restricted parameter fields/types/ranges per method.

    Undeclared methods are omitted and accept no parameters.
    """
    described: dict[str, list[dict]] = {}
    for registration in _default_registry(registry).list_methods():
        policy = registration.spec.parameter_policy
        if policy is not None:
            described[registration.spec.method_id] = policy.describe()
    return described


def _current_run(space: Workspace, handle: ExecutionHandle) -> RunRecord:
    run = space.store().read_record_current(handle.run_ref.record_id)
    if not isinstance(run, RunRecord):  # pragma: no cover - data should be consistent
        raise WorkspaceIntegrityError(f"执行 {handle.execution_id} 的运行清单缺失")
    return run


def _config_echo(handle: ExecutionHandle) -> dict:
    snapshot = handle.input_snapshot
    return {
        "method": snapshot.method,
        "mode": handle.mode,
        "expert_adjudication": snapshot.expert_adjudication,
        "models": [item.model_dump(mode="json") for item in snapshot.models],
        "rules_version": snapshot.rules.version,
        "configuration": (
            json.loads(snapshot.options["extraction_config"])
            if "extraction_config" in snapshot.options
            else None
        ),
        "prompt_refs": [ref.model_dump(mode="json") for ref in snapshot.prompt_refs],
        "example_refs": [ref.model_dump(mode="json") for ref in snapshot.example_refs],
        "existing_decision_refs": [
            ref.model_dump(mode="json") for ref in snapshot.existing_decision_refs
        ],
        "flow_id": handle.flow_id,
        "flow_version": handle.flow_version,
        "input_digest": handle.input_digest,
    }


def _build_report(
    space: Workspace, handle: ExecutionHandle, report: Any
) -> ExtractionReport:
    run = _current_run(space, handle)
    review = ReviewService(space)
    questions = review.list_questions(execution_id=handle.execution_id)
    pending = None
    if report.pending_question is not None:
        view: QuestionView = report.pending_question
        pending = {
            "question_ref": view.question_ref.model_dump(mode="json"),
            "status": view.status,
            "answered_outcome": view.answered_outcome,
            "needs_processing": view.needs_processing,
            "prompt": view.record.data.prompt,
        }
    return ExtractionReport(
        execution_id=handle.execution_id,
        outcome=report.outcome,
        mode=report.mode,
        state=run.data.state,
        config=_config_echo(handle),
        steps=list(report.steps),
        pending_question=pending,
        open_questions=sum(1 for q in questions if q.status == "open"),
        answered_questions=sum(1 for q in questions if q.status == "answered"),
        model_judgments=len(run.data.model_judgments),
        result_refs=[ref.model_dump(mode="json") for ref in run.data.result_refs],
        errors=list(report.errors),
        service_summary=report.service_summary,
    )


def execution_result_refs(space: Workspace, execution_id: str) -> list[RecordRef]:
    """Read all execution result references for stable scoring adaptation."""
    handle = get_execution(space, execution_id)
    run = _current_run(space, handle)
    return list(run.data.result_refs)
