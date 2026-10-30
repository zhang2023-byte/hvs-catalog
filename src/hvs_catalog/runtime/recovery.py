"""Execution recovery validates fixed inputs, checkpoints, and flow continuation.

Validate the complete current checkpoint before advancing. Known inconsistencies create
no scientific revisions/receipts. Allow legitimate committed-business/lagging-framework
stages. Completion requires both valid business output and terminal framework nodes,
never merely a returned call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from ..records.runs import RunError, RunRecord
from ..workspace import RecoveryInconsistentError, WorkspaceIntegrityError
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
class ExecutionReport:
    execution_id: str
    outcome: str  # running | finished | failed
    mode: str  # fresh | resumed | already-finished
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
    """Advance execution by continuing matching checkpoints.

    Hold locks for framework writes and release on completion. Mismatched frozen
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

    run = _current_run(space, execution)
    # Read/validate checkpoints before recovery actions that can generate business results;
    # known inconsistencies prohibit creating revisions first.
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

        mode = "resumed"
        if snapshot.created_at is None:
            mode = "fresh"
            # Commit recovery checkpoints before external calls so hard exits retain asynchronous progress.
            graph.invoke(flow.initial_state(ctx), config, durability="sync")
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
