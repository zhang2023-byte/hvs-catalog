"""执行恢复：固定输入核对、待应用决定、检查点一致性与继续流程。

裁决恢复分三个可识别阶段：保存决定（review 负责）、应用决定与继续
流程（本模块）。恢复顺序固定：先读取并完整核对当前执行的检查点，
再应用待应用决定——已知不一致时保留已保存的决定、记录诊断，但不产
生新的科学结果修订或应用回执；"业务已提交、框架落后"的合法阶段仍
允许继续。应用前重查适用性；已存在应用回执时复用对应结果，不再次
增加修订。完成状态须同时证明业务结果与框架到达结束节点，不能仅因
调用返回就宣布完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..records.common import RecordRef
from ..records.patches import PatchApplicationError
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
    checkpoint_row_digest,
    checkpoint_state_problems,
    framework_final_checkpoint,
    framework_final_credential,
    framework_final_digest,
    framework_final_digest_key,
    framework_final_key,
    verify_record_artifacts,
    walk_state_refs,
)
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
    """推进一个执行：应用待应用决定并从匹配检查点继续。

    持有空间锁期间完成框架写入；暂停或完成后释放。固定输入、流程
    与代码版本不符时报告恢复受阻，不读取当前新版替代原输入。
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
    # 记录引用的归档文件先行核验：损坏或缺失的响应/归档不能靠恢复或
    # "已完成"跳过检查
    artifact_problems = _record_artifact_problems(space)
    if artifact_problems:
        raise RecoveryInconsistentError(
            "记录引用的归档文件损坏或缺失，停止该执行：" + "；".join(artifact_problems)
        )
    # 先读取并核对检查点，再做任何可能产生业务结果的恢复动作：
    # 已知不一致时不得先应用决定或生成新修订
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
        if snapshot.created_at is not None:
            problems = flow.verify_state(ctx, dict(snapshot.values))
            problems.extend(_runtime_state_problems(space, execution, snapshot.values))
            if problems:
                # 标记失败尽力而为：记录集合本身损坏时也必须报告不一致
                try:
                    append_run_revision(
                        space,
                        execution,
                        state="failed",
                        errors=[
                            RunError(
                                error_id=f"err-inconsistent-{execution_id}",
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

        if run.data.state == "finished":
            # 完成须同时证明业务与框架完成：当前执行用调用方流程直接
            # 核对（无待执行节点）；通过后补记缺失的框架完成凭据
            problems = _verify_business_results(space, execution)
            if snapshot.created_at is None or snapshot.next != ():
                problems.append("运行清单标记完成，但框架仍有待执行节点，未到达结束")
            if not problems:
                self_heal_framework_final(space, execution_id, snapshot)
            problems.extend(_checkpoint_problems(space))
            if problems:
                raise RecoveryInconsistentError(
                    "执行已标记完成但业务结果不完整：" + "；".join(problems)
                )
            report.outcome = "finished"
            report.mode = "already-finished"
            report.service_summary = _service_summary(service)
            return report

        try:
            report.applied = _apply_pending_decisions(space, ctx, review)
        except RecoveryInconsistentError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任意应用故障都转为失败报告
            message = f"{type(exc).__name__}: {exc}"
            append_run_revision(
                space,
                execution,
                state="failed",
                errors=[
                    RunError(
                        error_id=f"err-apply-{execution_id}",
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
            # 外部调用前提交恢复检查点，避免硬退出丢失尚在后台写入的进度。
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
                # 待答复、已答复仍未决或应用受阻：受影响步骤保留等待
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
    except RecoveryInconsistentError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任意节点故障都转为失败报告
        message = f"{type(exc).__name__}: {exc}"
        append_run_revision(
            space,
            execution,
            state="failed",
            errors=[
                RunError(
                    error_id=f"err-flow-{execution_id}",
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
                    error_id=f"err-incomplete-{execution_id}",
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

    # 完成修订与框架完成凭据（最终检查点 id 及内容摘要）同一事务提交：
    # 凭据证明本执行是在调用方流程核验无待执行节点后才标记完成的，
    # 且摘要绑定核验时的检查点内容
    final_id = final_checkpoint_id(end, execution_id)
    append_run_revision(
        space,
        execution,
        state="finished",
        meta_updates=framework_final_credential(space, execution_id, final_id),
    )
    report.outcome = "finished"
    report.mode = mode
    report.steps = ctx.journal
    report.service_summary = _service_summary(service)
    return report


def final_checkpoint_id(snapshot: Any, execution_id: str) -> str:
    """读取快照对应的检查点 id；作为完成凭据与最新检查点比对。"""
    checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        raise RecoveryInconsistentError(
            f"执行 {execution_id} 完成时无法确定最终检查点，完成凭据缺失"
        )
    return checkpoint_id


def self_heal_framework_final(
    space: Workspace, execution_id: str, snapshot: Any
) -> None:
    """已完成执行的凭据缺失时，在调用方流程核验通过后成组补记。

    先按当前已核验快照构造完整候选凭据（最终检查点 id 及其内容摘
    要），逐项核对已存在的键：任一已有值与候选不符，立即拒绝并保
    持两个键原样——冲突证据不能靠补记覆盖；全部相符时才在一次事
    务中只补缺失的键。凭据齐全时的冲突仍由共用核对报告。
    """
    existing_id = framework_final_checkpoint(space, execution_id)
    existing_digest = framework_final_digest(space, execution_id)
    if existing_id is not None and existing_digest is not None:
        return
    candidate_id = final_checkpoint_id(snapshot, execution_id)
    conn = space.checkpoint_connection()
    try:
        candidate_digest = checkpoint_row_digest(conn, execution_id, candidate_id)
    finally:
        conn.close()
    if candidate_digest is None:
        raise RecoveryInconsistentError(
            f"执行 {execution_id} 的最终检查点 {candidate_id} 不存在，无法补记完成凭据"
        )
    if existing_id is not None and existing_id != candidate_id:
        raise RecoveryInconsistentError(
            f"执行 {execution_id} 已有的完成凭据检查点为 {existing_id}，"
            f"与当前核验状态（{candidate_id}）不符；已有凭据冲突不能补记"
        )
    if existing_digest is not None and existing_digest != candidate_digest:
        raise RecoveryInconsistentError(
            f"执行 {execution_id} 已有的完成凭据摘要与当前检查点内容"
            f"（{candidate_id}）不符；已有凭据冲突不能补记"
        )
    updates: dict[str, str] = {}
    if existing_id is None:
        updates[framework_final_key(execution_id)] = candidate_id
    if existing_digest is None:
        updates[framework_final_digest_key(execution_id)] = candidate_digest
    space.store().set_meta_many(updates)


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
    """应用阶段：重查适用性，受控组装新结果，一次事务提交修订与回执。"""
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
                continue  # 仍未决：不应用补丁，不自动排除对象
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
    """应用单个决定；受阻时写入 blocked 回执并保留决定与诊断。"""
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
        # 等待裁决不丢弃已归档的请求/响应尝试
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
    """通用检查：状态中的记录引用必须存在于本空间的业务库。"""
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
        ):
            problems.append(
                f"检查点引用了其他执行的记录：{value.record_id}"
                f"（属于 {record.execution_id}）"
            )
    return problems


def _record_artifact_problems(space: Workspace) -> list[str]:
    return verify_record_artifacts(space)


def _checkpoint_problems(space: Workspace) -> list[str]:
    return checkpoint_state_problems(space)


def _verify_business_results(space: Workspace, execution: ExecutionHandle) -> list[str]:
    """完成核对：步骤全部完成且声明的结果引用都能读回。"""
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
