"""合成流程适配器：材料导入后的名单、贡献提取、专家等待与收尾。

科学内容固定在 ``material`` 模块与材料快照中；节点重入时先查本执行
的步骤结果，输入相同即复用，不再调用模拟服务。提取输出（名单、论文
贡献、对象、问题）按执行隔离编号；来源与证据是可共享的固定输入。
A 篇存在作者判断歧义：开启专家裁决时在贡献步骤保存问题并暂停；关闭
时保存问题、记录模型判断并继续，不请求或等待专家。

贡献步骤把问题创建与步骤回执分两次提交：需要专家时先把服务响应按
哈希归档并记录到运行清单，再创建问题；重启后从归档响应继续，不重
复调用服务，也不与新时间戳比较已保存的论文修订。
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..records.common import RecordRef, sha256_of
from ..records.patches import PatchApplicationError, apply_decision_patch
from ..records.questions import DecisionRecord, QuestionRecord, RulesRef
from ..records.runs import Attempt, ModelJudgment, StepRecord
from ..review import ReviewService, derive_question_id
from ..runtime import (
    ApplicationPlan,
    DefaultFlowAdapter,
    RuntimeContext,
    append_run_revision,
)
from . import material as m
from .service import MODEL_NAME, material_snapshot, snapshot_json

STEP_ROSTER = "extract_roster"
STEP_CONTRIBUTION = "extract_contribution"
STEP_FINALIZE = "finalize"

RESPONSE_ARCHIVE_NAME = "responses/extract_contribution.json"

_MODEL_JUDGMENT_TEXT = (
    "Model attempted the interpretation under both models and could not"
    " determine an overall assessment; the question remains unresolved."
)


class SyntheticState(TypedDict, total=False):
    execution_id: str
    paper_key: str
    material: dict
    expert_adjudication: bool
    rules_version: str
    roster_refs: list[dict]
    contribution_refs: list[dict]
    question_ref: dict | None
    summary: dict | None


class SyntheticFlow(DefaultFlowAdapter):
    """R05 合成流程；``fault_hooks`` 是合成入口的故障注入点。"""

    flow_id = "synthetic-r05"
    flow_version = "1"
    mode = "synthetic"

    def __init__(self, *, paper: str = "A", fault_hooks: dict[str, Any] | None = None):
        self.paper = paper
        self.fault_hooks = fault_hooks or {}

    def hook(self, name: str) -> None:
        callback = self.fault_hooks.get(name)
        if callback is not None:
            callback()

    # ---- 流程定义 ----

    def initial_state(self, ctx: RuntimeContext) -> dict:
        snapshot = material_snapshot(self.paper)
        return {
            "execution_id": ctx.execution.execution_id,
            "paper_key": snapshot["paper_key"],
            "material": snapshot,
            "expert_adjudication": ctx.execution.input_snapshot.expert_adjudication,
            "rules_version": ctx.execution.input_snapshot.rules.version,
            "roster_refs": [],
            "contribution_refs": [],
            "question_ref": None,
            "summary": None,
        }

    def build_graph(self, ctx: RuntimeContext):
        builder = StateGraph(SyntheticState)
        builder.add_node(STEP_ROSTER, lambda state: self._extract_roster(ctx, state))
        builder.add_node(
            STEP_CONTRIBUTION, lambda state: self._extract_contribution(ctx, state)
        )
        builder.add_node(STEP_FINALIZE, lambda state: self._finalize(ctx, state))
        builder.add_edge(START, STEP_ROSTER)
        builder.add_edge(STEP_ROSTER, STEP_CONTRIBUTION)
        builder.add_edge(STEP_CONTRIBUTION, STEP_FINALIZE)
        builder.add_edge(STEP_FINALIZE, END)
        return builder.compile(checkpointer=ctx.checkpointer)

    # ---- 节点 ----

    def _op_input(self, ctx: RuntimeContext, step: str, state: dict) -> dict:
        return {
            "execution_id": ctx.execution.execution_id,
            "step": step,
            "paper_key": state["paper_key"],
            "material_sha256": sha256_of(
                {"snapshot": snapshot_json(state["material"])}
            ),
            "rules": state["rules_version"],
            "model": MODEL_NAME,
        }

    def _extract_roster(self, ctx: RuntimeContext, state: dict) -> dict:
        op_input = self._op_input(ctx, STEP_ROSTER, state)
        digest = sha256_of(op_input)
        saved = ctx.reuse_step(STEP_ROSTER, digest)
        if saved is not None:
            return {
                "roster_refs": [
                    ref.model_dump(mode="json") for ref in saved.result_refs
                ]
            }
        request = {"step": STEP_ROSTER, **op_input}
        ctx.service.call(step=STEP_ROSTER, request=request, material=state["material"])
        self.hook(f"after_response:{STEP_ROSTER}")
        roster = m.roster_record(
            self.paper,
            source_ref=RecordRef(record_id=m.SOURCE_IDS[self.paper], revision=1),
            created_at=ctx.now(),
            execution_id=ctx.execution.execution_id,
        )
        ctx.commit_step(
            step_id=STEP_ROSTER,
            input_digest=digest,
            records=[roster],
            result_refs=[roster.ref()],
        )
        return {"roster_refs": [roster.ref().model_dump(mode="json")]}

    def _stored_response(self, ctx: RuntimeContext) -> dict | None:
        """读取本执行已归档的贡献响应；哈希不符按损坏处理。"""
        run = ctx.space.store().read_record_current(ctx.execution.run_ref.record_id)
        step = (
            next((s for s in run.data.steps if s.step_id == STEP_CONTRIBUTION), None)
            if run is not None
            else None
        )
        if step is None:
            return None
        for attempt in reversed(step.attempts):
            if attempt.response is not None:
                data = ctx.space.read_file(attempt.response)
                return json.loads(data)
        return None

    def _persist_response(self, ctx: RuntimeContext, response: dict) -> None:
        """归档贡献响应并记入运行清单：问题创建前的可恢复检查点。"""
        ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            RESPONSE_ARCHIVE_NAME,
            snapshot_json(response).encode("utf-8"),
        )
        append_run_revision(
            ctx.space,
            ctx.execution,
            step=StepRecord(
                step_id=STEP_CONTRIBUTION,
                state="running",
                input_refs=[],
                attempts=[
                    Attempt(
                        request=None,
                        response=ref,
                        state="succeeded",
                        started_at=None,
                        finished_at=None,
                        usage=None,
                        cost=None,
                        currency=None,
                        service_request_id=None,
                    )
                ],
                result_refs=[],
                error_refs=[],
            ),
        )

    def _extract_contribution(self, ctx: RuntimeContext, state: dict) -> dict:
        op_input = {
            **self._op_input(ctx, STEP_CONTRIBUTION, state),
            "roster_refs": list(state["roster_refs"]),
        }
        digest = sha256_of(op_input)
        saved = ctx.reuse_step(STEP_CONTRIBUTION, digest)
        if saved is not None:
            refs = [ref.model_dump(mode="json") for ref in saved.result_refs]
        else:
            # 先查已归档的响应：问题创建与步骤回执分立提交的中断窗口内
            # 重启时不重复调用服务（收到响应但尚未归档的窗口除外）
            response = self._stored_response(ctx)
            if response is None:
                request = {"step": STEP_CONTRIBUTION, **op_input}
                response = ctx.service.call(
                    step=STEP_CONTRIBUTION, request=request, material=state["material"]
                )
                self.hook(f"after_response:{STEP_CONTRIBUTION}")
                if response["needs_expert"]:
                    self._persist_response(ctx, response)
            refs = self._commit_contribution(ctx, state, response)
        question_ref_dict = next(
            (r for r in refs if str(r["record_id"]).startswith("q-")), None
        )
        if state["material"]["needs_expert"] and state["expert_adjudication"]:
            # 等待专家决定：已应用则继续，其余情况保持暂停
            assert question_ref_dict is not None
            review = ReviewService(ctx.space)
            while True:
                view = review.get_question(RecordRef.model_validate(question_ref_dict))
                if view.application is not None and view.application.phase == "applied":
                    break
                interrupt(
                    {
                        "question_ref": question_ref_dict,
                        "step_id": STEP_CONTRIBUTION,
                        "prompt": view.record.data.prompt,
                    }
                )
        return {"contribution_refs": refs, "question_ref": question_ref_dict}

    def _commit_contribution(
        self, ctx: RuntimeContext, state: dict, response: dict
    ) -> list[dict]:
        review = ReviewService(ctx.space)
        execution_id = ctx.execution.execution_id
        paper_id = m.paper_record_id(self.paper, execution_id)
        source_ref = RecordRef(record_id=m.SOURCE_IDS[self.paper], revision=1)
        roster_ref = RecordRef(
            record_id=m.roster_record_id(self.paper, execution_id), revision=1
        )
        needs_expert = bool(response["needs_expert"])
        question_ref = None
        if needs_expert:
            rules: RulesRef = ctx.execution.input_snapshot.rules
            question_id = derive_question_id(
                execution_id,
                m.question_content(
                    self.paper,
                    source_ref=source_ref,
                    rules=rules,
                    paper_id=paper_id,
                ),
                space_id=ctx.space.identity.space_id,
            )
            question_ref = RecordRef(record_id=question_id, revision=1)
        paper = m.paper_record(
            self.paper,
            source_ref=source_ref,
            roster_ref=roster_ref,
            question_ref=question_ref,
            created_at=ctx.now(),
            execution_id=execution_id,
        )
        stored = ctx.store.read_record(paper.ref())
        if stored is not None:
            # 中断恢复：问题创建事务已保存同修订论文，复用原文，
            # 不与重新生成的时间戳比较
            paper = stored
        records = [paper]
        result_refs = [paper.ref()]
        if needs_expert:
            content = m.question_content(
                self.paper,
                source_ref=source_ref,
                rules=ctx.execution.input_snapshot.rules,
                paper_id=paper_id,
            )
            self.hook("before_question_create")
            view = review.create_question(
                execution_id=execution_id,
                step_id=STEP_CONTRIBUTION,
                request_key=f"{execution_id}:question:{m.PAPER_KEYS[self.paper]}",
                content=content,
                records=records,
                expected_revisions={},
            )
            self.hook("after_question_create")
            result_refs.append(view.question_ref)
            if not state["expert_adjudication"]:
                # 关闭专家裁决：疑问照常保存，模型判断入运行清单，不等待专家
                append_run_revision(
                    ctx.space,
                    ctx.execution,
                    model_judgment=ModelJudgment(
                        step_id=STEP_CONTRIBUTION,
                        judgment=_MODEL_JUDGMENT_TEXT,
                        created_at=ctx.now(),
                    ),
                )
        op_input = {
            **self._op_input(ctx, STEP_CONTRIBUTION, state),
            "roster_refs": list(state["roster_refs"]),
        }
        ctx.commit_step(
            step_id=STEP_CONTRIBUTION,
            input_digest=sha256_of(op_input),
            records=records,
            result_refs=result_refs,
        )
        return [ref.model_dump(mode="json") for ref in result_refs]

    def _finalize(self, ctx: RuntimeContext, state: dict) -> dict:
        execution_id = ctx.execution.execution_id
        paper_id = m.paper_record_id(self.paper, execution_id)
        paper_current = ctx.space.store().current_revision(paper_id)
        assert paper_current is not None
        paper_ref = RecordRef(record_id=paper_id, revision=paper_current)
        op_input = {
            **self._op_input(ctx, STEP_FINALIZE, state),
            "paper_ref": paper_ref.model_dump(mode="json"),
        }
        digest = sha256_of(op_input)
        saved = ctx.reuse_step(STEP_FINALIZE, digest)
        summary = {
            "object_id": m.object_record_id(self.paper, execution_id),
            "paper_ref": paper_ref.model_dump(mode="json"),
        }
        if saved is None:
            obj = m.object_record(
                self.paper,
                paper_ref=paper_ref,
                created_at=ctx.now(),
                execution_id=execution_id,
            )
            ctx.commit_step(
                step_id=STEP_FINALIZE,
                input_digest=digest,
                records=[obj],
                result_refs=[obj.ref()],
            )
        return {"summary": summary}

    # ---- 决定应用 ----

    def apply_decision(
        self, ctx: RuntimeContext, decision: DecisionRecord, question: QuestionRecord
    ) -> ApplicationPlan | None:
        patch = decision.data.result_patch
        if patch is None:
            return None
        record = ctx.space.store().read_record_current(patch.target.record_id)
        if record is None:
            return None
        try:
            patched = apply_decision_patch(record, patch)
        except PatchApplicationError:
            return None
        new_record = patched.model_copy(
            update={
                "revision": record.revision + 1,
                "created_at": ctx.now(),
                "execution_id": ctx.execution.execution_id,
            }
        )
        return ApplicationPlan(
            records=[new_record],
            expected_revisions={record.record_id: record.revision},
            result_refs=[new_record.ref()],
            notes={"patch_target": patch.target.model_dump(mode="json")},
        )

    def verify_state(self, ctx: RuntimeContext, state: dict) -> list[str]:
        problems: list[str] = []
        bound = state.get("execution_id")
        if bound is not None and bound != ctx.execution.execution_id:
            problems.append(
                f"检查点状态绑定执行 {bound}，当前执行为 {ctx.execution.execution_id}"
            )
        return problems

    def waiting_step(self, interrupt_payload: dict) -> str | None:
        step_id = interrupt_payload.get("step_id")
        return str(step_id) if step_id else None
