"""Fixed-PDF preparation using an explicit parser and the shared durable runtime."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
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
from .mineru_official import OfficialMinerU

__all__ = [
    "MaterialsPreparationError",
    "MaterialsService",
    "PreparationHandle",
    "PreparationReport",
]

PREPARED_INPUT_RELPATH = "outputs/prepared-input.json"


class MaterialsPreparationError(Exception):
    """Engineering preparation failure; retain committed facts and diagnostics."""


@dataclass(frozen=True)
class PreparationHandle:
    """prepare_materials result: execution identity and echoed frozen configuration."""

    execution_id: str
    request_key: str
    reused: bool
    source_ref: dict
    input_digest: str
    flow_id: str
    flow_version: str


@dataclass
class PreparationReport:
    """One advancement result: state, steps, questions, and gap summary."""

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
    """Resolve the exact preparation source revision without redownload/version mixing."""
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
        # Read explicitly saved historical references first, not current selected materials;
        # unavailable current selection must not block saved versions (second-review finding 25).
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
    if not isinstance(
        record, SourcePackageRecord
    ):  # pragma: no cover - defensive check
        raise MaterialsPreparationError("选定材料的来源记录缺失或类型不符")
    return selection.material_ref, record


def _latest_checkpoint_state(space: Workspace, execution_id: str) -> dict | None:
    """Read latest execution checkpoint values without writes; missing threads return None."""
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
    """Collect standard {path, sha256} attachment references from checkpoint state."""
    from ..workspace.store import walk_artifact_refs

    return list(walk_artifact_refs(state))


class MaterialsService:
    """Material-preparation service for initialized workspaces."""

    def __init__(self, space: Workspace, *, runner: OfficialMinerU | None = None):
        self._space = space
        self._runner = runner or OfficialMinerU(space)

    # ---- Creation and advancement ----

    def prepare_materials(
        self,
        *,
        request_key: str,
        paper: str | None = None,
        version: int | None = None,
        source_ref: RecordRef | None = None,
    ) -> tuple[PreparationHandle, PreparationReport]:
        """Create or reuse identical-request/input preparation execution and advance it.

        Freeze the PDF source revision and parser configuration. Any changed input under the same key rejects;
        changes require a new key. Accept no Gold or answer paths.
        """
        source, record = _resolve_source(
            self._space, paper=paper, version=version, source_ref=source_ref
        )
        request_payload = {
            "source_ref": source.model_dump(mode="json"),
            "paper_key": record.data.paper_key,
            "parser": self._runner.fingerprint(),
            "parser_fingerprint": parser_fingerprint(),
        }
        snapshot = build_input_snapshot(
            source_refs=[source],
            method="materials-prepare",
            expert_adjudication=False,
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
            input_digest=handle.input_digest,
            flow_id=FLOW_ID,
            flow_version=FLOW_VERSION,
        )
        report = self.resume_preparation(handle.execution_id)
        return prep_handle, report

    def resume_preparation(self, execution_id: str) -> PreparationReport:
        """Advance preparation using only execution-saved fixed input during recovery."""
        flow = MaterialsFlow(runner=self._runner)
        runtime_report = resume_execution(self._space, execution_id, flow, service=None)
        return self._build_report(execution_id, runtime_report)

    # ---- Queries and consumption ----

    def get_preparation(self, execution_id: str) -> dict:
        """Read-only state/configuration/step/question/gap/summary queries."""
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
        """Read prepared attachments, explicitly rejecting unfinished executions or missing
        files.
        """
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
        bundle = self.get_prepared_input(execution_id)
        source = self._space.store().read_record(
            RecordRef.model_validate(bundle["prepared_source_ref"])
        )
        for file in source.data.files:
            self._space.read_file(ArtifactRef(path=file.path, sha256=file.sha256))
        return {
            "execution_id": execution_id,
            "ok": True,
            "materials_complete": bundle["summary"]["materials_complete"],
            "summary": bundle["summary"],
            "gaps": bundle["gaps"],
            "problems": [],
            "verified_artifacts": [f.path for f in source.data.files],
        }

    def export_literature(self, manifest: dict, destination: Path) -> None:
        """Export native materials and the existing location index, with a gap inventory."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        inventory = []

        def write(path, payload, *, replace=False):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and not replace:
                if path.read_bytes() != payload:
                    raise MaterialsPreparationError(f"已有材料不同，不能覆盖：{path}")
                return
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)

        for paper in manifest["papers"]:
            preparation = paper["preparation"]
            item = {
                "arxiv_id": paper["arxiv_id"],
                "versioned_id": paper["download"]["versioned_id"],
                "state": preparation["state"],
                "errors": preparation["errors"],
                "gaps": [],
            }
            inventory.append(item)
            folder = destination / paper["arxiv_id"].replace("/", "_")
            input_ref = paper["download"]["source_ref"]
            if input_ref is not None:
                basis = self._space.store().read_record(
                    RecordRef.model_validate(input_ref)
                )
                pdf = next(f for f in basis.data.files if f.role == "pdf")
                write(
                    folder / "arxiv.pdf",
                    self._space.read_file(
                        ArtifactRef(path=pdf.path, sha256=pdf.sha256)
                    ),
                )
                item["pdf_sha256"] = pdf.sha256
            if preparation["state"] != "finished":
                if paper.get("issue"):
                    item["errors"] = [*item["errors"], paper["issue"]["message"]]
                continue
            bundle = self.get_prepared_input(preparation["execution_id"])
            source = self._space.store().read_record(
                RecordRef.model_validate(bundle["prepared_source_ref"])
            )
            item["gaps"] = bundle["gaps"]
            for role, name in (
                ("parser_output", "mineru.zip"),
                ("pdf_index", "pdf-blocks.json"),
            ):
                file = next(f for f in source.data.files if f.role == role)
                content = self._space.read_file(
                    ArtifactRef(path=file.path, sha256=file.sha256)
                )
                write(folder / name, content)
                if role == "parser_output":
                    with zipfile.ZipFile(io.BytesIO(content)) as archive:
                        for member in archive.infolist():
                            # The fixed PDF above remains the acquisition source of truth.
                            if not member.is_dir() and not member.filename.endswith(
                                "_origin.pdf"
                            ):
                                write(folder / member.filename, archive.read(member))
        summary = {
            "batch_id": manifest["batch_id"],
            "quality_review": "structural gaps only; transcription accuracy not reviewed",
            "papers": inventory,
        }
        write(
            destination / "summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2).encode(),
            replace=True,
        )

    # ---- Internal helpers ----

    def _bundle_ref(self, handle: ExecutionHandle) -> ArtifactRef:
        """Saved attachment references are authoritative; recomputing hashes on read cannot
        certify rewritten contents.
        """
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
