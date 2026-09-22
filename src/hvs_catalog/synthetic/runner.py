"""合成流程的进程内入口：导入材料、建立执行、推进与提交决定。

测试与发行检查通过这些函数走完整的公共链路：initialize/open →
导入材料 → create_execution → resume_execution → submit_decision →
备份恢复。故障注入由 ``SyntheticFlow(fault_hooks=...)`` 完成，不进入
正常用户流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..records.common import RecordRef
from ..records.questions import Actor
from ..records.runs import ModelSpec
from ..review import DecisionSubmission, ReviewService
from ..review.service import QuestionView
from ..runtime import (
    build_input_snapshot,
    create_execution,
    resume_execution,
)
from ..workspace import CommitRequest, Workspace
from . import material as m
from .flow import SyntheticFlow
from .service import MODEL_NAME, MockExtractionService

OPERATOR_ID = "synthetic-expert-01"


@dataclass
class Scenario:
    """一个合成场景的固定输入。"""

    paper: str
    expert_adjudication: bool = True
    request_key: str | None = None


def import_materials(space: Workspace, paper: str) -> RecordRef:
    """发布合成论文文件并提交来源包与证据记录（显式本地清单导入）。"""
    from datetime import UTC, datetime

    ref = space.publish_source_file(
        f"r05/{paper.lower()}/paper.tex", m.tex_bytes(paper)
    )
    created_at = datetime.now(UTC)
    source, evidences = m.build_source_and_evidence(
        paper, created_at=created_at, execution_id=None
    )
    assert ref.path == m.source_relative_path(paper)
    space.store().commit(
        CommitRequest(
            operation_key=f"import:{m.PAPER_KEYS[paper]}",
            kind="material_import",
            new_records=[source, *evidences],
            result_refs=[source.ref()],
        )
    )
    return source.ref()


def start_execution(
    space: Workspace,
    *,
    paper: str = "A",
    request_key: str | None = None,
    expert_adjudication: bool = True,
    fault_hooks: dict | None = None,
):
    """导入材料、建立执行并返回句柄与流程适配器；不推进流程。"""
    source_ref = import_materials(space, paper)
    snapshot = build_input_snapshot(
        source_refs=[source_ref],
        method="direct",
        models=[
            ModelSpec(step_id=None, provider=None, model=MODEL_NAME, parameters={})
        ],
        expert_adjudication=expert_adjudication,
    )
    flow = SyntheticFlow(paper=paper, fault_hooks=fault_hooks)
    key = request_key or f"synthetic:{m.PAPER_KEYS[paper]}:{expert_adjudication}"
    execution, reused = create_execution(
        space,
        request_key=key,
        input_snapshot=snapshot,
        flow=flow,
    )
    return execution, flow, reused


def run(space: Workspace, execution_id: str, flow: SyntheticFlow, service):
    """推进执行（等价于恢复入口）；返回执行报告。"""
    return resume_execution(space, execution_id, flow, service=service)


def open_service(ledger_path: str | Path) -> MockExtractionService:
    return MockExtractionService(ledger_path)


def pending_questions(
    space: Workspace, execution_id: str | None = None
) -> list[QuestionView]:
    return ReviewService(space).list_questions(execution_id=execution_id)


def submit_expert_decision(
    space: Workspace,
    *,
    execution_id: str,
    question_ref: RecordRef,
    request_key: str,
    outcome: str = "resolved",
    patch="default",
    answer_original: str = "无总体结论：作者在两种模型下给出相反结论且未作总体判断。",
    answer_en: str = (
        "No overall conclusion: the author gives opposite conditional"
        " conclusions and defers an overall assessment."
    ),
    reason: str = "Synthetic expert decision fixed in R05 test inputs.",
):
    """按固定测试内容提交专家决定；补丁目标取自问题自身。"""
    review = ReviewService(space)
    question = review.get_question(question_ref)
    submission = DecisionSubmission(
        question_ref=question_ref,
        fingerprint=question.record.data.fingerprint,
        actor=Actor(kind="human", id=OPERATOR_ID),
        outcome=outcome,
        answer_original=answer_original,
        answer_en=answer_en,
        reason=reason,
        evidence_refs=[],
        patch=(
            m.expert_decision_patch(
                question.record.data.target_refs[0],
                evidence_ref=question.record.data.evidence_refs[0],
            )
            if patch == "default" and outcome == "resolved"
            else (None if patch == "default" else patch)
        ),
    )
    return review.submit_decision(
        execution_id=execution_id,
        step_id="extract_contribution",
        submission=submission,
        request_key=request_key,
    )
