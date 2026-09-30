"""Offline demo: only shared extraction/review/workspace APIs, no private store.

The sibling JSONL journal represents the remote service, not client data. Never
reset, adopt an unmarked workspace, migrate old databases, or remove user files.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..pipeline import (
    ExtractionRequest,
    StepModel,
    get_extraction,
    pending_questions,
    resume_extraction,
    start_extraction,
)
from ..pipeline.flow import ExtractionFlow
from ..pipeline.methods import DIRECT_METHOD
from ..pipeline.mock import (
    PAPER_A,
    PipelineMockService,
    import_mock_source,
    mock_preflight,
    submit_expert_decision,
)
from ..pipeline.registry import MethodRegistration, PipelineRegistry
from ..workspace import initialize_workspace, open_workspace

CRASH_EXIT_CODE = 70
MARKER = {"synthetic_demo": 1}
EXECUTION_ID = "exec-000001"


def calls_path(root: Path) -> Path:
    return root.with_name(root.name + ".calls.jsonl")


def _open(root: Path, *, create=False):
    root = Path(root).resolve()
    if create and (not root.exists() or not any(root.iterdir())):
        return initialize_workspace(root, config=MARKER)
    space = open_workspace(root)
    if space.read_config() != MARKER:
        space.close()
        raise ValueError(
            "不是共享合成演示空间；请另选新目录执行 demo run，不会迁移或清理旧空间"
        )
    return space


def _registry(crash_after_response):
    def crash():
        print("[中断] 响应已到但尚未保存；再次调用风险保留在独立调用日志", flush=True)
        os._exit(CRASH_EXIT_CODE)

    registry = PipelineRegistry()
    registry.register(
        MethodRegistration(
            spec=DIRECT_METHOD,
            build_flow=lambda spec, mode: ExtractionFlow(
                spec,
                mode=mode,
                fault_hooks={"after_response:direct_roster": crash}
                if crash_after_response
                else {},
            ),
            preflight=mock_preflight,
        )
    )
    return registry


def _status(space, service):
    result = get_extraction(space, EXECUTION_ID)
    result["implementation"] = "synthetic-mock"
    result["external_calls"] = service.summary()
    result["calls_path"] = str(service.journal_path)
    result["pending"] = [
        {
            "question_ref": q.question_ref.model_dump(mode="json"),
            "prompt": q.record.data.prompt,
            "evidence_refs": [
                r.model_dump(mode="json") for r in q.record.data.evidence_refs
            ],
            "status": q.status,
            "answered_outcome": q.answered_outcome,
        }
        for q in pending_questions(space, EXECUTION_ID)
    ]
    return result


def run(root: Path, *, crash_after_response=False) -> dict:
    space = _open(root, create=True)
    try:
        existing = space.store().find_execution_by_request("demo")
        if existing is None:
            source = import_mock_source(space, PAPER_A)
            handle = start_extraction(
                space,
                request=ExtractionRequest(
                    source_refs=[source],
                    method="direct",
                    mode="synthetic",
                    models=[StepModel(model="synthetic-mock")],
                    expert_adjudication=True,
                ),
                request_key="demo",
            )
            execution_id = handle.execution_id
        else:
            execution_id = existing.execution_id
        service = PipelineMockService(calls_path(space.root))
        report = resume_extraction(
            space,
            execution_id,
            service=service,
            registry=_registry(crash_after_response),
        )
        result = _status(space, service)
        result["report"] = report.to_dict()
        return result
    finally:
        space.close()


def status(root: Path) -> dict:
    space = _open(root)
    try:
        return _status(space, PipelineMockService(calls_path(space.root)))
    finally:
        space.close()


def decide(root: Path, *, choice: str) -> dict:
    space = _open(root)
    try:
        questions = pending_questions(space, EXECUTION_ID)
        if not questions:
            raise ValueError("没有待办；请先 demo run --space 新目录")
        receipt = submit_expert_decision(
            space,
            execution_id=EXECUTION_ID,
            question_ref=questions[0].question_ref,
            request_key="demo-decision",
            choice=choice,
        )
        service = PipelineMockService(calls_path(space.root))
        report = resume_extraction(space, EXECUTION_ID, service=service)
        result = _status(space, service)
        result["first_save"] = receipt.first_save
        result["report"] = report.to_dict()
        return result
    finally:
        space.close()
