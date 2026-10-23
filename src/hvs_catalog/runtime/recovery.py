"""Execution recovery validates fixed inputs, pending decisions, checkpoints, and flow
continuation.

Review saves decisions; recovery applies and advances. Validate the complete current
checkpoint before application. Known inconsistencies preserve decisions/diagnostics but
create no scientific revisions/receipts. Allow legitimate
committed-business/lagging-framework stages. Recheck applicability and reuse existing
application receipts without repeated revisions. Completion requires both valid business
output and terminal framework nodes, never merely a returned call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..records.common import PatchApplicationError, RecordRef
from ..records.questions import DecisionRecord, QuestionRecord
from ..records.runs import RunError, RunRecord, StepRecord
from ..review import ReviewService
from ..review.service import DECISION_APPLY_KIND
from ..workspace import (
    CommitRequest,
    RecoveryInconsistentError,
    WorkspaceIntegrityError,
)
from ..workspace.backup import (
    checkpoint_state_problems,
    framework_final_key,
    walk_state_refs,
)
from .deferred import WaitingForCall
from .execution import (
    DefaultFlowAdapter,
    ExecutionHandle,
    RuntimeContext,
    StepRunInfo,
    append_run_revision,
    get_execution,
    verify_execution_inputs,
    verify_flow_compatibility,
)

if TYPE_CHECKING:
    from ..workspace import Workspace


@dataclass
class AppliedDecisionInfo:
    decision_ref: RecordRef
    question_ref: RecordRef
    result_refs: tuple[RecordRef, ...]
    blocked_reason: str | None = None


@dataclass
class ExecutionReport:
    execution_id: str
    outcome: str  # paused | finished | failed
    mode: str  # fresh | resumed | already-finished
    pending_question: Any | None = None
    applied: list[AppliedDecisionInfo] = field(default_factory=list)
    steps: list[StepRunInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    service_summary: dict | None = None


def resume_execution(
    space: Workspace,
    execution_id: str,
    flow: DefaultFlowAdapter,
    *,
    service: Any | None = None,
) -> ExecutionReport:
    """Advance execution by applying pending decisions and continuing matching checkpoints.

    Hold locks for framework writes and release on pause/completion. Mismatched frozen
    input/flow/code blocks recovery, never substituting current versions.
    """
    lock = space.lock()
    lock.acquire()
    try:
        return _resume_locked(space, execution_id, flow, service)
    finally:
        lock.release()


def _resume_locked(
    space: Workspace, execution_id: str, flow: DefaultFlowAdapter, service: Any | None
) -> ExecutionReport:
    execution = get_execution(space, execution_id)
    report = ExecutionReport(
        execution_id=execution_id, outcome="paused", mode="resumed"
    )

    from ..workspace.retention import PREFIX, retained_result

    if space.store().get_meta(PREFIX + execution_id) is not None:
        problems = verify_execution_inputs(space, execution)
        if problems:
            raise RecoveryInconsistentError(
                "已结束执行的固定输入损坏：" + "；".join(problems)
            )
    closed = retained_result(space, execution_id)
    if closed is not None:
        report.outcome = (
            "finished" if closed["status"]["state"] == "finished" else "failed"
        )
        report.mode = "already-finished"
        report.errors = list(closed["status"]["errors"])
        return report

    compatibility = verify_flow_compatibility(space, execution, flow)
    if compatibility:
        raise RecoveryInconsistentError(
            "当前代码、流程或依赖与执行固定版本不符，恢复受阻："
            + "；".join(compatibility)
        )
    input_problems = verify_execution_inputs(space, execution)
    if input_problems:
        raise RecoveryInconsistentError(
            "执行固定输入损坏或缺失，恢复受阻（不能用当前新版替代原输入）："
            + "；".join(input_problems)
        )

    review = ReviewService(space)
    run = _current_run(space, execution)
    # Read/validate checkpoints before recovery actions that can generate business results;
    # known inconsistencies prohibit applying decisions or creating revisions first.
    conn = space.checkpoint_connection()
    try:
        ctx = RuntimeContext(
            space=space, execution=execution, flow=flow, service=service
        )
        saver = SqliteSaver(conn)
        ctx.checkpointer = saver
        graph = flow.build_graph(ctx)
        config = {"configurable": {"thread_id": execution_id}}
        snapshot = graph.get_state(config)
        # Verify execution history/state through one exact closure; completion cannot bypass corruption.
        record_problems = _execution_record_problems(
            space, execution_id, snapshot.values
        )
        if record_problems:
            raise RecoveryInconsistentError(
                "执行业务结果不完整或记录引用的归档文件损坏或缺失，停止该执行："
                + "；".join(record_problems)
            )
        if snapshot.created_at is not None:
            problems = flow.verify_state(ctx, dict(snapshot.values))
            problems.extend(_runtime_state_problems(space, execution, snapshot.values))
            if problems:
                # Failure marking is best-effort, but corrupted collections still require inconsistency reporting.
                try:
                    append_run_revision(
                        space,
                        execution,
                        state="failed",
                        errors=[
                            RunError(
                                error_id=f"err-inconsistent-{execution_id}-{uuid4().hex}",
                                kind="recovery_inconsistent",
                                message="; ".join(problems),
                            )
                        ],
                    )
                except WorkspaceIntegrityError as exc:
                    problems.append(f"标记失败时记录集合已损坏：{exc}")
                raise RecoveryInconsistentError(
                    "检查点与业务记录不一致，停止该执行：" + "；".join(problems)
                )

        if (
            getattr(flow, "replay_terminal", False)
            and run.data.state == "running"
            and snapshot.created_at is not None
            and not snapshot.next
        ):
            saver.delete_thread(execution_id)
            snapshot = graph.get_state(config)

        if run.data.state == "finished":
            # Completed runs still verify terminal state/original credentials; never fabricate missing credentials.
            problems = _verify_business_results(space, execution)
            if snapshot.created_at is None or snapshot.next != ():
                problems.append("运行清单标记完成，但框架仍有待执行节点，未到达结束")
            problems.extend(checkpoint_state_problems(space, execution_id))
            if problems:
                raise RecoveryInconsistentError(
                    "执行已标记完成但业务结果不完整：" + "；".join(problems)
                )
            report.outcome = "finished"
            report.mode = "already-finished"
            report.service_summary = _service_summary(service)
            return report

        if (
            run.data.state == "failed"
            and getattr(flow, "terminal_failures", False)
            and run.data.errors
            and run.data.errors[-1].message.startswith(
                ("CallFailure:", "ValidationError:", "SubmissionError:")
            )
        ):
            # All frozen inputs, archives and checkpoint references were checked above.
            # A failed model extraction needs a new request, not another error revision.
            report.outcome = "failed"
            report.mode = "already-failed"
            report.errors = [error.message for error in run.data.errors]
            report.service_summary = _service_summary(service)
            return report

        try:
            report.applied = _apply_pending_decisions(space, ctx, review)
        except RecoveryInconsistentError:
            raise
        except Exception as exc:  # noqa: BLE001 - all application failures become failure reports
            message = f"{type(exc).__name__}: {exc}"
            append_run_revision(
                space,
                execution,
                state="failed",
                errors=[
                    RunError(
                        error_id=f"err-apply-{execution_id}-{uuid4().hex}",
                        kind="apply_failure",
                        message=message,
                    )
                ],
            )
            report.outcome = "failed"
            report.errors.append(message)
            report.steps = ctx.journal
            report.service_summary = _service_summary(service)
            return report

        mode = "resumed"
        if snapshot.created_at is None:
            mode = "fresh"
            # Commit recovery checkpoints before external calls so hard exits retain asynchronous progress.
            graph.invoke(flow.initial_state(ctx), config, durability="sync")
        elif _has_interrupt(snapshot):
            payload = _interrupt_payload(snapshot)
            question = _question_from_payload(review, payload)
            applied_now = (
                question is not None
                and question.application is not None
                and question.application.phase == "applied"
            )
            if applied_now:
                graph.invoke(
                    Command(
                        resume={
                            "question_ref": question.question_ref.model_dump(
                                mode="json"
                            ),
                            "resume_kind": "decision_applied",
                        }
                    ),
                    config,
                    durability="sync",
                )
            else:
                # Unanswered, answered-but-unresolved, or blocked applications keep affected steps waiting.
                _mark_waiting(space, execution, flow, payload, None)
                report.pending_question = question
                report.outcome = "paused"
                report.mode = mode
                report.steps = ctx.journal
                report.service_summary = _service_summary(service)
                return report
        else:
            graph.invoke(None, config, durability="sync")
        end = graph.get_state(config)
    except WaitingForCall:
        report.outcome = "running"
        report.steps = ctx.journal
        report.service_summary = _service_summary(service)
        return report
    except RecoveryInconsistentError:
        raise
    except Exception as exc:  # noqa: BLE001 - all node failures become failure reports
        message = f"{type(exc).__name__}: {exc}"
        append_run_revision(
            space,
            execution,
            state="failed",
            errors=[
                RunError(
                    error_id=f"err-flow-{execution_id}-{uuid4().hex}",
                    kind="flow_failure",
                    message=message,
                )
            ],
        )
        report.outcome = "failed"
        report.errors.append(message)
        report.steps = ctx.journal
        report.service_summary = _service_summary(service)
        end = None
        return report
    finally:
        conn.close()

    assert end is not None
    if _has_interrupt(end):
        payload = _interrupt_payload(end)
        question = _question_from_payload(review, payload)
        _mark_waiting(space, execution, flow, payload, None)
        report.pending_question = question
        report.outcome = "paused"
        report.mode = mode
        report.steps = ctx.journal
        report.service_summary = _service_summary(service)
        return report

    problems = _verify_business_results(space, execution)
    if end.next != () or problems:
        append_run_revision(
            space,
            execution,
            state="failed",
            errors=[
                RunError(
                    error_id=f"err-incomplete-{execution_id}-{uuid4().hex}",
                    kind="incomplete",
                    message="; ".join(problems) or "框架未到达结束节点",
                )
            ],
        )
        report.outcome = "failed"
        report.errors.extend(problems or ["框架未到达结束节点"])
        report.steps = ctx.journal
        report.service_summary = _service_summary(service)
        return report

    # Matching flows prove business/framework completion; save final IDs and completion revisions atomically.
    final_id = final_checkpoint_id(end, execution_id)
    append_run_revision(
        space,
        execution,
        state="finished",
        meta_updates={framework_final_key(execution_id): final_id},
    )
    report.outcome = "finished"
    report.mode = mode
    report.steps = ctx.journal
    report.service_summary = _service_summary(service)
    return report


def final_checkpoint_id(snapshot: Any, execution_id: str) -> str:
    """Read snapshot checkpoint IDs and compare them with latest checkpoints as completion
    credentials.
    """
    checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise RecoveryInconsistentError(
            f"执行 {execution_id} 完成时无法确定最终检查点，完成凭据缺失"
        )
    return checkpoint_id


def _current_run(space: Workspace, execution: ExecutionHandle) -> RunRecord:
    run = space.store().read_record_current(execution.run_ref.record_id)
    if not isinstance(run, RunRecord):
        raise WorkspaceIntegrityError(
            f"执行 {execution.execution_id} 的运行清单缺失或损坏"
        )
    return run


def _apply_pending_decisions(
    space: Workspace, ctx: RuntimeContext, review: ReviewService
) -> list[AppliedDecisionInfo]:
    """Application rechecks applicability, builds controlled results, and atomically
    commits revisions/receipts.
    """
    applied: list[AppliedDecisionInfo] = []
    snapshot = ctx.execution.input_snapshot
    allowed_existing = {
        (ref.record_id, ref.revision) for ref in snapshot.existing_decision_refs
    }
    for row in space.store().list_questions(execution_id=ctx.execution.execution_id):
        latest = space.store().read_record_current(row.question_id)
        if not isinstance(latest, QuestionRecord) or latest.data.status != "answered":
            continue
        for decision_ref in latest.data.decision_refs:
            record = space.store().read_record(decision_ref)
            if not isinstance(record, DecisionRecord):
                continue
            state = review.find_application(record.ref())
            if state is not None:
                applied.append(
                    AppliedDecisionInfo(
                        decision_ref=record.ref(),
                        question_ref=latest.ref(),
                        result_refs=state.result_refs,
                        blocked_reason=state.error
                        if state.phase == "blocked"
                        else None,
                    )
                )
                continue
            if record.data.outcome == "unresolved":
                continue  # Unresolved answers apply no patches and never automatically exclude objects.
            decision_binding = (record.record_id, record.revision)
            if (
                record.execution_id != ctx.execution.execution_id
                and decision_binding not in allowed_existing
            ):
                applied.append(
                    AppliedDecisionInfo(
                        decision_ref=record.ref(),
                        question_ref=latest.ref(),
                        result_refs=(),
                        blocked_reason=("决定不属于本执行，也不在运行固定的允许集合内"),
                    )
                )
                continue
            blocked = _apply_one_decision(space, ctx, review, record, latest, snapshot)
            applied.append(blocked)
    return applied


def _apply_one_decision(
    space: Workspace,
    ctx: RuntimeContext,
    review: ReviewService,
    decision: DecisionRecord,
    question: QuestionRecord,
    snapshot: Any,
) -> AppliedDecisionInfo:
    """Apply one decision; blocked applications save blocked receipts with
    decisions/diagnostics retained.
    """
    flow = ctx.flow
    run = _current_run(space, ctx.execution)

    def block(reason: str) -> AppliedDecisionInfo:
        store = space.store()
        store.commit(
            CommitRequest(
                operation_key=f"apply:{decision.record_id}:{decision.revision}",
                kind=DECISION_APPLY_KIND,
                new_records=[],
                expected_revisions={},
                execution_id=ctx.execution.execution_id,
                links={
                    "decision_ref": decision.ref().model_dump(mode="json"),
                    "question_ref": question.ref().model_dump(mode="json"),
                    "phase": "blocked",
                    "error": reason,
                },
            )
        )
        return AppliedDecisionInfo(
            decision_ref=decision.ref(),
            question_ref=question.ref(),
            result_refs=(),
            blocked_reason=reason,
        )

    if decision.data.question_ref.record_id != question.record_id:
        return block("决定指向的问题与当前问题不一致")
    if decision.data.question_fingerprint != question.data.fingerprint:
        return block("决定依据的问题指纹与当前问题不一致")
    patch = decision.data.result_patch
    if patch is not None:
        target_current = space.store().current_revision(patch.target.record_id)
        if target_current != patch.target.revision:
            return block(
                f"决定目标 {patch.target.record_id} 的当前修订为 v{target_current}，"
                f"决定基于 v{patch.target.revision}；旧补丁不能移植到新目标"
            )
    try:
        plan = flow.apply_decision(ctx, decision, question)
    except PatchApplicationError as exc:
        return block("; ".join(exc.reasons))
    if plan is None:
        return block(
            "该步骤无法据此决定构造结果；已保存决定并报告应用受阻，"
            "不从答复自由文本推断科学修改"
        )
    new_run = run.model_copy(
        update={
            "revision": run.revision + 1,
            "created_at": ctx.now(),
            "data": run.data.model_copy(
                update={
                    "state": "running",
                    "result_refs": [
                        *run.data.result_refs,
                        *[
                            ref
                            for ref in plan.result_refs
                            if ref not in run.data.result_refs
                        ],
                    ],
                }
            ),
        }
    )
    receipt = space.store().commit(
        CommitRequest(
            operation_key=f"apply:{decision.record_id}:{decision.revision}",
            kind=DECISION_APPLY_KIND,
            new_records=[*plan.records, new_run],
            expected_revisions=plan.expected_revisions,
            result_refs=list(plan.result_refs),
            execution_id=ctx.execution.execution_id,
            links={
                "decision_ref": decision.ref().model_dump(mode="json"),
                "question_ref": question.ref().model_dump(mode="json"),
                "phase": "applied",
            },
        )
    )
    flow.hook("after_apply")
    return AppliedDecisionInfo(
        decision_ref=decision.ref(),
        question_ref=question.ref(),
        result_refs=tuple(plan.result_refs) or receipt.result_refs,
    )


def _mark_waiting(
    space: Workspace,
    execution: ExecutionHandle,
    flow: DefaultFlowAdapter,
    payload: dict,
    note: str | None,
) -> None:
    step_id = flow.waiting_step(payload) or str(
        payload.get("step_id") or "await_decision"
    )
    run = _current_run(space, execution)
    steps = {item.step_id: item for item in run.data.steps}
    previous = steps.get(step_id)
    steps[step_id] = StepRecord(
        step_id=step_id,
        state="waiting_review",
        input_refs=[],
        # Review waits retain archived request/response attempts.
        attempts=list(previous.attempts) if previous is not None else [],
        result_refs=list(previous.result_refs) if previous else [],
        error_refs=[],
    )
    append_run_revision(
        space,
        execution,
        state="waiting_review",
        step=steps[step_id],
    )


def _has_interrupt(snapshot: Any) -> bool:
    return any(intr.value for task in snapshot.tasks for intr in task.interrupts)


def _interrupt_payload(snapshot: Any) -> dict:
    for task in snapshot.tasks:
        for intr in task.interrupts:
            if intr.value:
                return dict(intr.value)
    return {}


def _question_from_payload(review: ReviewService, payload: dict) -> Any | None:
    ref = payload.get("question_ref")
    if not isinstance(ref, dict) or "record_id" not in ref:
        return None
    try:
        return review.get_question(RecordRef.model_validate(ref))
    except WorkspaceIntegrityError:
        return None


def _runtime_state_problems(
    space: Workspace, execution: ExecutionHandle, state: dict
) -> list[str]:
    """General check: state record references must exist in this workspace's business
    database.
    """
    problems: list[str] = []
    for value in walk_state_refs(state):
        record = space.store().read_record(value)
        if record is None:
            problems.append(
                f"检查点引用的业务结果缺失：{value.record_id} v{value.revision}"
            )
        elif (
            record.execution_id is not None
            and record.execution_id != execution.execution_id
            and execution.input_snapshot.options.get("roster_ref")
            != value.model_dump_json()
        ):
            problems.append(
                f"检查点引用了其他执行的记录：{value.record_id}"
                f"（属于 {record.execution_id}）"
            )
    return problems


def _execution_record_problems(
    space: Workspace, execution_id: str, state: dict
) -> list[str]:
    from ..records.validate import CollectionValidationError, validate_collection
    from ..workspace.store import record_closure, verify_artifact_refs

    seeds = space.store().list_records(execution_id=execution_id)
    for ref in walk_state_refs(state):
        record = space.store().read_record(ref)
        if record is not None:
            seeds.append(record)
    records = record_closure(seeds, space.store().read_record)
    problems = verify_artifact_refs(records, space.root)
    try:
        validate_collection(records, space.root)
    except CollectionValidationError as exc:
        problems.append(f"执行记录集合未通过校验：{exc}")
    return problems


def _verify_business_results(space: Workspace, execution: ExecutionHandle) -> list[str]:
    """Completion check: every step completed and every declared output can be read back."""
    problems: list[str] = []
    run = _current_run(space, execution)
    for step in run.data.steps:
        if step.state != "finished":
            problems.append(f"步骤 {step.step_id} 状态为 {step.state}，未完成")
        for ref in step.result_refs:
            if space.store().read_record(ref) is None:
                problems.append(
                    f"步骤 {step.step_id} 的结果记录缺失："
                    f"{ref.record_id} v{ref.revision}"
                )
    for ref in run.data.result_refs:
        if space.store().read_record(ref) is None:
            problems.append(f"运行输出引用缺失：{ref.record_id} v{ref.revision}")
    if not run.data.steps:
        problems.append("运行清单没有任何已完成步骤")
    return problems


def _service_summary(service: Any | None) -> dict | None:
    if service is None:
        return None
    summary = getattr(service, "summary", None)
    if callable(summary):
        return summary()
    return None
