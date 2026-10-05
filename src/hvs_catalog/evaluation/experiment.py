"""Two-group comparisons vary only expert review for identical paper, material, method, and
model.

S11/E20 enabled runs pause for fixed simulated human decisions and resume;
evaluation/synthetic disabled runs never request/wait for experts and save
model_judgments, never expert decisions. Decisions derive from fixed pipeline mock
semantics, not Gold/scoring/diagnostics. Gold enters scoring only after extraction.

Report per-group/per-paper L0/L1/L2 quality, completion/pending/failure,
questions/expert counts, calls/usage/tokens, and expert time without a composite score.
Unknown usage/time is null. Creation-to-answer wall time includes queues and cannot
establish expert work time, so expert time is always null. science_completion and
run_state differ: finished graphs may still have unresolved science. Failed/partial
groups retain saved outputs, original errors, usage, and expert/question facts and score
against the complete planned denominator. Block groups lacking safe evaluation
identities with blocked_reason rather than fabricated quality. All materials are
fictional.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

WorkspaceLike = (
    Any  # Workspace is structurally required; avoid circular imports solely for typing.
)

from ..pipeline import (
    ExtractionReport,
    ExtractionRequest,
    StepModel,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_extraction,
    submit_expert_decision,
)
from ..pipeline.mock import PIPELINE_EXPERT_ID, PipelineMockService
from ..records.common import RecordRef
from ..records.paper import PaperRecord
from ..records.questions import DecisionRecord
from ..records.roster import RosterRecord
from .adapt import GoldAdaptationError, prediction_from_execution, science_completion
from .scoring import (
    PaperResult,
    ScoringReport,
    protocol_only_config,
    score_batch,
)
from .synthetic_gold import build_experiment_plan, synthetic_gold_for_paper

__all__ = [
    "ExperimentError",
    "GroupOutcome",
    "build_experiment_report",
    "run_two_group_experiment",
]

#: Fixed simulated expert decisions follow question categories and mock-material semantics,
#: entirely independently of Gold and scoring.
SIMULATED_EXPERT_CHOICES = {
    "author_assessment": "no_overall_conclusion",
    "inclusion": "include",
}


class ExperimentError(ValueError):
    """The fixed experiment cannot complete, for example because material/method/switch
    combinations are unsupported.
    """


@dataclass
class GroupOutcome:
    """Complete execution facts for one group, shared by reports and tests."""

    switch: str  # expert_on | expert_off
    request_key: str
    handle: dict
    report: dict  # Final ExtractionReport.to_dict().
    status: dict  # Read-only get_extraction state.
    service_calls: list[dict]
    decisions: list[dict]
    predictions: Any  # PredictionPaperEntry or None when blocked.
    blocked_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "switch": self.switch,
            "request_key": self.request_key,
            "handle": self.handle,
            "report": self.report,
            "status": self.status,
            "service_calls": list(self.service_calls),
            "decisions": list(self.decisions),
            "predictions": (
                self.predictions.model_dump(mode="json")
                if self.predictions is not None
                else None
            ),
            "blocked_reason": self.blocked_reason,
        }


def _simulated_choice(category: str | None) -> str:
    if category not in SIMULATED_EXPERT_CHOICES:
        raise ExperimentError(
            f"模拟专家决定不支持问题类别 {category!r}；固定支持："
            + ", ".join(SIMULATED_EXPERT_CHOICES)
        )
    return SIMULATED_EXPERT_CHOICES[category]


def _run_group(
    space,
    *,
    source: RecordRef,
    paper_key: str,
    method: str,
    mode: str,
    expert: bool,
    request_key: str,
    journal_dir: Path | None,
    service: Any = None,
) -> GroupOutcome:
    if service is None:
        journal = (
            Path(journal_dir) / f"{request_key}.jsonl"
            if journal_dir is not None
            else None
        )
        service = PipelineMockService(journal_path=journal)
    request = ExtractionRequest(
        mode=mode,
        method=method,
        source_refs=[source],
        models=[StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    handle = start_extraction(space, request=request, request_key=request_key)
    report: ExtractionReport = resume_extraction(
        space, handle.execution_id, service=service
    )
    decision_logs: list[dict] = []
    guard = 0
    while report.outcome == "paused":
        if not expert:
            raise ExperimentError("关闭专家组不应暂停等待专家；运行配置与开关不一致")
        pending = report.pending_question
        if (
            pending is None
        ):  # pragma: no cover - paused executions always have pending questions
            raise ExperimentError("执行暂停但没有待办问题")
        question_ref = RecordRef.model_validate(pending["question_ref"])
        view = next(
            view
            for view in _questions_of(space, handle.execution_id)
            if view.question_ref == question_ref
        )
        choice = _simulated_choice(view.record.data.category)
        receipt = submit_expert_decision(
            space,
            execution_id=handle.execution_id,
            question_ref=question_ref,
            request_key=f"{request_key}:decision:{len(decision_logs)}",
            choice=choice,
        )
        decision_logs.append(
            {
                "question_ref": question_ref.model_dump(mode="json"),
                "choice": choice,
                "decision_ref": (
                    receipt.decision_ref.model_dump(mode="json")
                    if receipt.decision_ref is not None
                    else None
                ),
                "actor": PIPELINE_EXPERT_ID,
            }
        )
        report = resume_extraction(space, handle.execution_id, service=service)
        guard += 1
        if guard > 10:  # pragma: no cover - mock material has at most one question
            raise ExperimentError("模拟决定循环超出预期，中止实验")
    if report.outcome not in ("finished", "failed"):
        # Retain failed/partial groups; explicitly block only states that cannot advance.
        raise ExperimentError(
            f"实验组 {request_key} 处于不可报告状态（outcome={report.outcome}）"
        )
    status = get_extraction(space, handle.execution_id)
    predictions = None
    blocked_reason = None
    try:
        predictions = prediction_from_execution(space, handle.execution_id)
    except GoldAdaptationError as exc:
        # Without a safe evaluation identity, block the group rather than fabricating empty predictions or quality.
        blocked_reason = f"预测适配失败：{exc}"
    return GroupOutcome(
        switch="expert_on" if expert else "expert_off",
        request_key=request_key,
        handle=handle.to_dict(),
        report=report.to_dict(),
        status=status,
        service_calls=list(getattr(service, "calls", [])),
        decisions=decision_logs,
        predictions=predictions,
        blocked_reason=blocked_reason,
    )


def _questions_of(space, execution_id: str):
    from ..pipeline import pending_questions

    return pending_questions(space, execution_id)


def _roster_record(space, execution_id: str) -> RosterRecord | None:
    from ..pipeline import execution_result_refs
    from ..workspace import WorkspaceError

    try:
        refs = execution_result_refs(space, execution_id)
    except (
        WorkspaceError
    ):  # Blocked groups may lack readable execution identity; mark facts missing.
        return None
    for ref in refs:
        record = space.store().read_record_current(ref.record_id)
        if isinstance(record, RosterRecord):
            return record
    return None


def _roster_counts(roster: RosterRecord | None) -> dict[str, int]:
    counts = {"included": 0, "excluded": 0, "pending": 0}
    if roster is None:
        return counts
    for entry in roster.data.entries:
        counts[entry.disposition] = counts.get(entry.disposition, 0) + 1
    return counts


def _human_decisions(space, execution_id: str) -> list[DecisionRecord]:
    return [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord)
        and record.execution_id == execution_id
        and record.data.actor.kind == "human"
    ]


def _paper_record(space, execution_id: str) -> PaperRecord | None:
    from ..pipeline import execution_result_refs
    from ..workspace import WorkspaceError

    try:
        refs = execution_result_refs(space, execution_id)
    except (
        WorkspaceError
    ):  # Blocked groups may lack readable execution identity; mark facts missing.
        return None
    for ref in refs:
        record = space.store().read_record_current(ref.record_id)
        if isinstance(record, PaperRecord):
            return record
    return None


def _quality_of(paper_key: str, prediction, gold) -> tuple[PaperResult, ScoringReport]:
    plan = build_experiment_plan([paper_key], [gold])
    report = score_batch(plan, [gold], [prediction], auxiliary=protocol_only_config())
    return report.papers[0], report


def _group_summary(space: WorkspaceLike, group: GroupOutcome, gold) -> dict:
    execution_id = group.handle["execution_id"]
    questions = group.status["questions"]
    usage = group.status["usage"]
    roster = _roster_record(space, execution_id)
    paper = _paper_record(space, execution_id)
    science_state, science_reasons = science_completion(group.status, roster, paper)
    quality = None
    scoring_provisional = None
    if group.predictions is not None:
        paper_result, scoring = _quality_of(
            group.predictions.paper_key, group.predictions, gold
        )
        quality = paper_result.model_dump(mode="json")
        scoring_provisional = scoring.batch_provisional
    return {
        "switch": group.switch,
        "execution_id": execution_id,
        "request_key": group.request_key,
        "input_digest": group.handle["input_digest"],
        "flow_id": group.handle["flow_id"],
        "flow_version": group.handle["flow_version"],
        "mode": group.handle["mode"],
        "method": group.handle["method"],
        "models": group.handle["models"],
        "run_state": group.status["state"],
        "failures": list(group.status["errors"]),
        "blocked_reason": group.blocked_reason,
        "roster": _roster_counts(roster),
        "questions": {
            "created": len(questions),
            "answered": sum(1 for q in questions if q["status"] == "answered"),
            "open": sum(1 for q in questions if q["status"] == "open"),
        },
        "decisions": {
            "human": len(_human_decisions(space, execution_id)),
            "simulated_choices": [d["choice"] for d in group.decisions],
        },
        "model_judgments": len(group.status["model_judgments"]),
        "usage": {
            "attempts": usage["attempts"],
            # Unknown usage stays null, never fabricated zero.
            "tokens": usage["tokens"],
        },
        # No trustworthy expert work timing exists; creation-to-answer wall time includes
        # queueing, so never treat it as expert time and always report null.
        "expert_time_seconds": None,
        # Separate scientific completion from graph termination; retain facts after failures/unresolved issues.
        "science_completion": science_state,
        "science_completion_reasons": science_reasons,
        "quality": quality,
        "scoring_provisional": scoring_provisional,
    }


def run_two_group_experiment(
    space: WorkspaceLike,
    paper_key: str,
    *,
    method: str = "roster",
    mode: str = "evaluation",
    journal_dir: Path | None = None,
    service_factory: Any = None,
) -> dict:
    """Run both review-switch groups for one synthetic paper and return a complete
    comparison report.

    Material, method, and model configuration are identical except expert_adjudication
    (S11); disabled evaluation/synthetic runs form the AI-only group (E20). Enabled
    simulated decisions follow fixed material semantics, never Gold/scoring.
    Failed/partial groups still produce complete reports with full planned denominators,
    saved outputs, errors, and usage. service_factory(switch, request_key) injects
    actual failures in tests; default calls use mock services.
    """
    if mode not in ("evaluation", "synthetic"):
        raise ExperimentError(
            "对照实验使用 evaluation/synthetic 模式；daily 不提供纯 AI 对照组"
        )
    source = import_mock_source(space, paper_key)

    def _service_for(switch: str, request_key: str):
        if service_factory is not None:
            return service_factory(switch, request_key)
        journal = (
            Path(journal_dir) / f"{request_key}.jsonl"
            if journal_dir is not None
            else None
        )
        return PipelineMockService(journal_path=journal)

    on = _run_group(
        space,
        source=source,
        paper_key=paper_key,
        method=method,
        mode=mode,
        expert=True,
        request_key=f"experiment:{paper_key}:{method}:expert-on",
        journal_dir=journal_dir,
        service=_service_for("expert_on", f"experiment:{paper_key}:{method}:expert-on"),
    )
    off = _run_group(
        space,
        source=source,
        paper_key=paper_key,
        method=method,
        mode=mode,
        expert=False,
        request_key=f"experiment:{paper_key}:{method}:expert-off",
        journal_dir=journal_dir,
        service=_service_for(
            "expert_off", f"experiment:{paper_key}:{method}:expert-off"
        ),
    )
    return build_experiment_report(space, paper_key, on=on, off=off)


def build_experiment_report(
    space: WorkspaceLike, paper_key: str, *, on: GroupOutcome, off: GroupOutcome
) -> dict:
    """Assemble execution facts and scoring into a two-group report without a composite
    score.
    """
    gold = synthetic_gold_for_paper(paper_key)
    if on.handle["execution_id"] == off.handle["execution_id"]:
        raise ExperimentError("两组必须使用不同的执行")  # pragma: no cover
    report = {
        "schema": "hvs.evaluation.experiment-report",
        "version": 1,
        "paper_key": paper_key,
        "method": on.handle["method"],
        "mode": on.handle["mode"],
        "models": on.handle["models"],
        "groups": {
            "expert_on": _group_summary(space, on, gold),
            "expert_off": _group_summary(space, off, gold),
        },
        "isolation": {
            "execution_ids": [on.handle["execution_id"], off.handle["execution_id"]],
            "input_digests": {
                "expert_on": on.handle["input_digest"],
                "expert_off": off.handle["input_digest"],
            },
            # Experiment checks that disabled fixed input excludes newly added enabled-group decisions.
            "expert_off_existing_decision_refs": off.status["config"][
                "existing_decision_refs"
            ],
            "paper_record_ids": {
                "expert_on": _paper_id(space, on.handle["execution_id"]),
                "expert_off": _paper_id(space, off.handle["execution_id"]),
            },
        },
        "notes": [
            "质量只报告 L0/L1/L2 分层指标，不合成总分。",
            (
                "science_completion 是科学完成状态，与 run_state（运行图终止"
                "状态）分开：图 finished 不等于科学 complete，存在开放/已答"
                "仍未决的科学问题或名单未决条目时为 pending。"
            ),
            (
                "消费者不得以 run_state=finished 或 L0 交付率冒称科学完成率；"
                "科学未决记录在 science_completion_reasons，不是技术错误"
                "（failures 只保留运行错误）。"
            ),
            (
                "专家用时没有可信计时证据，为 null；不把问题创建到答复的墙钟"
                "等待当作专家实际操作时间。"
            ),
            "调用用量未知时为 null，不伪造 0。",
            ("模拟专家决定来自固定模拟材料语义，Gold 与评分不参与提取或专家决定。"),
            ("辅助评分运行在 protocol_only 模拟模式，不代表真实辅助模型能力已验证。"),
        ],
    }
    return report


def _paper_id(space: WorkspaceLike, execution_id: str) -> str | None:
    paper = _paper_record(space, execution_id)
    return paper.record_id if paper is not None else None
