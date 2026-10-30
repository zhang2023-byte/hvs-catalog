"""Bind executions, freeze inputs, reuse step results, and maintain runs.

Reuse keys derive from full snapshots/actual step inputs. Archive rules at creation but
never fabricate unsent request archives. Initial caches are execution-local; identical
configurations elsewhere do not automatically reuse intermediate results.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..records.common import ArtifactRef, RecordRef, sha256_of
from ..records.registry import AnyRecord
from ..records.rules import rules_bytes
from ..records.runs import (
    InputSnapshot,
    ModelJudgment,
    RulesRef,
    RunData,
    RunError,
    RunRecord,
    StepRecord,
    Usage,
)
from ..records.source import SourcePackageRecord
from ..workspace import (
    CommitRequest,
    OperationKeyConflictError,
    Receipt,
    WorkspaceIntegrityError,
    resolve_within,
)

if TYPE_CHECKING:
    from ..workspace import Workspace

PACKAGE_NAME = "hvs-catalog"
RULES_ARCHIVE_REL = "inputs/rules.toml"


class FlowAdapter(Protocol):
    """Trusted caller flow adapter, verified through synthetic implementations.

    Accept supplied program objects only; never dynamically import/execute record
    strings.
    """

    flow_id: str
    flow_version: str
    mode: str

    def initial_state(self, ctx: RuntimeContext) -> dict: ...

    def build_graph(self, ctx: RuntimeContext) -> Any: ...


class DefaultFlowAdapter:
    """Base adapter for synthetic flows to extend.

    verify_state reports checkpoint/business inconsistencies. hook injects synthetic
    failures outside ordinary user workflows.
    """

    flow_id: str = "unnamed-flow"
    flow_version: str = "1"
    mode: str = "synthetic"

    def initial_state(self, ctx: RuntimeContext) -> dict:  # pragma: no cover
        raise NotImplementedError

    def build_graph(self, ctx: RuntimeContext) -> Any:  # pragma: no cover
        raise NotImplementedError

    def verify_state(self, ctx: RuntimeContext, state: dict) -> list[str]:
        return []

    def hook(self, name: str) -> None:
        return None


@dataclass
class StepRunInfo:
    step_id: str
    reused: bool
    service_calls: int = 0


@dataclass(frozen=True)
class ExecutionHandle:
    execution_id: str
    request_key: str
    run_ref: RecordRef
    input_snapshot: InputSnapshot
    input_digest: str
    flow_id: str
    flow_version: str
    mode: str


@dataclass
class RuntimeContext:
    """Context shared by flow nodes/runtime; recovery holds workspace locks."""

    space: Workspace
    execution: ExecutionHandle
    flow: Any
    service: Any | None
    journal: list[StepRunInfo] = field(default_factory=list)
    checkpointer: Any | None = None

    @property
    def store(self):
        return self.space.store()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def reuse_step(self, step_id: str, input_digest: str):
        """Find saved execution step results and synchronize run step state on hits."""
        from .execution import find_step_result

        return find_step_result(self, step_id, input_digest)

    def commit_step(
        self,
        *,
        step_id: str,
        input_digest: str,
        records: list[AnyRecord],
        result_refs: list[RecordRef],
    ) -> Receipt:
        info = StepRunInfo(step_id=step_id, reused=False)
        self.journal.append(info)
        return commit_step_result(
            self.space,
            self.execution,
            step_id=step_id,
            input_digest=input_digest,
            records=records,
            result_refs=result_refs,
        )


def current_environment() -> dict[str, Any]:
    """Read installed code/dependency versions from sole package definitions."""
    code_version = metadata.version(PACKAGE_NAME)
    dependency_versions: dict[str, str] = {}
    for requirement in metadata.requires(PACKAGE_NAME) or []:
        name = requirement.split(";")[0].split(" ")[0].split("=")[0].split(">")[0]
        name = name.split("[")[0].strip()
        if name:
            try:
                dependency_versions[name] = metadata.version(name)
            except metadata.PackageNotFoundError:  # pragma: no cover
                dependency_versions[name] = "unknown"
    return {
        "code_version": code_version,
        "dependency_versions": dependency_versions,
    }


def build_input_snapshot(
    *,
    source_refs: list[RecordRef],
    method: str | None = None,
    models: list | None = None,
    expert_adjudication: bool = True,
    prompt_refs: list | None = None,
    example_refs: list | None = None,
    precedent_refs: list | None = None,
    rules: RulesRef | None = None,
    options: dict[str, str] | None = None,
) -> InputSnapshot:
    """Assemble current-environment snapshots, defaulting rules to the current source."""
    from ..records.rules import load_rules

    environment = current_environment()
    loaded = load_rules()
    return InputSnapshot(
        source_refs=list(source_refs),
        rules=rules or RulesRef(version=loaded.version, sha256=loaded.sha256),
        method=method,
        models=list(models or []),
        code_version=environment["code_version"],
        dependency_versions=environment["dependency_versions"],
        expert_adjudication=expert_adjudication,
        prompt_refs=list(prompt_refs or []),
        example_refs=list(example_refs or []),
        precedent_refs=list(precedent_refs or []),
        existing_decision_refs=[],
        options=dict(options or {}),
    )


def snapshot_input_digest(
    snapshot: InputSnapshot, *, flow_id: str, flow_version: str
) -> str:
    return sha256_of(
        {
            "flow_id": flow_id,
            "flow_version": flow_version,
            "snapshot": snapshot.model_dump(mode="json"),
        }
    )


def rules_archive_rel(execution_id: str, space=None) -> str:
    if space is not None:
        return space.run_path(execution_id, RULES_ARCHIVE_REL)
    return f"runs/{execution_id}/{RULES_ARCHIVE_REL}"


def create_execution(
    space: Workspace,
    *,
    request_key: str,
    input_snapshot: InputSnapshot,
    flow: FlowAdapter | DefaultFlowAdapter,
    execution_id: str | None = None,
) -> tuple[ExecutionHandle, bool]:
    """Create/reuse executions: identical request/input reuses; changed same-key input
    rejects.

    Archive rules under runs/<execution>/inputs/rules.toml; reject mismatches between
    current rules and snapshot declarations.
    """
    digest = snapshot_input_digest(
        input_snapshot, flow_id=flow.flow_id, flow_version=flow.flow_version
    )
    if execution_id is not None and space.identity.format_version not in {2, 3}:
        raise ValueError(
            "Named executions require a benchmark or literature workspace."
        )
    with space.lock():
        store = space.store()
        existing = store.find_execution_by_request(request_key)
        if existing is not None:
            if execution_id is not None and existing.execution_id != execution_id:
                raise OperationKeyConflictError(
                    "The request key identifies another execution."
                )
            if existing.input_digest == digest:
                return _handle_from_row(space, existing), True
            raise OperationKeyConflictError(
                f"请求键 {request_key!r} 已用于不同输入"
                f"（已有执行 {existing.execution_id}）；"
                "输入改变后必须使用新的请求键建立新执行"
            )
        if execution_id is not None:
            import re

            if space.is_literature:
                source = (
                    store.read_record(input_snapshot.source_refs[0])
                    if len(input_snapshot.source_refs) == 1
                    else None
                )
                if (
                    not re.fullmatch(r"\d{4}\.\d{4,5}", execution_id)
                    or not isinstance(source, SourcePackageRecord)
                    or source.data.paper_key != execution_id
                ):
                    raise ValueError(
                        "A named literature execution must match its arXiv paper."
                    )
            elif not re.fullmatch(r"exec\d+/\d{4}\.\d{4,5}", execution_id):
                raise ValueError(
                    "A named execution must identify one benchmark round and paper."
                )
            if store.get_execution(execution_id) is not None:
                raise OperationKeyConflictError("The named execution already exists.")
        else:
            with store.transaction() as tx:
                execution_id = tx.next_id("exec")
        # Publish rule archives before the final transaction; interruption leaves at most unreferenced files.
        archive = space.publish_run_file(execution_id, RULES_ARCHIVE_REL, rules_bytes())
        if archive.sha256 != input_snapshot.rules.sha256:
            raise WorkspaceIntegrityError(
                "当前规则内容与快照声明的规则引用不一致；"
                "先更新快照中的 rules 引用再创建执行"
            )
        run = _build_run_record(
            execution_id=execution_id,
            mode=flow.mode,
            snapshot=input_snapshot,
            created_at=datetime.now(UTC),
        )
        with store.transaction() as tx:
            # Recheck request keys inside final transactions to prevent duplicate concurrent creation.
            raced = tx.find_execution_by_request(request_key)
            if (
                raced is not None
            ):  # pragma: no cover - impossible within single-process locking
                raise OperationKeyConflictError(f"请求键 {request_key!r} 已被并发使用")
            tx.insert_execution(
                _execution_row(
                    execution_id=execution_id,
                    request_key=request_key,
                    input_digest=digest,
                    run_record_id=run.record_id,
                    flow_id=flow.flow_id,
                    flow_version=flow.flow_version,
                    mode=flow.mode,
                    state=run.data.state,
                )
            )
            tx.insert_record(run)
            tx.insert_receipt(
                Receipt(
                    operation_key=f"create_execution:{request_key}",
                    kind="create_execution",
                    content_digest=digest,
                    execution_id=execution_id,
                    step_id=None,
                    result_refs=(run.ref(),),
                    links={"request_key": request_key},
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
        return get_execution(space, execution_id), False


def get_execution(space: Workspace, execution_id: str) -> ExecutionHandle:
    row = space.store().get_execution(execution_id)
    if row is None:
        raise WorkspaceIntegrityError(f"未知执行：{execution_id}")
    return _handle_from_row(space, row)


def _handle_from_row(space: Workspace, row) -> ExecutionHandle:
    run = space.store().read_record_current(row.run_record_id)
    if not isinstance(run, RunRecord):  # pragma: no cover - data should be consistent
        raise WorkspaceIntegrityError(f"执行 {row.execution_id} 的运行清单缺失")
    return ExecutionHandle(
        execution_id=row.execution_id,
        request_key=row.request_key,
        run_ref=run.ref(),
        input_snapshot=run.data.input_snapshot,
        input_digest=row.input_digest,
        flow_id=row.flow_id,
        flow_version=row.flow_version,
        mode=row.mode,
    )


def _execution_row(
    *,
    execution_id: str,
    request_key: str,
    input_digest: str,
    run_record_id: str,
    flow_id: str,
    flow_version: str,
    mode: str,
    state: str,
):
    from ..workspace import ExecutionRow

    return ExecutionRow(
        execution_id=execution_id,
        request_key=request_key,
        input_digest=input_digest,
        run_record_id=run_record_id,
        flow_id=flow_id,
        flow_version=flow_version,
        mode=mode,
        state=state,
        created_at=datetime.now(UTC).isoformat(),
    )


def _build_run_record(
    *, execution_id: str, mode: str, snapshot: InputSnapshot, created_at: datetime
) -> RunRecord:
    from ..records.common import build_record

    return build_record(
        RunRecord,
        record_id=f"run-{execution_id}",
        created_at=created_at,
        execution_id=execution_id,
        data=RunData(
            execution_id=execution_id,
            mode=mode,
            input_snapshot=snapshot,
            state="created",
            steps=[],
            result_refs=[],
            model_judgments=[],
            usage=Usage(tokens=None, cost=None, currency=None),
            errors=[],
        ),
    )


def commit_step_result(
    space: Workspace,
    execution: ExecutionHandle,
    *,
    step_id: str,
    input_digest: str,
    records: list[AnyRecord],
    result_refs: list[RecordRef],
) -> Receipt:
    """Commit records, new run revisions, step caches, and receipts together.

    Same-input reentry reuses saved execution-local results without second revisions.
    Derive operation keys from execution/step/input digests.
    """
    store = space.store()
    existing = store.find_step_result(execution.execution_id, step_id, input_digest)
    if existing is not None:
        row = store.find_receipt(existing.operation_key)
        if row is None:  # pragma: no cover - data should be consistent
            raise WorkspaceIntegrityError(
                f"步骤结果 {step_id} 缺少保存回执：{existing.operation_key}"
            )
        _ensure_step_finished(space, execution, step_id, list(existing.result_refs))
        return row.to_receipt(replayed=True)
    from ..workspace.retention import require_resumable

    require_resumable(space, execution.execution_id)
    run = store.read_record_current(execution.run_ref.record_id)
    if not isinstance(run, RunRecord):  # pragma: no cover - defensive check
        raise WorkspaceIntegrityError(f"执行 {execution.execution_id} 的运行清单缺失")
    updated_steps = {step.step_id: step for step in run.data.steps}
    previous = updated_steps.get(step_id)
    updated_steps[step_id] = StepRecord(
        step_id=step_id,
        state="finished",
        input_refs=[],
        # Retain archived interruption/recovery attempts; completed states keep response references.
        attempts=list(previous.attempts) if previous is not None else [],
        result_refs=list(result_refs),
        error_refs=[],
    )
    new_run = run.model_copy(
        update={
            "revision": run.revision + 1,
            "created_at": datetime.now(UTC),
            "data": run.data.model_copy(
                update={
                    "state": "running",
                    "steps": [updated_steps[key] for key in sorted(updated_steps)],
                    "result_refs": [
                        *run.data.result_refs,
                        *[
                            ref
                            for ref in result_refs
                            if ref not in run.data.result_refs
                        ],
                    ],
                }
            ),
        }
    )
    request = CommitRequest(
        operation_key=f"step:{execution.execution_id}:{step_id}:{input_digest}",
        kind="step_result",
        new_records=[*records, new_run],
        expected_revisions={run.record_id: run.revision},
        result_refs=list(result_refs),
        execution_id=execution.execution_id,
        step_id=step_id,
        input_digest=input_digest,
        links={"step_id": step_id},
    )
    return store.commit(request)


def find_step_result(ctx: RuntimeContext, step_id: str, input_digest: str):
    """Find saved execution step results and synchronize run step state on hits."""
    existing = ctx.store.find_step_result(
        ctx.execution.execution_id, step_id, input_digest
    )
    if existing is not None:
        _ensure_step_finished(
            ctx.space, ctx.execution, step_id, list(existing.result_refs)
        )
    return existing


def _ensure_step_finished(
    space: Workspace,
    execution: ExecutionHandle,
    step_id: str,
    result_refs: list[RecordRef],
) -> None:
    """Restore run step state to completed when reentry reuses saved results."""
    store = space.store()
    run = store.read_record_current(execution.run_ref.record_id)
    if not isinstance(run, RunRecord):  # pragma: no cover - defensive check
        raise WorkspaceIntegrityError(f"执行 {execution.execution_id} 的运行清单缺失")
    step = next((s for s in run.data.steps if s.step_id == step_id), None)
    if step is not None and step.state == "finished":
        return
    append_run_revision(
        space,
        execution,
        step=StepRecord(
            step_id=step_id,
            state="finished",
            input_refs=[],
            attempts=list(step.attempts) if step is not None else [],
            result_refs=result_refs,
            error_refs=[],
        ),
        operation_key=(
            f"run_step_finished:{execution.execution_id}:{step_id}:{run.revision + 1}"
        ),
    )


def append_run_revision(
    space: Workspace,
    execution: ExecutionHandle,
    *,
    state: str | None = None,
    step: StepRecord | None = None,
    model_judgment: ModelJudgment | None = None,
    errors: list[RunError] | None = None,
    extra_result_refs: list[RecordRef] | None = None,
    operation_key: str | None = None,
    meta_updates: dict[str, str] | None = None,
    usage: Usage | None = None,
    artifacts: list[ArtifactRef] | None = None,
) -> RunRecord:
    """Append run revisions for pauses/completion/failure/model judgments.

    Write meta_updates in the same business transaction; completion credentials and
    revisions cannot be separated by a failure window.
    """
    from ..workspace.retention import require_resumable

    require_resumable(space, execution.execution_id)
    store = space.store()
    run = store.read_record_current(execution.run_ref.record_id)
    if not isinstance(run, RunRecord):  # pragma: no cover - defensive check
        raise WorkspaceIntegrityError(f"执行 {execution.execution_id} 的运行清单缺失")
    steps = {item.step_id: item for item in run.data.steps}
    if step is not None:
        steps[step.step_id] = step
    judgments = list(run.data.model_judgments)
    if model_judgment is not None and not any(
        existing.step_id == model_judgment.step_id
        and existing.judgment == model_judgment.judgment
        for existing in judgments
    ):
        judgments.append(model_judgment)
    run_errors = list(run.data.errors)
    if errors:
        run_errors.extend(errors)
    result_refs = list(run.data.result_refs)
    for ref in extra_result_refs or []:
        if ref not in result_refs:
            result_refs.append(ref)
    run_artifacts = list(run.data.artifacts)
    for ref in artifacts or []:
        if ref not in run_artifacts:
            run_artifacts.append(ref)
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "steps": [steps[key] for key in sorted(steps)],
        "model_judgments": judgments,
        "errors": run_errors,
        "result_refs": result_refs,
        "artifacts": run_artifacts,
    }
    if state is not None:
        payload["state"] = state
    if usage is not None:
        payload["usage"] = usage
    new_run = run.model_copy(
        update={
            "revision": run.revision + 1,
            "created_at": now,
            "data": run.data.model_copy(update=payload),
        }
    )
    # model_copy does not run validators. Never persist a revision that only
    # becomes unreadable on the next status/resume/backup operation.
    new_run = RunRecord.model_validate(new_run.model_dump(mode="python"))
    store.commit(
        CommitRequest(
            operation_key=operation_key
            or (
                f"run_update:{execution.execution_id}:{run.revision + 1}:"
                + sha256_of(
                    {
                        "state": state,
                        "step": step.step_id if step is not None else None,
                        "judgment": model_judgment.judgment
                        if model_judgment is not None
                        else None,
                        "errors": [error.message for error in (errors or [])],
                        "extra_refs": [
                            ref.model_dump(mode="json")
                            for ref in (extra_result_refs or [])
                        ],
                        "artifacts": [ref.model_dump() for ref in artifacts or []],
                    }
                )[:16]
            ),
            kind="run_update",
            new_records=[new_run],
            expected_revisions={run.record_id: run.revision},
            execution_id=execution.execution_id,
            links={"state": new_run.data.state},
            meta_updates=dict(meta_updates or {}),
        )
    )
    if state is not None:
        with store.transaction() as tx:
            tx.update_execution_state(execution.execution_id, state)
    return new_run


def _resolved_input_file(space: Workspace, rel: str) -> Path:
    """Resolve fixed-input files componentwise within workspace roots, rejecting
    escapes/symlinks.
    """
    return resolve_within(space.root, rel)


def verify_execution_inputs(space: Workspace, execution: ExecutionHandle) -> list[str]:
    """Verify fixed source/rule/run file existence and hashes."""
    problems: list[str] = []
    store = space.store()
    snapshot = execution.input_snapshot
    for ref in snapshot.source_refs:
        record = store.read_record(ref)
        if record is None:
            problems.append(f"输入来源记录缺失：{ref.record_id} v{ref.revision}")
            continue
        if not isinstance(record, SourcePackageRecord):
            problems.append(
                f"输入来源不是来源包：{ref.record_id}（{record.RECORD_NAME}）"
            )
            continue
        for file in record.data.files:
            try:
                path = _resolved_input_file(space, file.path)
            except WorkspaceIntegrityError as exc:
                problems.append(f"来源文件路径不安全：{exc}")
                continue
            if not path.is_file():
                problems.append(f"来源文件缺失：{file.path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != file.sha256:
                problems.append(f"来源文件哈希改变：{file.path}")
    try:
        archive = _resolved_input_file(
            space, rules_archive_rel(execution.execution_id, space)
        )
    except WorkspaceIntegrityError as exc:
        archive = None
        problems.append(f"规则归档路径不安全：{exc}")
    if archive is not None:
        if not archive.is_file():
            problems.append(
                f"规则归档缺失：{rules_archive_rel(execution.execution_id, space)}"
            )
        else:
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != snapshot.rules.sha256:
                problems.append("规则归档与快照声明的规则哈希不一致")
    prepared_refs = []
    for name in ("prepared_input", "prepared_prompt", "extraction_input"):
        if name in snapshot.options:
            try:
                prepared_refs.append(
                    ArtifactRef.model_validate_json(snapshot.options[name])
                )
            except ValueError as exc:
                problems.append(f"固定准备引用无效：{name}：{exc}")
    for label, refs in (
        ("提示", snapshot.prompt_refs),
        ("示例", snapshot.example_refs),
        ("准备", prepared_refs),
    ):
        for ref in refs:
            try:
                path = _resolved_input_file(space, ref.path)
            except WorkspaceIntegrityError as exc:
                problems.append(f"{label}归档路径不安全：{exc}")
                continue
            if not path.is_file():
                problems.append(f"{label}归档缺失：{ref.path}")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != ref.sha256:
                problems.append(f"{label}归档哈希改变：{ref.path}")
    for ref in snapshot.precedent_refs:
        artifact = ref.text
        try:
            path = _resolved_input_file(space, artifact.path)
        except WorkspaceIntegrityError as exc:
            problems.append(f"判例归档路径不安全：{exc}")
            continue
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256
        ):
            problems.append(f"判例归档缺失或哈希改变：{artifact.path}")
    return problems


def verify_flow_compatibility(
    space: Workspace, execution: ExecutionHandle, flow: Any
) -> list[str]:
    """Match adapters, code, and dependency versions exactly against frozen input."""
    problems: list[str] = []
    if flow.flow_id != execution.flow_id or flow.flow_version != execution.flow_version:
        problems.append(
            f"流程适配器不匹配：固定 {execution.flow_id} v{execution.flow_version}，"
            f"当前 {flow.flow_id} v{flow.flow_version}"
        )
    environment = current_environment()
    if environment["code_version"] != execution.input_snapshot.code_version:
        problems.append(
            f"代码版本不符：固定 {execution.input_snapshot.code_version}，"
            f"当前 {environment['code_version']}"
        )
    for name, version in execution.input_snapshot.dependency_versions.items():
        actual = environment["dependency_versions"].get(name)
        if actual != version:
            problems.append(
                f"依赖 {name} 版本不符：固定 {version}，当前 {actual or '未知'}"
            )
    return problems
