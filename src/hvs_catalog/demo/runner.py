"""演示空间的进程级操作：运行、状态、裁决与重置。

演示空间是一个独立目录（不放在仓库或日常 ``.work/`` 内），包含三个
数据库文件，分别由三方负责：

- ``business.db``：业务数据库，应用负责，保存步骤结果、裁决决定与汇总；
- ``checkpoint.db``：LangGraph 检查点，框架负责，保存流程执行到哪里、
  中断现场与运行固定输入；
- ``service.db``：模拟服务账单，扮演远程服务端，只增不改，不随客户端
  退出回滚。

恢复规则对应产品设计的保存与恢复：决定先写入业务库、再恢复流程；
恢复会让所在节点从头重跑，节点先查业务库，输入相同则复用结果、不再
调用服务；若上次“收到响应但尚未保存”就退出，业务库没有该结果，
恢复会再次调用服务并再次计费。恢复一律沿用运行开始时固定并保存在
状态中的输入；当前代码或材料与固定输入不一致时给出提示。决定属于
特定版本的问题（摘要覆盖证据与选项）：恢复时节点核对提交决定依据
的问题摘要与当前重建的问题，不一致则不应用该决定，改为按当前问题
重新提问。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from hvs_catalog.demo import graph as demo_graph
from hvs_catalog.demo import service as demo_service
from hvs_catalog.demo import store as demo_store
from hvs_catalog.demo.graph import StepExecution

BUSINESS_DB = "business.db"
CHECKPOINT_DB = "checkpoint.db"
SERVICE_DB = "service.db"
SPACE_DBS = (BUSINESS_DB, CHECKPOINT_DB, SERVICE_DB)

#: 注入退出的进程退出码（EX_SOFTWARE），用于演示“收到响应但尚未保存”
CRASH_EXIT_CODE = 70


class NoPendingQuestionError(RuntimeError):
    """没有等待裁决的问题时提交了新决定。"""


def crash_after_response_hook(step: str) -> None:
    """演示注入：收到响应、尚未保存结果时立即终止进程。"""
    print(
        f"[注入退出] 已收到步骤 {step} 的服务响应，结果尚未保存——"
        f"现在终止进程（退出码 {CRASH_EXIT_CODE}）",
        flush=True,
    )
    os._exit(CRASH_EXIT_CODE)


def _pending_question(snapshot) -> dict | None:
    """从检查点快照取出中断现场中的待裁决问题。"""
    for task in snapshot.tasks:
        for intr in task.interrupts:
            if intr.value:
                return dict(intr.value)
    return None


def _billing_line(billing: dict) -> str:
    return (
        f"[计费] 模拟服务累计调用 {billing['calls']} 次，"
        f"合成费用 {billing['total_cost']:.2f} 元"
    )


def _input_divergence(values: dict) -> list[str]:
    """比较运行固定输入与当前代码、材料，列出不一致的项。"""
    notes: list[str] = []
    if values.get("rules_version") != demo_graph.RULES_VERSION:
        notes.append(
            f"规则 {values.get('rules_version')} → 当前 {demo_graph.RULES_VERSION}"
        )
    pinned_digest = demo_service.material_digest(values.get("material", {}))
    current_digest = demo_service.material_digest(demo_service.default_material())
    if pinned_digest != current_digest:
        notes.append(f"材料摘要 {pinned_digest} → 当前 {current_digest}")
    if values.get("model") != demo_service.MODEL_NAME:
        notes.append(f"模型 {values.get('model')} → 当前 {demo_service.MODEL_NAME}")
    return notes


def _divergence_lines(values: dict) -> list[str]:
    notes = _input_divergence(values)
    if not notes:
        return []
    return [
        "[固定输入] 本次运行固定的输入与当前不一致（"
        + "；".join(notes)
        + "）；恢复沿用固定输入，变化只影响新的执行"
    ]


def _pinned_input_line(values: dict) -> str:
    pinned_digest = demo_service.material_digest(values.get("material", {}))
    return (
        f"[固定输入] 规则 {values.get('rules_version')}；"
        f"材料摘要 {pinned_digest}；模型 {values.get('model')}"
    )


@dataclass
class RunReport:
    """一次 ``demo run`` 的结果。"""

    mode: str  # fresh：新流程；resumed-after-exit：崩溃后续跑；none：未执行
    end_state: str  # awaiting-decision：等待裁决；completed：已完成；already-finished
    executions: list[StepExecution]
    pending_question: dict | None
    billing: dict
    divergence: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.mode == "fresh":
            out.append(f"[启动] 开始处理合成论文 {demo_service.PAPER_ID}（虚构材料）")
        elif self.mode == "resumed-after-exit":
            out.append("[恢复] 检查点显示上次进程未跑完，从待执行节点继续")
        for ex in self.executions:
            if ex.reused:
                out.append(
                    f"[复用] {ex.step}：输入相同，复用业务库已保存结果，未调用服务"
                )
            else:
                if ex.billed_before:
                    out.append(
                        f"[重复计费] {ex.step}：业务库无已保存结果，重新调用模拟服务；"
                        f"同一请求此前已计费 {ex.billed_before} 次"
                        "（收到响应但未保存就退出的直接后果）"
                    )
                else:
                    out.append(f"[调用] {ex.step}：调用模拟服务，结果已保存业务库")
        if self.end_state == "awaiting-decision":
            out.append("[暂停] 流程在裁决处暂停，LangGraph 检查点已保存中断现场")
            question = self.pending_question or {}
            out.append(
                f"[待办] {question.get('prompt', '')}"
                f" 可选：{' / '.join(question.get('choices', []))}"
            )
            out.append(
                "[提示] 用 hvs-catalog demo decide 保存决定；决定落盘后才恢复流程"
            )
        elif self.end_state == "completed":
            out.append("[完成] 演示流程已执行完毕")
        elif self.end_state == "already-finished":
            out.append("[完成] 演示流程此前已完成，本次未重复执行")
        out.extend(self.divergence)
        out.append(_billing_line(self.billing))
        return out


@dataclass
class DecideReport:
    """一次 ``demo decide`` 的结果。"""

    decision_outcome: str  # saved：本次保存；already-in-effect：此前已生效
    completed: bool
    superseded: bool  # 待裁决问题已变化，本次决定基于旧问题、未被应用
    choice: str
    executions: list[StepExecution]
    billing: dict
    summary: dict | None
    divergence: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        if self.decision_outcome == "saved":
            out.append(
                f"[决定] 决定已保存业务库：对象身份裁决取 {self.choice}"
                "（先落盘，再恢复流程）"
            )
        else:
            out.append("[幂等] 相同决定此前已保存，未重复生效")
        for ex in self.executions:
            if ex.reused:
                out.append(
                    f"[复用] {ex.step}：输入相同，复用业务库已保存结果，未调用服务"
                )
            else:
                out.append(f"[调用] {ex.step}：调用模拟服务，结果已保存业务库")
        if self.superseded:
            out.append(
                "[未应用] 待裁决问题已变化（证据或输入更新），本次决定基于旧问题、"
                "未被应用；流程已改为就当前问题暂停，请针对新待办重新提交决定"
            )
        if self.completed:
            out.append("[完成] 演示流程已完成，汇总已写入业务库（同一汇总只写一次）")
            if self.summary:
                groups = self.summary.get("object_groups", [])
                measurements = self.summary.get("all_measurements", [])
                described = "、".join("+".join(group) for group in groups)
                out.append(
                    f"[汇总] 对象分组：{len(groups)} 组（{described}）；"
                    f"测量 {len(measurements)} 条全部保留"
                )
        out.extend(self.divergence)
        out.append(_billing_line(self.billing))
        return out


@dataclass
class StatusReport:
    """一次 ``demo status`` 的结果。"""

    started: bool
    checkpoint_next: tuple[str, ...]
    values: dict
    pending_question: dict | None
    results: list[dict]
    decisions: list[demo_store.StoredDecision]
    summary: dict | None
    billing: dict

    def lines(self) -> list[str]:
        out = []
        if not self.started:
            out.append("[流程进度] 演示流程尚未开始（检查点为空）")
        else:
            out.append(_pinned_input_line(self.values))
            out.extend(_divergence_lines(self.values))
            if self.pending_question is not None:
                out.append(
                    "[流程进度] 检查点：等待裁决"
                    f"（所在步骤待重入：{', '.join(self.checkpoint_next) or '（重问后暂停）'}）"
                )
                question = self.pending_question
                out.append(
                    f"[待办] {question['prompt']}"
                    f" 可选：{' / '.join(question['choices'])}"
                )
            elif self.checkpoint_next:
                out.append(
                    f"[流程进度] 检查点：待执行节点 {', '.join(self.checkpoint_next)}"
                    "（上次进程未跑完）"
                )
            else:
                out.append("[流程进度] 检查点：流程已全部执行完毕")
        if self.results:
            names = "、".join(item["op_id"] for item in self.results)
            out.append(f"[业务数据] 已保存结果 {len(self.results)} 项：{names}")
        else:
            out.append("[业务数据] 尚无已保存结果")
        if self.decisions:
            for item in self.decisions:
                out.append(
                    f"[业务数据] 裁决决定：{item.question_id}"
                    f"（摘要 {item.question_digest}）→ {item.choice}"
                )
        else:
            out.append("[业务数据] 尚无裁决决定")
        if self.summary:
            groups = self.summary.get("object_groups", [])
            measurements = self.summary.get("all_measurements", [])
            described = "、".join("+".join(group) for group in groups)
            out.append(
                f"[汇总] 已生成：对象分组 {len(groups)} 组（{described}）；"
                f"测量 {len(measurements)} 条全部保留"
            )
        else:
            out.append("[汇总] 尚未生成")
        out.append(_billing_line(self.billing))
        return out


class DemoSpace:
    """打开一个演示空间，组装业务库、模拟服务、检查点与图。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.store = demo_store.BusinessStore(root / BUSINESS_DB)
        self.service = demo_service.MockModelService(root / SERVICE_DB)
        self._checkpoint_conn = sqlite3.connect(
            root / CHECKPOINT_DB, check_same_thread=False
        )
        self._checkpoint_conn.row_factory = sqlite3.Row
        self.saver = SqliteSaver(self._checkpoint_conn)
        self.config = {"configurable": {"thread_id": f"demo-{demo_service.PAPER_ID}"}}

    def graph(
        self,
        *,
        after_response=None,
        executions: list[StepExecution] | None = None,
    ):
        return demo_graph.build_graph(
            self.store,
            self.service,
            self.saver,
            after_response=after_response,
            executions=executions,
        )

    def close(self) -> None:
        self.service.close()
        self.store.close()
        self._checkpoint_conn.close()


def run(space_root: Path, *, after_response=None) -> RunReport:
    """运行演示流程：新流程执行到裁决处暂停；上次未跑完则从检查点续跑。

    已在裁决处暂停或已完成的流程不会在此推进——恢复裁决必须先由
    ``decide`` 把决定保存到业务库。
    """
    space = DemoSpace(space_root)
    try:
        executions: list[StepExecution] = []
        graph = space.graph(after_response=after_response, executions=executions)
        snapshot = graph.get_state(space.config)
        if snapshot.created_at is None:
            mode = "fresh"
            graph.invoke(demo_graph.initial_state(), space.config)
        elif _pending_question(snapshot) is None and snapshot.next:
            mode = "resumed-after-exit"
            graph.invoke(None, space.config)
        else:
            mode = "none"
        end = graph.get_state(space.config)
        pending = _pending_question(end)
        if pending is not None:
            end_state = "awaiting-decision"
        elif end.next == () and mode == "none":
            end_state = "already-finished"
        else:
            end_state = "completed"
        divergence = (
            _divergence_lines(end.values) if mode == "resumed-after-exit" else []
        )
        return RunReport(
            mode=mode,
            end_state=end_state,
            executions=executions,
            pending_question=pending,
            billing=space.service.billing_summary(),
            divergence=divergence,
        )
    finally:
        space.close()


def decide(space_root: Path, choice: str, reason: str | None = None) -> DecideReport:
    """保存裁决决定并恢复流程。

    决定先写入业务库再恢复；决定属于当前待裁决问题的版本（摘要覆盖
    证据与选项）。同一版本的问题重复提交同一决定只生效一次，不同决定
    被拒绝。恢复时节点核对决定依据的问题摘要与当前重建的问题：不一
    致则不应用该决定，流程改为就当前问题重新暂停（``superseded``）。
    """
    if choice not in demo_graph.CHOICES:
        raise ValueError(
            f"无效的裁决选择：{choice!r}，可选 {' / '.join(demo_graph.CHOICES)}"
        )
    space = DemoSpace(space_root)
    try:
        graph = space.graph()
        snapshot = graph.get_state(space.config)
        pending = _pending_question(snapshot)
        if pending is None:
            # 无待裁决问题：只有流程已按同一决定完成时幂等重放
            summary = space.store.find_summary(demo_service.PAPER_ID)
            if summary is None:
                raise NoPendingQuestionError(
                    "当前没有等待裁决的问题；请先运行 hvs-catalog demo run"
                )
            if summary["choice"] != choice:
                raise demo_store.DecisionConflictError(
                    f"该流程已按决定 {summary['choice']} 完成，"
                    f"本次提交的 {choice} 与之冲突，被拒绝"
                )
            return DecideReport(
                decision_outcome="already-in-effect",
                completed=False,
                superseded=False,
                choice=choice,
                executions=[],
                billing=space.service.billing_summary(),
                summary=summary,
            )
        digest = pending["question_digest"]
        existing = space.store.find_decision(demo_graph.QUESTION_ID, digest)
        if existing is not None:
            if existing.choice != choice:
                raise demo_store.DecisionConflictError(
                    f"问题 {demo_graph.QUESTION_ID}（摘要 {digest}）已保存决定"
                    f" {existing.choice}，本次提交的 {choice} 与之冲突，被拒绝"
                )
            decision_outcome = "already-in-effect"
        else:
            space.store.save_decision(
                pending["question_id"], digest, pending, choice, reason
            )
            decision_outcome = "saved"
        executions: list[StepExecution] = []
        resumed = space.graph(executions=executions)
        resumed.invoke(
            Command(
                resume={
                    "question_id": demo_graph.QUESTION_ID,
                    "question_digest": digest,
                    "choice": choice,
                }
            ),
            space.config,
        )
        end = resumed.get_state(space.config)
        end_pending = _pending_question(end)
        superseded = (
            end_pending is not None and end_pending["question_digest"] != digest
        )
        completed = end.next == () and end_pending is None and not superseded
        return DecideReport(
            decision_outcome=decision_outcome,
            completed=completed,
            superseded=superseded,
            choice=choice,
            executions=executions,
            billing=space.service.billing_summary(),
            summary=space.store.find_summary(demo_service.PAPER_ID),
            divergence=_divergence_lines(end.values),
        )
    finally:
        space.close()


def status(space_root: Path) -> StatusReport:
    """查看流程进度、固定输入、业务数据、待办、汇总与服务账单。"""
    space = DemoSpace(space_root)
    try:
        graph = space.graph()
        snapshot = graph.get_state(space.config)
        return StatusReport(
            started=snapshot.created_at is not None,
            checkpoint_next=tuple(snapshot.next),
            values=dict(snapshot.values),
            pending_question=_pending_question(snapshot),
            results=space.store.list_results(),
            decisions=space.store.list_decisions(),
            summary=space.store.find_summary(demo_service.PAPER_ID),
            billing=space.service.billing_summary(),
        )
    finally:
        space.close()


def reset(space_root: Path) -> None:
    """清空演示空间的三个数据库，便于重新演示。"""
    space_root.mkdir(parents=True, exist_ok=True)
    for name in SPACE_DBS:
        for suffix in ("", "-wal", "-shm"):
            (space_root / f"{name}{suffix}").unlink(missing_ok=True)
