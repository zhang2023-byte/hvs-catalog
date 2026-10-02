"""材料准备共享入口：建立、查询、恢复与无模型消费。

日常与评测共用同一入口：``prepare_materials`` 以准确来源修订建立（或
同请求复用）一个准备执行并推进；``resume_preparation`` 在歧义问题等待
专家后继续；``get_preparation`` 只读查询；``get_prepared_input``／
``consume_prepared_input`` 读取并核对准备输入附件（无模型合成消费者）。

请求固定来源修订、主稿／格式选项、解析代码与依赖、实际转换器版本；
改变输入须用新请求键建立新执行，同请求重放复用已保存结果。准备接口
不接收 Gold，也不把真实材料接入 mock 冒充真实提取。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..pipeline.acquisition.lifecycle import AcquisitionService
from ..records.common import ArtifactRef, RecordRef
from ..records.runs import RunRecord
from ..records.source import SourcePackageRecord
from ..review import ReviewService
from ..runtime import ExecutionHandle, get_execution, resume_execution
from ..runtime.execution import (
    build_input_snapshot,
    create_execution,
)
from ..workspace import Workspace, WorkspaceIntegrityError
from .flow import FLOW_ID, FLOW_VERSION, MaterialsFlow, parser_fingerprint
from .sandbox import ConverterRunner, SandboxedConverterRunner

__all__ = [
    "MaterialsPreparationError",
    "MaterialsService",
    "PreparationHandle",
    "PreparationReport",
]

PREPARED_INPUT_RELPATH = "outputs/prepared-input.json"


class MaterialsPreparationError(Exception):
    """材料准备的工程失败；已提交事实与诊断保留。"""


@dataclass(frozen=True)
class PreparationHandle:
    """``prepare_materials`` 的返回：执行标识与冻结配置回显。"""

    execution_id: str
    request_key: str
    reused: bool
    source_ref: dict
    root_file: str | None
    expert_adjudication: bool
    input_digest: str
    flow_id: str
    flow_version: str


@dataclass
class PreparationReport:
    """一次推进的结果：状态、步骤、问题与缺口汇总。"""

    execution_id: str
    outcome: str  # paused | finished | failed
    mode: str  # fresh | resumed | already-finished
    state: str
    pending_question: dict | None = None
    open_questions: int = 0
    answered_questions: int = 0
    steps: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary: dict | None = None

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "outcome": self.outcome,
            "mode": self.mode,
            "state": self.state,
            "pending_question": self.pending_question,
            "open_questions": self.open_questions,
            "answered_questions": self.answered_questions,
            "steps": list(self.steps),
            "errors": list(self.errors),
            "summary": self.summary,
        }


def _resolve_source(
    space: Workspace,
    *,
    paper: str | None,
    version: int | None,
    source_ref: RecordRef | None,
) -> tuple[RecordRef, SourcePackageRecord]:
    """解析准备输入的准确来源修订；不重新下载或混用版本。"""
    store = space.store()
    if source_ref is not None:
        record = store.read_record(source_ref)
        if not isinstance(record, SourcePackageRecord):
            raise MaterialsPreparationError(
                f"指定的来源引用不是来源包记录：{source_ref.record_id}"
            )
        return source_ref, record
    if not paper:
        raise MaterialsPreparationError("必须提供 paper（arXiv 标识）或 source_ref")
    if version is not None:
        # 显式历史版本优先按准确保存引用读取；不先读当前选定材料——
        # 当前选择不可用不应挡住已保存版本（二次审核发现 25）。
        wanted = AcquisitionService(space).get_version(f"{paper}v{version}")
        if wanted is None or wanted.source_record is None:
            raise MaterialsPreparationError(
                f"{paper}v{version} 没有已保存的材料；不能隐式改用其他版本"
            )
        record = store.read_record(wanted.source_record)
        if not isinstance(record, SourcePackageRecord):  # pragma: no cover
            raise MaterialsPreparationError("版本材料记录缺失或类型不符")
        return wanted.source_record, record
    selection = AcquisitionService(space).selected_material(paper)
    if selection is None:
        raise MaterialsPreparationError(
            f"{paper} 没有已完成的选定材料；先完成获取或本地导入，"
            "材料准备不下载、不猜版本"
        )
    record = store.read_record(selection.material_ref)
    if not isinstance(record, SourcePackageRecord):  # pragma: no cover - 防御
        raise MaterialsPreparationError("选定材料的来源记录缺失或类型不符")
    return selection.material_ref, record


def _latest_checkpoint_state(space: Workspace, execution_id: str) -> dict | None:
    """读取执行最新检查点的状态值（只读；线程缺失返回 None）。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = space.checkpoint_connection()
    try:
        saver = SqliteSaver(conn)
        tup = saver.get_tuple({"configurable": {"thread_id": execution_id}})
        if tup is None or tup.checkpoint is None:
            return None
        return dict(tup.checkpoint.get("channel_values") or {})
    finally:
        conn.close()


def _state_artifact_refs(state: dict) -> list[dict]:
    """收集检查点状态中标准 ``{path, sha256}`` 附件引用。"""
    from ..workspace.store import walk_artifact_refs

    return list(walk_artifact_refs(state))


def _frozen_converters(runner: ConverterRunner) -> dict[str, dict]:
    return {
        tool: {
            "available": item.available,
            "version": item.version,
            "reason": item.reason,
        }
        for tool, item in (
            (tool, runner.availability(tool)) for tool in ("latexmlc", "pandoc")
        )
    }


class MaterialsService:
    """已初始化工作空间的材料准备入口。"""

    def __init__(self, space: Workspace, *, runner: ConverterRunner | None = None):
        self._space = space
        self._runner = runner or SandboxedConverterRunner()

    # ---- 建立／推进 ----

    def prepare_materials(
        self,
        *,
        request_key: str,
        paper: str | None = None,
        version: int | None = None,
        source_ref: RecordRef | None = None,
        root_file: str | None = None,
        range_notations: Sequence[dict] = (),
        expert_adjudication: bool = False,
        encoding: str | None = None,
    ) -> tuple[PreparationHandle, PreparationReport]:
        """建立（或同请求同输入复用）一个材料准备执行并推进。

        请求内容（来源修订、主稿、格式选项、范围记号、成员编码、转换器
        版本）全部进入固定输入快照；同请求键换任何输入都会被拒绝，改变
        输入须用新请求键。接口不接收 Gold 或答案路径。
        """
        if encoding is not None and encoding not in ("utf-8", "latin-1"):
            raise MaterialsPreparationError(
                f"未支持的成员编码 {encoding!r}；受支持：utf-8、latin-1，"
                "未知编码保持未处理，不静默猜"
            )
        source, record = _resolve_source(
            self._space, paper=paper, version=version, source_ref=source_ref
        )
        request_payload = {
            "source_ref": source.model_dump(mode="json"),
            "paper_key": record.data.paper_key,
            "root_file": root_file,
            "range_notations": [dict(item) for item in range_notations],
            "expert_adjudication": bool(expert_adjudication),
            "member_encoding": encoding,
            "converters": _frozen_converters(self._runner),
            "parser_fingerprint": parser_fingerprint(),
        }
        snapshot = build_input_snapshot(
            source_refs=[source],
            method="materials-prepare",
            expert_adjudication=bool(expert_adjudication),
            options={
                "materials_request": json.dumps(
                    request_payload, ensure_ascii=False, sort_keys=True
                )
            },
        )
        flow = MaterialsFlow(runner=self._runner)
        handle, reused = create_execution(
            self._space,
            request_key=request_key,
            input_snapshot=snapshot,
            flow=flow,
        )
        prep_handle = PreparationHandle(
            execution_id=handle.execution_id,
            request_key=request_key,
            reused=reused,
            source_ref=source.model_dump(mode="json"),
            root_file=root_file,
            expert_adjudication=bool(expert_adjudication),
            input_digest=handle.input_digest,
            flow_id=FLOW_ID,
            flow_version=FLOW_VERSION,
        )
        report = self.resume_preparation(handle.execution_id)
        return prep_handle, report

    def resume_preparation(self, execution_id: str) -> PreparationReport:
        """推进一个准备执行；恢复只使用执行内保存的固定输入。"""
        flow = MaterialsFlow(runner=self._runner)
        runtime_report = resume_execution(self._space, execution_id, flow, service=None)
        return self._build_report(execution_id, runtime_report)

    # ---- 查询／消费 ----

    def get_preparation(self, execution_id: str) -> dict:
        """只读查询执行状态、配置、步骤、问题、缺口与汇总。"""
        handle = get_execution(self._space, execution_id)
        run = self._current_run(handle)
        review = ReviewService(self._space)
        questions = review.list_questions(execution_id=execution_id)
        snapshot = handle.input_snapshot
        bundle = None
        errors = [error.message for error in run.data.errors]
        state = _latest_checkpoint_state(self._space, execution_id) or {}
        if run.data.state == "finished":
            try:
                bundle = self.get_prepared_input(execution_id)
            except (
                MaterialsPreparationError,
                WorkspaceIntegrityError,
                json.JSONDecodeError,
            ) as exc:
                errors.append(f"最终材料事实不可读：{exc}")
        return {
            "execution_id": execution_id,
            "state": run.data.state,
            "config": {
                "request": json.loads(snapshot.options.get("materials_request", "{}")),
                "input_digest": handle.input_digest,
                "code_version": snapshot.code_version,
                "dependency_versions": snapshot.dependency_versions,
            },
            "steps": [
                {
                    "step_id": step.step_id,
                    "state": step.state,
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
                    "prompt": view.record.data.prompt,
                }
                for view in questions
            ],
            "errors": errors,
            "gaps": bundle.get("gaps", []) if bundle is not None else state.get("gaps"),
            "summary": bundle.get("summary") if bundle is not None else None,
            "result_refs": [
                ref.model_dump(mode="json") for ref in run.data.result_refs
            ],
        }

    def get_prepared_input(self, execution_id: str) -> dict:
        """读取准备输入附件；执行未完成或附件缺失时明确报错。"""
        handle = get_execution(self._space, execution_id)
        run = self._current_run(handle)
        if run.data.state != "finished":
            raise MaterialsPreparationError(
                f"执行 {execution_id} 状态为 {run.data.state}，尚无完整准备输入"
            )
        ref = self._bundle_ref(handle)
        payload = self._space.read_file(ref)
        bundle = json.loads(payload)
        bundle["bundle_ref"] = ref.model_dump(mode="json")
        return bundle

    def consume_prepared_input(self, execution_id: str) -> dict:
        """无模型合成消费者：核对附件中全部引用与表格回读。

        逐项核对正文视图、ECSV、excerpt 与派生文件的路径／哈希，并用共
        用 CSV／ECSV 逻辑记录接口回读每张成功表。任何不一致都是错误，
        不把损坏输入当作可用材料。
        """
        from ..records.tabular import TabularReadError, read_table_records

        problems: list[str] = []
        verified: list[str] = []
        try:
            bundle = self.get_prepared_input(execution_id)
        except (MaterialsPreparationError, WorkspaceIntegrityError) as exc:
            return {
                "execution_id": execution_id,
                "ok": False,
                "problems": [f"准备输入附件不可读：{exc}"],
                "verified_artifacts": [],
                "tables": [],
                "gaps": [],
                "ranges": [],
                "summary": {},
            }
        # 检查点保存的全部附件引用共同核验（不只 bundle 本体）。
        handle = get_execution(self._space, execution_id)
        state = _latest_checkpoint_state(self._space, execution_id)
        for ref in _state_artifact_refs(state or {}):
            try:
                self._space.read_file(
                    ArtifactRef(path=ref["path"], sha256=ref["sha256"])
                )
            except WorkspaceIntegrityError as exc:
                problems.append(f"检查点附件 {ref['path']}：{exc}")
        _ = handle

        def check(path: str | None, sha: str | None, label: str) -> None:
            if not path or not sha:
                problems.append(f"{label} 缺少路径或哈希")
                return
            try:
                self._space.read_file(ArtifactRef(path=path, sha256=sha))
            except WorkspaceIntegrityError as exc:
                problems.append(f"{label}：{exc}")
                return
            verified.append(label)

        body = bundle.get("body", {})
        if body.get("status") == "organized":
            for entry in body.get("files", []):
                check(
                    entry.get("cleaned_path"),
                    entry.get("cleaned_sha256"),
                    f"正文视图 {entry.get('name')}",
                )
            check(body.get("view_path"), body.get("view_sha256"), "正文总视图")
        tables_read: list[dict] = []
        for table in bundle.get("tables", []):
            if table.get("status") != "success":
                continue
            check(
                table.get("ecsv_path"),
                table.get("ecsv_sha256"),
                f"表 {table.get('table_id')} ECSV",
            )
            try:
                data = self._space.read_file(
                    ArtifactRef(path=table["ecsv_path"], sha256=table["ecsv_sha256"])
                )
                records = read_table_records(data, media_type="text/ecsv")
                tables_read.append(
                    {
                        "table_id": table.get("table_id"),
                        "columns": list(records.columns),
                        "rows": len(records.rows),
                    }
                )
                if (
                    table.get("row_count") is not None
                    and len(records.rows) != table["row_count"]
                ):
                    problems.append(f"表 {table.get('table_id')} 行数与附件声明不一致")
            except (WorkspaceIntegrityError, TabularReadError, KeyError) as exc:
                problems.append(f"表 {table.get('table_id')} 回读失败：{exc}")
        for member in bundle.get("members", []):
            if member.get("derived_path"):
                check(
                    member["derived_path"],
                    member.get("derived_published_sha256"),
                    f"派生文本 {member.get('name')}",
                )
        return {
            "execution_id": execution_id,
            "ok": not problems,
            "materials_complete": not problems
            and bundle.get("summary", {}).get("materials_complete", False),
            "problems": problems,
            "verified_artifacts": verified,
            "tables": tables_read,
            "gaps": bundle.get("gaps", []),
            "ranges": bundle.get("ranges", []),
            "summary": bundle.get("summary", {}),
        }

    # ---- 内部 ----

    def _bundle_ref(self, handle: ExecutionHandle) -> ArtifactRef:
        """以保存时的附件引用为唯一依据；读取时重算哈希不能认证改写。"""
        state = _latest_checkpoint_state(self._space, handle.execution_id)
        saved = (state or {}).get("bundle")
        if not isinstance(saved, dict) or not saved.get("path"):
            raise MaterialsPreparationError(
                f"执行 {handle.execution_id} 检查点缺少已保存的准备输入引用；不伪造输入"
            )
        return ArtifactRef(path=saved["path"], sha256=saved.get("sha256", ""))

    def _current_run(self, handle: ExecutionHandle) -> RunRecord:
        run = self._space.store().read_record_current(handle.run_ref.record_id)
        if not isinstance(run, RunRecord):
            raise WorkspaceIntegrityError(
                f"执行 {handle.execution_id} 的运行清单缺失或损坏"
            )
        return run

    def _build_report(
        self, execution_id: str, runtime_report: Any
    ) -> PreparationReport:
        handle = get_execution(self._space, execution_id)
        run = self._current_run(handle)
        review = ReviewService(self._space)
        questions = review.list_questions(execution_id=execution_id)
        pending = None
        if runtime_report.pending_question is not None:
            view = runtime_report.pending_question
            pending = {
                "question_ref": view.question_ref.model_dump(mode="json"),
                "status": view.status,
                "prompt": view.record.data.prompt,
            }
        summary = None
        if run.data.state == "finished":
            try:
                bundle = self.get_prepared_input(execution_id)
                summary = bundle.get("summary")
            except (MaterialsPreparationError, json.JSONDecodeError):
                summary = None
        return PreparationReport(
            execution_id=execution_id,
            outcome=runtime_report.outcome,
            mode=runtime_report.mode,
            state=run.data.state,
            pending_question=pending,
            open_questions=sum(1 for q in questions if q.status == "open"),
            answered_questions=sum(1 for q in questions if q.status == "answered"),
            steps=[
                {"step_id": s.step_id, "reused": s.reused} for s in runtime_report.steps
            ],
            errors=list(runtime_report.errors),
            summary=summary,
        )
